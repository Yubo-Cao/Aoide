"""Read Aoide API keys from the desktop Secret Service without logging them."""

import shutil
import subprocess


SERVICES = frozenset({"openai", "elevenlabs", "llm"})
LABELS = {
    "openai": "聆序（Aoide）· OpenAI 语音识别",
    "elevenlabs": "聆序（Aoide）· ElevenLabs 语音识别",
    "llm": "聆序（Aoide）· 大模型整理",
}


def lookup(service: str) -> str | None:
    if service not in SERVICES or not shutil.which("secret-tool"):
        return None
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", "aoide", "service", service],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.rstrip("\n") if result.returncode == 0 and result.stdout.strip() else None


def present(service: str) -> bool:
    """Check for an item without reading its value or unlocking a wallet."""
    if service not in SERVICES or not shutil.which("secret-tool"):
        return False
    try:
        result = subprocess.run(
            ["secret-tool", "search", "application", "aoide", "service", service],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def resolve(spec: str | None, service: str) -> str:
    """A stored key wins over a legacy env or literal key; no key is logged."""
    import os

    stored = lookup(service)
    if stored:
        return stored
    spec = spec or ""
    if spec.startswith("env:"):
        return os.environ.get(spec[4:], "")
    return spec


def store(service: str, key: str) -> None:
    if service not in SERVICES or not key.strip():
        raise ValueError("Choose a service and enter an API key")
    if not shutil.which("secret-tool"):
        raise RuntimeError("secret-tool is not installed")
    subprocess.run(
        ["secret-tool", "store", f"--label={LABELS[service]}",
         "application", "aoide", "service", service],
        input=key, text=True, capture_output=True, timeout=30, check=True,
    )


def clear(service: str) -> None:
    if service not in SERVICES:
        raise ValueError("Unknown service")
    if not shutil.which("secret-tool"):
        raise RuntimeError("secret-tool is not installed")
    subprocess.run(
        ["secret-tool", "clear", "application", "aoide", "service", service],
        text=True, capture_output=True, timeout=15, check=True,
    )
