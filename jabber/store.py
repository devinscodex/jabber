"""Per-user JSON storage for conversations and projects.

Layout: <data_dir>/users/<ns>/<kind>/<id>.json, where ns is derived only from
the authenticated identity id (never from client input) and ids are 32 hex
chars. Every path is built from those two validated parts, so one user can
never address another user's files. Writes are atomic (temp file + rename).
"""
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path

ID_RE = re.compile(r"^[a-f0-9]{32}$")
KINDS = ("conversations", "projects")


class NotFound(Exception):
    pass


def new_id() -> str:
    return uuid.uuid4().hex


def ns_for(identity_id: str) -> str:
    return "u" + hashlib.sha256(identity_id.encode()).hexdigest()[:30]


class Store:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "users"
        self._lock = threading.RLock()

    def _dir(self, ns, kind, create=False) -> Path:
        if kind not in KINDS or not re.fullmatch(r"u[a-f0-9]{30}", ns):
            raise ValueError("bad namespace or kind")
        d = self.root / ns / kind
        if create:
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    def _path(self, ns, kind, obj_id) -> Path:
        if not isinstance(obj_id, str) or not ID_RE.match(obj_id):
            raise NotFound(obj_id)
        return self._dir(ns, kind) / f"{obj_id}.json"

    def load(self, ns, kind, obj_id) -> dict:
        p = self._path(ns, kind, obj_id)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raise NotFound(obj_id)

    def save(self, ns, kind, obj):
        p = self._path(ns, kind, obj["id"])
        with self._lock:
            self._dir(ns, kind, create=True)
            tmp = p.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f)
            os.replace(tmp, p)

    def delete(self, ns, kind, obj_id):
        p = self._path(ns, kind, obj_id)
        with self._lock:
            try:
                p.unlink()
            except FileNotFoundError:
                raise NotFound(obj_id)

    def list(self, ns, kind) -> list:
        d = self._dir(ns, kind)
        out = []
        if d.is_dir():
            for f in d.glob("*.json"):
                try:
                    out.append(json.loads(f.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    continue
        return out
