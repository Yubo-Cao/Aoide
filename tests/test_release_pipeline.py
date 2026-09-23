import _isolation  # noqa: F401  -- must precede backend imports (no real keys/data)
import asyncio
import unittest
from unittest.mock import AsyncMock

import numpy as np

from backend.pipeline import PTTPipeline
from backend.speech_frontend import SpeechFrontend
from backend.llm_optimizer import LLMOptimizer


class Sink:
    def __init__(self):
        self.messages = []
    async def broadcast(self, message):
        self.messages.append(message)


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_refinement_cannot_delete_english_or_change_numbers(self):
        raw = "明天测试。He was writing music for other people.预算是25美元。"
        self.assertFalse(LLMOptimizer._preserves_content(raw, "明天测试，预算是25美元。"))
        self.assertFalse(LLMOptimizer._preserves_content(raw, raw.replace("25", "50")))
        self.assertTrue(LLMOptimizer._preserves_content(raw, raw.replace("。He", "。\nHe")))

    async def test_long_speech_never_commits_before_release(self):
        sink = Sink()
        optimizer = AsyncMock()
        optimizer.optimize.return_value = "整理后的完整长段。"
        pipeline = PTTPipeline(sink, optimizer, commit_on_release=True)
        pipeline.start_emergency_timer()
        for i in range(1, 8):
            await pipeline.on_intermediate("这是一段很长而且持续说话的中文内容。" * (i * 10))
            await pipeline.on_offline_correction("完整纠正段落。" * (i * 10), i)
            await pipeline.commit_now()
        self.assertTrue(all(m["type"] == "preedit" for m in sink.messages))
        optimizer.optimize.assert_not_called()
        self.assertIsNone(pipeline._emergency_timer)
        await pipeline.finalize()
        commits = [m for m in sink.messages if m["type"] in ("commit", "replace", "interrupt_commit")]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["text"], "整理后的完整长段。")
        optimizer.optimize.assert_awaited_once()

    async def test_focus_interrupt_cancels_without_upload_or_commit(self):
        sink = Sink()
        optimizer = AsyncMock()
        pipeline = PTTPipeline(sink, optimizer, commit_on_release=True)
        await pipeline.on_intermediate("不应进入新窗口的内容")
        await pipeline.finalize_interrupt()
        self.assertFalse(any(m["type"] in ("commit", "replace", "interrupt_commit") for m in sink.messages))
        self.assertEqual(sink.messages[-1]["type"], "interrupt_done")
        optimizer.optimize.assert_not_called()


class FrontendTests(unittest.TestCase):
    def test_silence_and_tail_preservation(self):
        f = SpeechFrontend()
        self.addCleanup(f.close)
        # Non-10ms aligned input catches lost final frames.
        f.process(np.zeros(16007, np.int16).tobytes())
        self.assertEqual(f.finish(), [])
        self.assertEqual(f.samples, 16007)
        self.assertEqual(f.finish(), [])
        self.assertEqual(f.samples, 16007)

    def test_segments_never_overlap_or_exceed_25_seconds(self):
        f = SpeechFrontend(denoise=False)
        self.addCleanup(f.close)
        f.parts = [np.arange(16000 * 61, dtype=np.int16)]
        f.samples = len(f.parts[0])
        f.probabilities = [0.99] * ((f.samples + 511) // 512)
        f.finished = True
        chunks = f.finish()
        self.assertTrue(all(len(x) <= 25 * 16000 for x in chunks))
        np.testing.assert_array_equal(np.concatenate(chunks), f.parts[0])


if __name__ == "__main__":
    unittest.main()
