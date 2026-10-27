"""Structured event log for the operator: one JSON object per line.

Records timings, sizes, model names and outcomes. It never records message
text, prompts, replies, file contents, or passwords. Users appear only as an
opaque namespace hash. The file is owner-only and rotates by size.
"""
import json
import os
import threading
import time
from pathlib import Path


class Telemetry:
    def __init__(self, log_dir, enabled=True, max_bytes=20 * 1024 * 1024, keep=3):
        self.enabled = enabled
        self.dir = Path(log_dir)
        self.path = self.dir / "events.jsonl"
        self.max_bytes = max_bytes
        self.keep = keep
        self._lock = threading.Lock()

    def event(self, kind, **fields):
        if not self.enabled:
            return
        rec = {"ts": round(time.time(), 3), "kind": kind}
        rec.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    self._rotate()
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass  # logging must never break a request

    def _rotate(self):
        for i in range(self.keep, 0, -1):
            src = self.path if i == 1 else self.dir / f"events.jsonl.{i - 1}"
            dst = self.dir / f"events.jsonl.{i}"
            if src.exists():
                if i == self.keep and dst.exists():
                    dst.unlink()
                os.replace(src, dst)

    def read(self, since=0.0):
        files = [self.dir / f"events.jsonl.{i}" for i in range(self.keep, 0, -1)] + [self.path]
        for f in files:
            if not f.exists():
                continue
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("ts", 0) >= since:
                        yield rec
