"""Keep a bounded local recovery copy before handing text to the input method."""
import json
import os
import tempfile
import time
from pathlib import Path


class ResultStore:
    def __init__(self, directory=None):
        self.directory = Path(directory or "~/.local/state/aoide/results").expanduser()

    def save(self, raw, text):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        record = json.dumps({"created_ns": time.time_ns(), "raw": raw, "text": text}, ensure_ascii=False)
        name = f"result-{time.time_ns()}.json"
        for target, content in [(name, record), ("latest.txt", text)]:
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".pending-")
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(content)
                os.replace(tmp, self.directory / target)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        for old in sorted(self.directory.glob("result-*.json"))[:-10]:
            old.unlink()
