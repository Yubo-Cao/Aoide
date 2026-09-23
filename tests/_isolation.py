"""Keep unit tests away from real credentials and the user's Aoide data.

Every ``tests/test_*.py`` imports this module before any backend module
(``test_isolation.py`` enforces it). On import it:

- removes provider/Aoide environment variables (API keys, AWS profiles, ...);
- points HOME and the XDG directories at a throwaway directory, so the real
  ``~/.config/aoide`` (dictionary, config, cloud.env) and ``~/.aws`` are never read;
- puts a fake ``secret-tool`` first on PATH that finds nothing, and cuts the
  D-Bus session address, so the desktop wallet (Secret Service) is never queried;
- points boto3 at empty AWS config/credential files;
- links only the non-secret native assets (``~/.local/lib/aoide`` denoise library,
  ``~/.local/share/aoide/models`` VAD model) into the fake home when present.

Tests that need a key set a fake one themselves (``patch.dict(os.environ, ...)``).
"""
import atexit
import os
import re
import shutil
import tempfile
from pathlib import Path

REAL_HOME = Path.home()

# Optional local speech sample for audio tests (not in the repository).
SPEECH_FIXTURE = Path(os.environ.get(
    "AOIDE_TEST_SPEECH_WAV", REAL_HOME / ".local/state/aoide/bench-20260921/clean_zh.wav"))

# Built artifacts and public models the audio tests load; no keys or user data.
_ASSETS = (".local/lib/aoide", ".local/share/aoide/models")

_SENSITIVE = re.compile(
    r"^(AOIDE_|YUHUANG_|OPENAI_|ELEVENLABS_|XI_|OPENROUTER_|ANTHROPIC_|AWS_|BOTO_|DEEPSEEK_|"
    r"DASHSCOPE_|GEMINI_|GOOGLE_API|AZURE_OPENAI)|(_API_KEY|_TOKEN|_SECRET)$")

ROOT = Path(tempfile.mkdtemp(prefix="aoide-tests-"))
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)
BIN = ROOT / "bin"


def _isolate():
    for name in list(os.environ):
        if _SENSITIVE.search(name):
            del os.environ[name]
    home = ROOT / "home"
    for sub in (".config", ".local/share", ".local/state", ".cache"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    for asset in _ASSETS:
        if (REAL_HOME / asset).exists():
            (home / asset).parent.mkdir(parents=True, exist_ok=True)
            (home / asset).symlink_to(REAL_HOME / asset)
    os.environ.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "DBUS_SESSION_BUS_ADDRESS": "disabled:",
        "AWS_CONFIG_FILE": str(ROOT / "aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(ROOT / "aws-credentials"),
        "AWS_EC2_METADATA_DISABLED": "true",
    })
    BIN.mkdir(exist_ok=True)
    fake = BIN / "secret-tool"
    fake.write_text("#!/bin/sh\n# Test stand-in: the wallet is always empty.\nexit 1\n")
    fake.chmod(0o755)
    os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}"


_isolate()
