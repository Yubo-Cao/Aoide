import _isolation  # noqa: F401  -- must precede backend imports (no real keys/data)
import asyncio
import json
from pathlib import Path
import stat
import tempfile
import unittest

from backend.personal_dictionary import PersonalDictionary
from backend.llm_optimizer import LLMOptimizer
from backend.result_store import ResultStore
from backend.pipeline import PTTPipeline


class DictionaryTests(unittest.TestCase):
    def test_hot_reload_boundaries_and_no_cascade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictionary.yaml"
            path.write_text('terms:\n - {term: Codex, aliases: [code x]}\n - {term: final, aliases: [Codex]}\n')
            d = PersonalDictionary(path).reload()
            self.assertEqual(d.apply_aliases('code x, mycode x, code xyz'), 'Codex, mycode x, code xyz')
            path.write_text('terms: [Kylian]\n')
            d.reload()
            self.assertIn('Kylian', d.hints())
            self.assertEqual(d.apply_aliases('code x'), 'code x')
            path.write_text('terms: [')
            d.reload()
            self.assertIn('Kylian', d.hints())

    def test_spelling_changes_are_allowed_by_preservation_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictionary.yaml"
            path.write_text('terms:\n - {term: Codex, aliases: [code x]}\n')
            optimizer = LLMOptimizer(optimize_delay=0)
            optimizer.personal_dictionary = PersonalDictionary(path)
            raw = 'Use code x and code x for this task 42.'
            async def mock_call(prompt, urgent=False):
                self.assertIn('Personal vocabulary', prompt)
                return 'Use Codex and Codex for this task 42.'
            optimizer._call_llm = mock_call
            self.assertEqual(asyncio.run(optimizer.optimize(raw)), 'Use Codex and Codex for this task 42.')


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_complete_result_before_delivery_and_bound_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ResultStore(tmp)
            text = '第一句话不能丢。\n第二句话包含 Codex。'
            class Server:
                async def broadcast(self, msg):
                    if msg['type'] == 'replace':
                        self.assert_saved = Path(tmp, 'latest.txt').read_text()
            server = Server()
            p = PTTPipeline(server, commit_on_release=True, result_store=store)
            await p.on_intermediate(text)
            await p.finalize()
            self.assertEqual(server.assert_saved, text)
            for i in range(12):
                store.save(str(i), str(i))
            self.assertEqual(len(list(Path(tmp).glob('result-*.json'))), 10)
            self.assertEqual(stat.S_IMODE(Path(tmp, 'latest.txt').stat().st_mode), 0o600)
            record = json.loads(sorted(Path(tmp).glob('result-*.json'))[-1].read_text())
            self.assertEqual(record['text'], '11')
