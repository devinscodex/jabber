"""Project files: storage, chunked upload, browsing, reading, search.

Layout under <data_dir>/users/<ns>/:
  files/<project_id>/tree/...   what the user and the model see (text, extracted
                                 document text as <name>.extracted.txt, images)
  files/<project_id>/src/...    original documents kept for re-extraction
  uploads/<upload_id>.part|.json   in-flight chunked uploads

Upload protocol (each step is its own request, so no single request is large):
  POST /api/projects/<id>/upload/begin   {"path": "dir/file.pdf", "size": N} -> {"upload_id"}
  POST /api/uploads/<uid>/chunk?offset=K  raw bytes (<= 16 MB), K must equal bytes so far
  POST /api/uploads/<uid>/finish          -> {"written_paths": [...], "notes": [...]}
"""
import json
import os
import re
import shutil
import struct
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import extract, store

CHUNK_MAX = 16 * 1024 * 1024
EXTRACT_SOURCE_MAX = 512 * 1024 * 1024
LIST_MAX = 200
PREVIEW_CHARS = 200_000
IMAGE_MAX_PIXELS = 40_000_000
STALE_UPLOAD_SECONDS = 24 * 3600
_HEX32 = re.compile(r"^[a-f0-9]{32}$")


class FileError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def clean_rel(path) -> str:
    """A safe relative path using '/', or raises FileError."""
    if not isinstance(path, str):
        raise FileError(400, "path must be text")
    p = path.replace("\\", "/").strip()
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or len(p) > 400:
        raise FileError(400, "invalid file path")
    for x in parts:
        if x == ".." or x.startswith(".") or any(ord(c) < 32 for c in x) or len(x) > 200:
            raise FileError(400, f"invalid file path component {x!r}")
    return "/".join(parts)


