"""Resolve the per-user Aoide IPC address, including the old public default."""

import os
from pathlib import Path


LEGACY_SOCKET = "/tmp/yuhuang-backend.sock"


def default_socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(f"/tmp/aoide-{os.getuid()}")
    return str(base / "aoide" / "backend.sock")


def resolve_socket_path(value: str | None) -> str:
    if not value or value in ("auto", LEGACY_SOCKET):
        return default_socket_path()
    return os.path.expanduser(os.path.expandvars(value))
