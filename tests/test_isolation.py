"""The test environment must never reach real keys, the wallet, or the user's Aoide data."""
import _isolation  # noqa: F401  -- must precede backend imports (no real keys/data)
import ast
import os
import shutil
import unittest
from pathlib import Path

from backend import secret_store
from backend.personal_dictionary import PersonalDictionary


class IsolationTests(unittest.TestCase):
    def test_no_provider_or_aoide_secrets_in_environment(self):
        leaked = [name for name in os.environ if _isolation._SENSITIVE.search(name)
                  and name not in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
                                   "AWS_EC2_METADATA_DISABLED")]
        self.assertEqual(leaked, [])

    def test_wallet_is_unreachable(self):
        self.assertEqual(shutil.which("secret-tool"), str(_isolation.BIN / "secret-tool"))
        self.assertEqual(os.environ["DBUS_SESSION_BUS_ADDRESS"], "disabled:")
        for service in secret_store.SERVICES:
            self.assertIsNone(secret_store.lookup(service))
            self.assertFalse(secret_store.present(service))
        self.assertEqual(secret_store.resolve("env:AOIDE_OPENAI_API_KEY", "openai"), "")

    def test_user_data_directories_are_temporary(self):
        home = Path.home()
        self.assertTrue(home.is_relative_to(_isolation.ROOT))
        self.assertNotEqual(home, _isolation.REAL_HOME)
        self.assertTrue(PersonalDictionary().path.is_relative_to(_isolation.ROOT))
        self.assertFalse((home / ".aws").exists())

    def test_aws_has_no_profiles(self):
        try:
            import boto3
        except ImportError:
            self.skipTest("boto3 not installed")
        session = boto3.Session()
        self.assertEqual(session.available_profiles, [])
        self.assertIsNone(session.get_credentials())

    def test_every_test_module_imports_isolation_first(self):
        for path in sorted(Path(__file__).parent.glob("test_*.py")):
            tree = ast.parse(path.read_text())
            imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
            with self.subTest(path.name):
                self.assertTrue(imports, "no imports")
                first = imports[0]
                self.assertIsInstance(first, ast.Import)
                self.assertEqual([alias.name for alias in first.names], ["_isolation"])


if __name__ == "__main__":
    unittest.main()