def validate_image(raw: bytes):
    """(width, height) for PNG/JPEG/GIF/WebP, else raises ValueError."""
    w = h = None
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        w, h = struct.unpack(">II", raw[16:24])
    elif raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
        w, h = struct.unpack("<HH", raw[6:10])
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" and len(raw) >= 30:
        kind = raw[12:16]
        if kind == b"VP8 ":
            w, h = struct.unpack("<HH", raw[26:30])
            w, h = w & 0x3FFF, h & 0x3FFF
        elif kind == b"VP8L":
            bits = struct.unpack("<I", raw[21:25])[0]
            w, h = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        elif kind == b"VP8X":
            w = int.from_bytes(raw[24:27], "little") + 1
            h = int.from_bytes(raw[27:30], "little") + 1
    elif raw[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                i += 1
                continue
            m = raw[i + 1]
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                break
            i += 2 + seg
    if not w or not h:
        raise ValueError("not a readable PNG, JPEG, GIF or WebP image")
    if w * h > IMAGE_MAX_PIXELS:
        raise ValueError(f"image is {w}x{h}, over the {IMAGE_MAX_PIXELS:,} pixel limit")
    return w, h


class Files:
    def __init__(self, data_dir, max_upload_bytes, quota_bytes):
        self.root = Path(data_dir) / "users"
        self.max_upload = max_upload_bytes
        self.quota = quota_bytes

    # ---- paths
    def _user(self, ns) -> Path:
        if not re.fullmatch(r"u[a-f0-9]{30}", ns):
            raise ValueError("bad namespace")
        return self.root / ns

    def proj_dir(self, ns, pid) -> Path:
        if not _HEX32.match(pid):
            raise FileError(404, "no such project")
        return self._user(ns) / "files" / pid

    def tree(self, ns, pid) -> Path:
        return self.proj_dir(ns, pid) / "tree"

    def resolve(self, ns, pid, rel) -> Path:
        base = self.tree(ns, pid).resolve()
        target = (base / rel).resolve() if rel not in (".", "") else base
        if target != base and base not in target.parents:
            raise FileError(400, "path is outside the project files")
        return target

    def usage(self, ns) -> int:
        d = self._user(ns) / "files"
        total = 0
        if d.is_dir():
            for dp, _, fs in os.walk(d):
                for f in fs:
                    try:
                        total += os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        pass
        return total

    def has_files(self, ns, pid) -> bool:
        t = self.tree(ns, pid)
        return t.is_dir() and any(t.iterdir())

    def purge(self, ns, pid):
        shutil.rmtree(self.proj_dir(ns, pid), ignore_errors=True)

    # ---- upload protocol
    def _updir(self, ns) -> Path:
        d = self._user(ns) / "uploads"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    def _sweep(self, ns):
        cutoff = time.time() - STALE_UPLOAD_SECONDS
        for f in self._updir(ns).iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    def begin(self, ns, pid, path, size) -> str:
        rel = clean_rel(path)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise FileError(400, "size must be a non-negative integer")
        if size > self.max_upload:
            raise FileError(413, f"{rel} is {size:,} bytes, over the {self.max_upload:,}-byte per-file limit")
        if self.usage(ns) + size > self.quota:
            raise FileError(413, f"not enough storage left for {rel} ({self.quota:,}-byte account quota)")
        self._sweep(ns)
        uid = uuid.uuid4().hex
        d = self._updir(ns)
        (d / f"{uid}.part").write_bytes(b"")
        os.chmod(d / f"{uid}.part", 0o600)
        (d / f"{uid}.json").write_text(json.dumps({"project": pid, "path": rel, "size": size}))
        return uid

    def _upload(self, ns, uid):
        if not _HEX32.match(uid or ""):
            raise FileError(404, "no such upload")
        d = self._updir(ns)
        try:
            meta = json.loads((d / f"{uid}.json").read_text())
        except (FileNotFoundError, ValueError):
            raise FileError(404, "no such upload")
        return meta, d / f"{uid}.part", d / f"{uid}.json"

    def chunk(self, ns, uid, offset, data: bytes) -> int:
        meta, part, _ = self._upload(ns, uid)
        have = part.stat().st_size
        if offset != have:
            raise FileError(409, f"expected offset {have}, got {offset}")
        if have + len(data) > meta["size"]:
            raise FileError(413, "more data than the declared size")
        with open(part, "ab") as f:
            f.write(data)
        return have + len(data)

    def abort(self, ns, uid):
        try:
            _, part, mf = self._upload(ns, uid)
        except FileError:
            return
        part.unlink(missing_ok=True)
        mf.unlink(missing_ok=True)

    def finish(self, ns, uid):
        """Returns (project_id, written_paths, notes). Raises FileError."""
        meta, part, mf = self._upload(ns, uid)
        try:
            if part.stat().st_size != meta["size"]:
                raise FileError(409, f"incomplete upload: {part.stat().st_size} of {meta['size']} bytes")
            written, notes = self._store(ns, meta["project"], meta["path"], part)
            return meta["project"], written, notes
        finally:
            part.unlink(missing_ok=True)
            mf.unlink(missing_ok=True)

    def _store(self, ns, pid, rel, part: Path):
        kind = extract.kind_of(rel)
        tree = self.tree(ns, pid)
        notes = []

        def put(dst_rel, source=None, text=None):
            dst = self.resolve(ns, pid, dst_rel)
            dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if text is not None:
                dst.write_text(text, encoding="utf-8")
            else:
                os.replace(source, dst) if source.parent == dst.parent else shutil.copyfile(source, dst)
            return dst_rel

        size = part.stat().st_size
        if kind == "text":
            with open(part, "rb") as f:
                if b"\x00" in f.read(8192):
                    raise FileError(415, f"{rel}: looks like a binary file, not text")
            return [put(rel, source=part)], notes
        if kind == "image":
            raw = part.read_bytes()
            try:
                validate_image(raw)
            except ValueError as e:
                raise FileError(415, f"{rel}: {e}")
            return [put(rel, source=part)], notes
        if kind in ("pdf", "ooxml", "legacy"):
            if size > EXTRACT_SOURCE_MAX:
                raise FileError(413, f"{rel}: documents over {EXTRACT_SOURCE_MAX // 2**20} MB cannot be read yet")
            raw = part.read_bytes()
            try:
                text, doc_notes = extract.extract(rel, raw)
            except ValueError as e:
                raise FileError(422, f"{rel}: {e}")
            src = self.proj_dir(ns, pid) / "src" / rel
            src.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(part, src)
            out = put(rel + ".extracted.txt", text=text)
            notes += [f"{rel}: {n}" for n in doc_notes]
            if not text.strip():
                notes.append(f"{rel}: no text could be extracted")
            return [out], notes
        raise FileError(415, f"{rel}: {('%s files' % Path(rel).suffix) if Path(rel).suffix else 'this kind of file'} cannot be read")

    # ---- browse / read / search
    def list_dir(self, ns, pid, rel="."):
        target = self.resolve(ns, pid, rel)
        if rel in (".", "") and not target.exists():
            return {"entries": [], "details": [], "truncated": False}
        if not target.is_dir():
            raise FileError(400, f"{rel!r} is not a folder")
        items = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        names = [p.name + ("/" if p.is_dir() else "") for p in items]
        details = [{"name": p.name, "type": "dir" if p.is_dir() else "file",
                    "size": None if p.is_dir() else p.stat().st_size} for p in items[:LIST_MAX]]
        return {"entries": names[:LIST_MAX], "details": details, "truncated": len(names) > LIST_MAX}

    def read(self, ns, pid, rel, offset=0, limit=PREVIEW_CHARS):
        target = self.resolve(ns, pid, rel)
        if not target.is_file():
            raise FileError(400, f"{rel!r} is not a file")
        if extract.kind_of(target.name) == "image":
            w, h = validate_image(target.read_bytes())
            return {"content": f"(image {w}x{h}, {target.stat().st_size:,} bytes: shown to the model only when you attach it to a message)",
                    "total_chars": 0, "offset": 0, "next_offset": None}
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), PREVIEW_CHARS))
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            total_bytes = f.tell()
            f.seek(0)
            if offset:
                f.read(offset)
            content = f.read(limit)
            more = bool(f.read(1))
        return {"content": content, "offset": offset, "next_offset": offset + len(content) if more else None,
                "total_bytes": total_bytes}

    def search(self, ns, pid, query, rel=".", max_results=40):
        q = (query or "").strip().lower()
        if len(q) < 2:
            raise FileError(400, "search text must be at least 2 characters")
        base = self.resolve(ns, pid, rel)
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        hits, scanned = [], 0
        for f in files:
            if extract.kind_of(f.name) == "image":
                continue
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        scanned += len(line)
                        if q in line.lower():
                            i = line.lower().index(q)
                            snip = line[max(0, i - 80):i + 120].strip()
                            hits.append({"path": str(f.relative_to(self.tree(ns, pid).resolve())), "line": n, "text": snip})
                            if len(hits) >= max_results:
                                return {"matches": hits, "truncated": True}
                        if scanned > 400_000_000:
                            return {"matches": hits, "truncated": True}
            except OSError:
                continue
        return {"matches": hits, "truncated": False}

    def top_listing(self, ns, pid, limit=25):
        try:
            return self.list_dir(ns, pid)["entries"][:limit]
        except FileError:
            return []


