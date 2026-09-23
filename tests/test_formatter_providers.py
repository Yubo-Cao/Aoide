"""Cleanup LLM provider selection: OpenAI reasoning models and Amazon Bedrock (no network)."""
import _isolation  # noqa: F401  -- must precede backend imports (no real keys/data)
import json
import os
import unittest
from unittest.mock import patch

import httpx

from backend.llm_optimizer import LLMOptimizer
from backend.pipeline import PTTPipeline


def stream_response(text):
    chunks = [{"choices": [{"delta": {"content": text}, "finish_reason": None}]},
              {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
    return httpx.Response(200, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                          + "data: [DONE]\n\n")


class FakeBedrock:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response
        self.error = error

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class ClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


def bedrock_reply(text, stop="end_turn"):
    return {"stopReason": stop, "output": {"message": {"content": [{"text": text}]}}}


class FormatterTests(unittest.IsolatedAsyncioTestCase):
    async def openai(self, model, effort, handler):
        opt = LLMOptimizer(base_url="https://api.openai.com/v1", api_key="env:AOIDE_TEST_KEY",
                           model=model, optimize_delay=0, reasoning_effort=effort, temperature=0.1)
        opt._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opt._client_base_url = opt.base_url
        self.addAsyncCleanup(opt.close)
        return opt

    async def test_reasoning_model_payload(self):
        seen = []
        def handler(request):
            seen.append(json.loads(request.content))
            return stream_response("测试 OpenAI。")
        with patch.dict(os.environ, {"AOIDE_TEST_KEY": "k"}):
            luna = await self.openai("gpt-6-luna", "none", handler)
            self.assertEqual(await luna.optimize("测试open ai"), "测试 OpenAI。")
            classic = await self.openai("gpt-4.1", "none", handler)
            await classic.optimize("测试open ai")
            medium = await self.openai("gpt-6-luna", "low", handler)
            await medium.optimize("测试open ai")
        self.assertEqual(seen[0]["reasoning_effort"], "none")
        self.assertIn("max_completion_tokens", seen[0])
        self.assertNotIn("max_tokens", seen[0])
        self.assertEqual(seen[0]["temperature"], 0.1)
        # Non-reasoning model never receives reasoning_effort, even if YAML sets it.
        self.assertNotIn("reasoning_effort", seen[1])
        self.assertIn("max_tokens", seen[1])
        self.assertNotIn("temperature", seen[2])

    async def test_bedrock_selected_by_base_url(self):
        opt = LLMOptimizer(base_url="bedrock:us-east-1", model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
                           aws_profile="test-profile", optimize_delay=0, temperature=0.1, max_tokens=512)
        self.assertEqual(opt.provider, "bedrock")
        fake = FakeBedrock(bedrock_reply("我们明天测试 OpenAI 接口。"))
        with patch.object(opt, "_bedrock_client", return_value=fake):
            self.assertEqual(await opt.optimize("我们明天测试open ai接口"), "我们明天测试 OpenAI 接口。")
        call = fake.calls[0]
        self.assertEqual(call["modelId"], "global.anthropic.claude-haiku-4-5-20251001-v1:0")
        self.assertEqual(call["inferenceConfig"], {"maxTokens": 512, "temperature": 0.1})
        self.assertEqual(call["system"], [{"text": opt.system_prompt}])
        self.assertEqual(LLMOptimizer(base_url="https://api.openai.com/v1").provider, "openai")

    async def test_bedrock_real_client_uses_named_profile(self):
        opt = LLMOptimizer(base_url="bedrock:us-west-2", model="m", aws_profile="some-profile")
        with patch("boto3.Session") as session:
            opt._bedrock_client()
        session.assert_called_once_with(profile_name="some-profile", region_name="us-west-2")

    async def test_bedrock_failures_keep_transcript(self):
        opt = LLMOptimizer(base_url="bedrock:us-east-1", model="m", optimize_delay=0)
        truncated = FakeBedrock(bedrock_reply("半截", stop="max_tokens"))
        with patch.object(opt, "_bedrock_client", return_value=truncated):
            self.assertIsNone(await opt.optimize("完整的原文不能被截断"))
        denied = FakeBedrock(error=ClientError("AccessDeniedException"))
        with patch.object(opt, "_bedrock_client", return_value=denied):
            self.assertIsNone(await opt.optimize("第一次"))
            self.assertIsNone(await opt.optimize("第二次"))
        self.assertEqual(len(denied.calls), 1)  # auth rejection stops further calls
        opt.update_config(base_url="bedrock:us-west-2")
        self.assertFalse(opt._auth_failed)  # switching endpoint gets a fresh try

    async def test_bedrock_output_passes_content_guard(self):
        opt = LLMOptimizer(base_url="bedrock:us-east-1", model="m", optimize_delay=0)
        dropped = FakeBedrock(bedrock_reply("明天测试。"))
        with patch.object(opt, "_bedrock_client", return_value=dropped):
            self.assertIsNone(await opt.optimize(
                "明天测试。He was writing music for other people.预算是25美元。"))

    async def test_pipeline_uses_configured_timeout(self):
        class Slow(LLMOptimizer):
            async def optimize(self, text, **kw):
                import asyncio
                await asyncio.sleep(1)
                return "不应出现"
        class Sink:
            messages = []
            async def broadcast(self, m):
                self.messages.append(m)
        import time
        slow = Slow(timeout=0.05)
        pipeline = PTTPipeline(Sink(), slow, commit_on_release=True)
        t = time.monotonic()
        self.assertEqual(await pipeline._refine_final("原文"), "原文")
        self.assertLess(time.monotonic() - t, 0.5)


if __name__ == "__main__":
    unittest.main()
