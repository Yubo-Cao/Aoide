"""Coverage for Aoide's desktop settings and migration paths."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from backend.secret_store import resolve
from backend.socket_path import resolve_socket_path


class SocketMigrationTests(unittest.TestCase):
    def test_old_default_and_auto_use_private_runtime_path(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}):
            expected = "/run/user/1000/aoide/backend.sock"
            self.assertEqual(resolve_socket_path("auto"), expected)
            self.assertEqual(resolve_socket_path("/tmp/yuhuang-backend.sock"), expected)
            self.assertEqual(resolve_socket_path("/tmp/custom.sock"), "/tmp/custom.sock")


class SecretResolutionTests(unittest.TestCase):
    def test_wallet_key_wins_and_environment_remains_compatible(self):
        with patch("backend.secret_store.lookup", return_value="wallet-key"):
            self.assertEqual(resolve("env:LEGACY_KEY", "openai"), "wallet-key")
        with patch("backend.secret_store.lookup", return_value=None):
            with patch.dict(os.environ, {"LEGACY_KEY": "old-key"}):
                self.assertEqual(resolve("env:LEGACY_KEY", "openai"), "old-key")


class DictionarySettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_table_round_trip_preserves_other_yaml_keys(self):
        from tools import aoide_settings

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dictionary.yaml"
            path.write_text(yaml.safe_dump({"notes": "keep", "terms": [{"term": "FunASR", "aliases": ["方ASR"]}]}))
            with patch.object(aoide_settings, "DICTIONARY", path):
                window = aoide_settings.SettingsWindow()
                self.assertEqual(window.table.rowCount(), 1)
                window._add_term("Claude Code", "扣的, Claude Cod")
                window._save_dictionary()
                data = yaml.safe_load(path.read_text())
                self.assertEqual(data["notes"], "keep")
                self.assertEqual(data["terms"][1], {"term": "Claude Code", "aliases": ["扣的", "Claude Cod"]})
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                window.close()

    def test_chinese_and_english_settings_pages(self):
        from tools import aoide_settings

        with patch("tools.aoide_settings.secret_store.present", return_value=False):
            for language, expected_title, expected_tabs in (
                ("zh_CN", "聆序设置", ["API 密钥", "个人词典", "输入与预览"]),
                ("en_US", "Aoide Settings", ["API Keys", "Personal Dictionary", "Input & Preview"]),
            ):
                with self.subTest(language=language), patch.dict(os.environ, {"AOIDE_UI_LANG": language}):
                    window = aoide_settings.SettingsWindow()
                    self.assertEqual(window.windowTitle(), expected_title)
                    tabs = window.centralWidget()
                    self.assertEqual([tabs.tabText(i) for i in range(tabs.count())], expected_tabs)
                    window.close()


if __name__ == "__main__":
    unittest.main()