# ------------------------------------------------------------------ HTTP
_UPLOAD_BEGIN = re.compile(r"^/api/projects/([a-f0-9]{32})/upload/begin$")
_UPLOAD_STEP = re.compile(r"^/api/uploads/([a-f0-9]{32})/(chunk|finish|abort)$")
_PROJ_FILES = re.compile(r"^/api/projects/([a-f0-9]{32})/(files|file|clear-files)$")


def handle(h, method, path):
    m_begin, m_step, m_pf = _UPLOAD_BEGIN.match(path), _UPLOAD_STEP.match(path), _PROJ_FILES.match(path)
    if not (m_begin or m_step or m_pf):
        return False
    ident = h._identity()
    if not ident:
        h._json(401, {"error": "sign in required"})
        return True
    ns = store.ns_for(ident.id)
    fl, st = h.app.files, h.app.store
    q = parse_qs(urlparse(h.path).query)
    try:
        if m_begin and method == "POST":
            pid = m_begin.group(1)
            st.load(ns, "projects", pid)
            body = h._body()
            if body is None:
                return True
            uid = fl.begin(ns, pid, body.get("path"), body.get("size"))
            h._json(200, {"upload_id": uid, "chunk_bytes": h.app.cfg.chunk_bytes})
        elif m_step and method == "POST":
            uid, step = m_step.groups()
            if step == "chunk":
                try:
                    n = int(h.headers.get("Content-Length", ""))
                    off = int(q.get("offset", [""])[0])
                except ValueError:
                    raise FileError(400, "offset and Content-Length are required")
                if n < 0 or n > CHUNK_MAX:
                    raise FileError(413, f"chunks are limited to {CHUNK_MAX} bytes")
                data = h.rfile.read(n)
                if len(data) != n:
                    raise FileError(400, "chunk was cut short")
                h._json(200, {"received": fl.chunk(ns, uid, off, data)})
            elif step == "abort":
                fl.abort(ns, uid)
                h._json(200, {"ok": True})
            else:
                pid, written, notes = fl.finish(ns, uid)
                try:
                    p = st.load(ns, "projects", pid)
                    if not p.get("has_files"):
                        p["has_files"] = True
                        st.save(ns, "projects", p)
                except store.NotFound:
                    pass
                h.app.telemetry.event("upload", user=ns, ok=len(written), notes=len(notes))
                h._json(200, {"written_paths": written, "notes": notes})
        elif m_pf:
            pid, what = m_pf.groups()
            proj = st.load(ns, "projects", pid)
            if what == "files" and method == "GET":
                h._json(200, fl.list_dir(ns, pid, (q.get("path") or ["."])[0]))
            elif what == "file" and method == "GET":
                p = (q.get("path") or [""])[0]
                if not p:
                    raise FileError(400, "expected ?path=<relative file path>")
                h._json(200, fl.read(ns, pid, p))
            elif what == "clear-files" and method == "POST":
                if h._body() is None:
                    return True
                fl.purge(ns, pid)
                proj["has_files"] = False
                st.save(ns, "projects", proj)
                h._json(200, proj)
            else:
                h._json(405, {"error": "method not allowed"})
        else:
            h._json(405, {"error": "method not allowed"})
    except FileError as e:
        h._json(e.status, {"error": e.message})
    except store.NotFound:
        h._json(404, {"error": "no such project"})
    return True
