"""Focused checks for dictation cleanup and content safeguards."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.llm_optimizer import LLMOptimizer
from backend.personal_dictionary import PersonalDictionary


class LLMOptimizerCopyEditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.optimizer = LLMOptimizer(optimize_delay=0)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.optimizer.personal_dictionary = PersonalDictionary(
            Path(self.temp_dir.name) / "dictionary.yaml")

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_prompt_requests_ellipsis_and_supported_proper_noun_spelling(self):
        self.optimizer._call_llm = AsyncMock(return_value="Claude Code……然后继续。")
        result = await self.optimizer.optimize("cloud code然后继续", urgent=True)
        self.assertEqual(result, "Claude Code……然后继续。")
        system = self.optimizer.system_prompt
        self.assertIn("……", system)
        self.assertIn("proper nouns", system)
        self.assertIn("established capitalization", system)
        self.assertIn("Do not invent or insert", system)

    async def test_existing_ellipsis_cannot_disappear(self):
        self.optimizer._call_llm = AsyncMock(return_value="然后继续。")
        self.assertIsNone(await self.optimizer.optimize("然后……继续", urgent=True))
        self.assertTrue(self.optimizer._preserves_content("然后……继续", "然后...继续"))

    async def test_dictionary_term_cannot_be_corrupted(self):
        dictionary_path = self.optimizer.personal_dictionary.path
        dictionary_path.write_text(
            'terms:\n  - term: Claude Code\n    aliases: [cloud code]\n',
            encoding="utf-8")
        self.optimizer._call_llm = AsyncMock(return_value="cloud coat 很好用。")
        self.assertIsNone(await self.optimizer.optimize("Claude Code 很好用", urgent=True))

    async def test_dictionary_alias_can_be_corrected(self):
        dictionary_path = self.optimizer.personal_dictionary.path
        dictionary_path.write_text(
            'terms:\n  - term: Claude Code\n    aliases: [cloud code]\n',
            encoding="utf-8")
        self.optimizer._call_llm = AsyncMock(return_value="cloud code 很好用。")
        self.assertEqual(
            await self.optimizer.optimize("cloud code 很好用", urgent=True),
            "Claude Code 很好用。")

    async def test_filler_cleanup_and_markdown_are_accepted(self):
        raw = "呃，我觉得第一，我们需要修复登录。然后，然后第二，测试登录。"
        edited = "1. 修复登录。\n2. 测试登录。"
        self.optimizer._call_llm = AsyncMock(return_value=edited)
        self.assertEqual(await self.optimizer.optimize(raw, urgent=True), edited)
        self.assertIn("Markdown", self.optimizer.system_prompt)
        self.assertIn("filler words", self.optimizer.system_prompt)

    async def test_number_change_still_rejected(self):
        self.optimizer._call_llm = AsyncMock(return_value="部署 4 台服务器。")
        self.assertIsNone(await self.optimizer.optimize("部署 3 台服务器。", urgent=True))


if __name__ == "__main__":
    unittest.main()
