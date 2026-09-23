"""Cloud protocol and microphone-tail regression tests; no external requests."""
import _isolation  # noqa: F401  -- must precede backend imports (no real keys/data)
import asyncio
import json
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import httpx
import numpy as np

from backend.audio_capture import AudioCapture
from backend.llm_optimizer import LLMOptimizer
from backend.pipeline import PTTPipeline


def stream_response(text="你好，OpenAI。", reason="stop"):
    chunks = [
        {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": reason}]},
    ]
    return httpx.Response(200, text="".join(
        "data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")


class CloudRefineTests(unittest.IsolatedAsyncioTestCase):
    async def optimizer(self, handler, url="https://api.openai.com/v1"):
        opt = LLMOptimizer(base_url=url, api_key="env:AOIDE_TEST_KEY",
                           model="gpt-4.1-mini", optimize_delay=0)
        opt._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        opt._client_base_url = url
        self.addAsyncCleanup(opt.close)
        return opt

    async def test_openai_stream_and_environment_key(self):
        def handler(request):
            self.assertEqual(request.headers["authorization"], "Bearer test-value")
            self.assertEqual(request.url.path, "/v1/chat/completions")
            payload = json.loads(request.content)
            for key in ("thinking", "enable_thinking", "chat_template_kwargs", "reasoning"):
                self.assertNotIn(key, payload)
            return stream_response()
        opt = await self.optimizer(handler)
        with patch.dict(os.environ, {"AOIDE_TEST_KEY": "test-value"}):
            self.assertEqual(await opt.optimize("你好open ai"), "你好，OpenAI。")

    async def test_openrouter_reasoning_parameter(self):
        def handler(request):
            self.assertEqual(json.loads(request.content)["reasoning"], {"enabled": False})
            return stream_response()
        opt = await self.optimizer(handler, "https://openrouter.ai/api/v1")
        with patch.dict(os.environ, {"AOIDE_TEST_KEY": "test-value"}):
            self.assertIsNotNone(await opt.optimize("测试"))

    async def test_truncated_output_falls_back(self):
        opt = await self.optimizer(lambda r: stream_response(reason="length"))
        with patch.dict(os.environ, {"AOIDE_TEST_KEY": "test-value"}):
            self.assertIsNone(await opt.optimize("原文不能被截断的输出覆盖"))

    async def test_auth_failure_is_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(401)
        opt = await self.optimizer(handler)
        with patch.dict(os.environ, {"AOIDE_TEST_KEY": "test-value"}):
            self.assertIsNone(await opt.optimize("测试"))
            self.assertIsNone(await opt.optimize("再次测试"))
        self.assertEqual(len(calls), 1)

    async def test_key_updates_are_redacted(self):
        opt = await self.optimizer(lambda r: stream_response())
        with self.assertLogs("aoide.llm", level="INFO") as logs:
            opt.update_config(api_key="private-secret-value")
        self.assertNotIn("private-secret-value", " ".join(logs.output))

    async def test_short_dictation_fallback_keeps_refinement(self):
        class Sink:
            def __init__(self):
                self.messages = []
            async def broadcast(self, message):
                self.messages.append(message)
        opt = await self.optimizer(lambda r: stream_response("我们明天测试 OpenAI 接口。"))
        sink = Sink()
        pipeline = PTTPipeline(sink, opt)
        pipeline.buffer._chars.extend("嗯我们我们明天测试OpenAI接口")
        with patch.dict(os.environ, {"AOIDE_TEST_KEY": "test-value"}):
            await pipeline.finalize()
        result = next(m for m in sink.messages if m["type"] == "replace")
        self.assertEqual(result["text"], "我们明天测试 OpenAI 接口。")
        self.assertEqual(result["fallback_text"], result["text"])
        self.assertEqual(result["delete_chars"], 0)


class AudioTailTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_routing_moves_only_our_stream(self):
        commands = []
        def pactl(cmd, **kwargs):
            commands.append(cmd)
            args = cmd[1:]
            if args == ["get-default-source"]:
                return SimpleNamespace(stdout="internal-mic\n")
            data = {
                "clients": [{"index": 5, "properties": {"application.process.id": str(os.getpid())}},
                            {"index": 6, "properties": {"application.process.id": "other-process"}}],
                "sources": [{"index": 12, "name": "internal-mic"}],
                "source-outputs": [{"index": 20, "client": 5, "source": 11},
                                   {"index": 21, "client": 6, "source": 11}],
            }
            return SimpleNamespace(stdout=json.dumps(data.get(args[-1], {})))
        capture = AudioCapture()
        with patch("backend.audio_capture.subprocess.run", side_effect=pactl):
            capture._follow_pulse_default()
        moves = [c for c in commands if c[1] == "move-source-output"]
        self.assertEqual(moves, [["pactl", "move-source-output", "20", "internal-mic"]])

    async def test_thread_callback_delivers_tail_after_release(self):
        capture = AudioCapture()
        capture._loop = asyncio.get_running_loop()
        capture._running = True
        received = []
        async def receive(data):
            received.append(data)
        capture._audio_callback = receive
        forward = asyncio.create_task(capture._audio_forward_loop())
        capture.start_listening()
        audio = np.array([[1], [-32768], [1000]], dtype=np.int16)
        await asyncio.to_thread(capture._sounddevice_callback, audio, 3, None, None)
        capture.stop_listening()
        await asyncio.wait_for(capture.drain_pending(), timeout=1)
        self.assertEqual(received, [audio.tobytes()])
        self.assertEqual(capture.session_peak, 32768)
        capture._running = False
        forward.cancel()
        await asyncio.gather(forward, return_exceptions=True)

    async def test_stale_session_audio_is_ignored(self):
        capture = AudioCapture()
        capture.start_listening()
        old_session = capture._session
        capture.stop_listening()
        capture.start_listening()
        capture._enqueue_audio(old_session, b"\x01\x00")
        self.assertTrue(capture._audio_queue.empty())
        self.assertEqual(capture.session_peak, 0)


if __name__ == "__main__":
    unittest.main()
