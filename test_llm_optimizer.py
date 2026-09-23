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

    async def test_prompt_leaves_fillers_and_ellipses_to_the_model(self):
        self.optimizer._call_llm = AsyncMock(return_value="Claude Code……然后继续。")
        result = await self.optimizer.optimize("cloud code然后继续", urgent=True)
        self.assertEqual(result, "Claude Code……然后继续。")
        system = self.optimizer.system_prompt
        for phrase in ("filler words", "hesitation", "……嗯……", "carries meaning",
                       "homophones", "proper nouns", "established capitalization",
                       "Do not invent or insert", "Never translate", "Markdown bullet list",
                       "numbered list"):
            self.assertIn(phrase, system)
        # Personal homophone pairs belong in the user's config, not the default.
        for pair in ("翻到", "原上", "雨黄"):
            self.assertNotIn(pair, system)

    async def test_hesitation_ellipses_and_fillers_can_be_removed(self):
        raw = ("我们 demo 的话，好像它现在说的这个词儿和我们......嗯......对于正常的用户的 "
               "production 的 system 好像已经发生了很大的偏差。")
        edited = "我们 demo 现在说的词，好像和正常用户的 production system 已经有很大偏差。"
        self.optimizer._call_llm = AsyncMock(return_value=edited)
        self.assertEqual(await self.optimizer.optimize(raw, urgent=True), edited)
        self.assertTrue(self.optimizer._preserves_content("然后……继续", "然后继续。"))

    async def test_numbered_list_markers_are_not_changed_numbers(self):
        raw = "嗯明天要做两件事，第一个是把 3 台服务器升级，然后第二个呢就是测一下 API 延迟 250 毫秒的问题"
        edited = "明天要做两件事：\n\n1. 把 3 台服务器升级。\n2) 测一下 API 延迟 250 毫秒的问题。"
        self.optimizer._call_llm = AsyncMock(return_value=edited)
        self.assertEqual(await self.optimizer.optimize(raw, urgent=True), edited)
        bullets = "明天要做两件事：\n- 把 3 台服务器升级。\n- 测一下 API 延迟 250 毫秒的问题。"
        self.assertTrue(self.optimizer._preserves_content(raw, bullets))

    async def test_numbered_list_cannot_hide_a_changed_number(self):
        raw = "第一个是把 3 台服务器升级，第二个是测一下 250 毫秒的问题"
        self.assertIn("missing [3], added [4]", self.optimizer._content_problem(
            raw, "1. 把 4 台服务器升级。\n2. 测一下 250 毫秒的问题。"))
        self.assertIn("missing [250]", self.optimizer._content_problem(
            raw, "1. 把 3 台服务器升级。\n2. 测一下毫秒级的问题。"))
        # A digit that starts a line but is not a list marker is still compared.
        self.assertIsNotNone(self.optimizer._content_problem(
            "版本 2.0 发布了", "3.0 版本发布了"))
        self.assertIsNone(self.optimizer._content_problem("版本 2.0 发布了", "2.0 版本发布了"))

    async def test_lost_english_is_rejected_with_details(self):
        raw = "如果能整理成 markdown 或者 structured information 的时候，它也没有把它变成 structured information"
        with self.assertLogs("aoide.llm", "WARNING") as logs:
            self.optimizer._call_llm = AsyncMock(return_value="如果能整理成 Markdown 或结构化信息，它也没有把它变成结构化信息。")
            self.assertIsNone(await self.optimizer.optimize(raw, urgent=True))
        self.assertIn("English words retained 1/5", logs.output[0])
        self.assertIn("missing: information, structured", logs.output[0])

    async def test_truncated_cleanup_is_rejected(self):
        raw = ("把我们之前在 Voice Agent 上的迭代都同步到网上的 demo 上，然后检查 production "
               "system 的偏差，最后给我写一份汇报，以 email 的形式发过来。")
        with self.assertLogs("aoide.llm", "WARNING") as logs:
            self.optimizer._call_llm = AsyncMock(return_value="把之前的迭代同步。")
            self.assertIsNone(await self.optimizer.optimize(raw, urgent=True))
        self.assertIn("text shrank to", logs.output[0])

    async def test_dictionary_term_cannot_be_corrupted(self):
        dictionary_path = self.optimizer.personal_dictionary.path
        dictionary_path.write_text(
            'terms:\n  - term: Claude Code\n    aliases: [cloud code]\n',
            encoding="utf-8")
        self.optimizer._call_llm = AsyncMock(return_value="cloud coat 很好用。")
        with self.assertLogs("aoide.llm", "WARNING") as logs:
            self.assertIsNone(await self.optimizer.optimize("Claude Code 很好用", urgent=True))
        self.assertIn("dictionary term 'Claude Code' missing", logs.output[0])

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
        with self.assertLogs("aoide.llm", "WARNING") as logs:
            self.assertIsNone(await self.optimizer.optimize("部署 3 台服务器。", urgent=True))
        self.assertIn("numbers changed: missing [3], added [4]", logs.output[0])


if __name__ == "__main__":
    unittest.main()
