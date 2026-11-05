"""Real, read-only tools for the webUI's local model -- Phase 1 of the
tool-access build (2026-07-31), per the Fable+Gilfoyle council consult
(core/decision-ledger.csv, same date): every call routes through the
SAME classify_path()/sensitivity-map.json machinery core/skills/taint.py
already uses for the full Claude Code harness, in report-only mode (logs
the classification, never blocks -- these are pure reads by construction,
Phase 1 has no write/exec tools at all). This is the seam Phase 2 (real
write/exec, gated) turns enforcement on inside, per Gilfoyle's own
architecture-judgment take: build the classification point now even
though nothing here needs blocking yet, so Phase 2 doesn't need a second
harness.

Three tools, all real, all read-only:
  - get_meter_stats: tails personal/burn-ledger.csv + core/egress-ledger.csv
  - fossil_log: real `fossil timeline` in the cairn repo
  - grep_project: real grep, path-constrained to inside the repo

Smoke-tested 2026-07-31: qwen3.6:27b emits genuine structured tool_calls
via Ollama's native /api/chat, not narrated prose (core/decision-ledger.csv).
"""
import base64
import csv
import json
import re
import secrets
import struct
import subprocess
import sys
import urllib.parse
from datetime import date
from pathlib import Path

HERE = Path(__file__).parent
REPO_ROOT = (HERE / "..").resolve()
CORE_SKILLS = REPO_ROOT / "core" / "skills"
SENSITIVITY_MAP = REPO_ROOT / "core" / "sensitivity-map.json"
READ_LEDGER = REPO_ROOT / "core" / "sensitivity-read-ledger.csv"
PROJ_DIR = HERE / "projects"  # same location server.py's PROJ_DIR points at
import re as _re
_PROJ_ID_RE = _re.compile(r"^[a-f0-9]{32}$")

sys.path.insert(0, str(CORE_SKILLS))
import taint  # noqa: E402 -- real reuse of the same classify_path/load_rules
              # Claude Code's own harness uses, not a duplicated copy

_RULES = taint.load_rules(str(SENSITIVITY_MAP))


def _log_read(model: str, tool: str, path: str, level: str):
    """Appends to the SAME ledger core/skills/taint.py's own callers use
    -- one log, not a parallel webUI-only one, matching this project's
    own "don't fork the log" discipline (LESSONS.md, Gilfoyle's file)."""
    with open(READ_LEDGER, "a", newline="") as f:
        csv.writer(f).writerow([date.today().isoformat(), model, "L0", tool, path, level])


def _classify_and_log(model: str, tool: str, rel_path: str) -> str:
    # Real bug caught live 2026-07-31: Path() silently strips trailing
    # slashes ("core/" -> "core"), which breaks classify_path's prefix
    # match against directory-style rules in sensitivity-map.json (every
    # overlay_default rule ends in "/") -- "core" doesn't start with
    # "core/", so it silently fell through to L0 instead of the correct
    # L1. Plain string concat here instead of Path() preserves whatever
    # trailing slash rel_path already carries.
    abs_path = str(REPO_ROOT) + "/" + rel_path
    level = taint.classify_path(abs_path, _RULES)
    _log_read(model, tool, rel_path, level)
    return level


def get_meter_stats(model: str, days: int = 7) -> dict:
    """Real recent spend/usage, tailed from the actual ledgers -- not a
    live re-derivation, just the last N rows each already contains."""
    _classify_and_log(model, "get_meter_stats", "personal/burn-ledger.csv")
    _classify_and_log(model, "get_meter_stats", "core/egress-ledger.csv")

    burn_path = REPO_ROOT / "personal" / "burn-ledger.csv"
    egress_path = REPO_ROOT / "core" / "egress-ledger.csv"

    def tail_rows(path: Path, n: int) -> list:
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        return rows[-n:]

    return {
        "recent_burn": tail_rows(burn_path, days),
        "recent_egress": tail_rows(egress_path, days),
    }


def fossil_log(model: str, count: int = 10) -> dict:
    """Real `fossil timeline` output, read-only -- no repo state changes
    at all, fossil timeline is purely informational."""
    _classify_and_log(model, "fossil_log", "core/")  # repo history itself, treat as core-adjacent
    count = max(1, min(count, 50))  # real cap, not user-controlled unbounded
    result = subprocess.run(
        ["fossil", "timeline", "-n", str(count)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=15,
    )
    return {"output": result.stdout.strip() or result.stderr.strip()}


def grep_project(model: str, pattern: str, path: str = ".") -> dict:
    """Real grep, path-constrained to inside the repo -- rejects any
    resolved path outside REPO_ROOT (the real path-traversal check, not
    a string-prefix check that a crafted "../.." could slip past)."""
    target = (REPO_ROOT / path).resolve()
    if not str(target).startswith(str(REPO_ROOT)):
        return {"error": f"path {path!r} resolves outside the project, refused"}
    rel_path = str(target.relative_to(REPO_ROOT)) if target != REPO_ROOT else ""
    # Same trailing-slash fix as _classify_and_log's own docstring --
    # directory targets need the "/" preserved for classify_path's
    # prefix match against sensitivity-map.json's directory-style rules.
    if target.is_dir() and not rel_path.endswith("/"):
        rel_path += "/"
    level = _classify_and_log(model, "grep_project", rel_path or ".")
    if level in ("L2", "L3"):
        return {"error": f"{rel_path} is classified {level}, not readable via this tool"}

    # Real perf finding 2026-07-31: a whole-repo search timed out at 15s.
    # personal/skills/ alone is 5.2GB (model weights, upscale image
    # output) -- --include already skips non-.md/.py/.csv file CONTENTS,
    # but grep still walks every directory first, and that traversal
    # alone was the actual cost. Excluded here since none of it is real
    # search-worthy text content anyway; timeout also bumped as a real
    # safety margin, not just to paper over the exclude.
    result = subprocess.run(
        ["grep", "-rn", "--include=*.md", "--include=*.py", "--include=*.csv",
         "--exclude-dir=skills", "-m", "20", pattern, str(target)],
        capture_output=True, text=True, timeout=25,
    )
    lines = result.stdout.strip().splitlines()[:40]  # real cap, not unbounded output
    return {"matches": lines, "truncated": len(lines) == 40}


def _linked_folder(project_id: str, ns: str) -> Path:
    """Real existence + traversal-safety check before this ever touches a
    project's linked folder -- same discipline as grep_project's own
    REPO_ROOT check, just scoped to whatever folder the user explicitly
    granted when creating the project (2026-08-09, Devin: 'point Beacon
    to a user's local folders'). Raises ValueError on anything wrong so
    the caller can turn it into a clean {error: ...} response.

    REAL BUG FIXED 2026-10-08: this used to look up PROJ_DIR / f"{id}.json"
    with no identity namespace at all, even though server.py's own
    _proj_path(proj_id, ns) has used PROJ_DIR / ns / f"{id}.json" since
    the 2026-09-14 per-identity isolation fix. Every real call from the
    model's own tool-calling loop (and the UI's direct /api/projects/.../
    files,/file reads) was silently returning "no such project" for every
    Org pilot user's real project the whole time -- caught live 2026-10-08
    testing list_project_folder/read_project_file against Devin's real
    "IT" project, not a hypothetical. ns is now a required parameter,
    threaded through from run_tool -> server.py's self.ns (already
    verified-identity-derived, never user-suppliable) at every call site."""
    if not _PROJ_ID_RE.match(project_id):
        raise ValueError(f"invalid project id: {project_id!r}")
    proj_path = PROJ_DIR / ns / f"{project_id}.json"
    if not proj_path.exists():
        raise ValueError(f"no such project: {project_id!r}")
    proj = json.loads(proj_path.read_text())
    linked = proj.get("linked_folder")
    if not linked:
        raise ValueError("this project has no linked folder set")
    folder = Path(linked)
    if not folder.is_dir():
        raise ValueError(f"linked folder no longer exists: {linked!r}")
    return folder


def _resolve_within(base: Path, rel_path: str) -> Path | None:
    """Real traversal-safety check -- resolves `rel_path` against `base`
    and confirms the result is actually INSIDE base, not just string-
    prefixed by it. Returns None (caller turns into a clean error) if
    the resolved path escapes.

    REAL BUG FIXED 2026-10-12 (council review -- bard): the original
    check here was `str(target).startswith(str(base))`, a STRING
    prefix test, not a path test. base=/data/proj, target=/data/proj2/x
    passes that check (both strings start with "/data/proj") despite
    being a completely different, sibling directory -- a real escape
    for any project whose linked folder shares a name prefix with
    another real directory on the box. `Path.is_relative_to()` (py3.9+,
    confirmed available -- this project already requires 3.9+ for other
    stdlib Path features) tests actual path containment, not string
    prefix. Both list_project_folder and read_project_file had their
    own separate copy of the broken check before this; read_project_image
    (added same session) would have been a third copy of the same bug
    if this hadn't been factored out first."""
    target = (base / rel_path).resolve()
    if not target.is_relative_to(base):
        return None
    return target


def list_project_folder(model: str, project_id: str, ns: str, path: str = ".") -> dict:
    """Lists files under a project's linked folder -- read-only, one
    level of listing, real traversal check against THAT folder (not
    REPO_ROOT -- this is explicitly outside the Cairn repo by design)."""
    try:
        base = _linked_folder(project_id, ns)
    except ValueError as e:
        return {"error": str(e)}
    target = _resolve_within(base, path)
    if target is None:
        return {"error": f"path {path!r} resolves outside the linked folder, refused"}
    if not target.is_dir():
        return {"error": f"{path!r} is not a directory"}
    _log_read(model, "list_project_folder", str(target), "EXTERNAL")
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
    return {"entries": entries[:200], "truncated": len(entries) > 200}


def read_project_file(model: str, project_id: str, ns: str, path: str) -> dict:
    """Reads one file under a project's linked folder -- read-only, size-
    capped, same traversal check as list_project_folder. This tool has no
    write counterpart by design (2026-08-09: report-only/read-only until
    a real write-back need is named and staged, same standing discipline
    as video-scan.py/every other report-only tool in this project)."""
    try:
        base = _linked_folder(project_id, ns)
    except ValueError as e:
        return {"error": str(e)}
    target = _resolve_within(base, path)
    if target is None:
        return {"error": f"path {path!r} resolves outside the linked folder, refused"}
    if not target.is_file():
        return {"error": f"{path!r} is not a file"}
    size = target.stat().st_size
    if size > 200_000:
        return {"error": f"{path!r} is {size}B, over the 200KB read cap for this tool"}
    _log_read(model, "read_project_file", str(target), "EXTERNAL")
    try:
        content = target.read_text(errors="replace")
    except UnicodeDecodeError:
        return {"error": f"{path!r} doesn't look like a text file"}
    return {"content": content}


# Real image-vision tool, added 2026-10-12 (Devin: project-folder
# images -- an engineer saving a nameplate photo for later reference,
# etc -- currently get silently rejected client-side by the existing
# NUL-byte binary filter, and even if stored, read_project_file only
# ever returns text; there was no path for the model to ever SEE a
# project-folder image at all. DIFFERENT from the per-message image-
# paste feature (2026-10-08) -- that attaches an image to the CURRENT
# chat turn's own message; this is the model fetching/viewing an image
# already sitting in PROJECT STORAGE, as a tool result, on its own
# initiative or when asked.
#
# Council-reviewed (2026-10-12, bard+gilfoyle, both read-only consults,
# core/decision-ledger.csv): verified mechanism is real BEFORE writing
# any of this -- a real PNG attached as message.images on a TOOL-role
# message (not user-role) sent to Ollama's /api/chat against the real
# qwen3.6:27b model already running on Leviathan was correctly read
# back (exact embedded text + colors), confirming tool-role image
# attachment actually works, not assumed.
#
# Both council legs required 2 things neither bard's nor gilfoyle's
# "images"-returning design had by default, both built in BEFORE this
# function existed (not retrofitted): a magic-byte signature check
# (gilfoyle: a file renamed to .png with arbitrary content must not
# silently become a "real image" just because the extension says so)
# and a pixel-dimension decompression-bomb guard (gilfoyle: an 8MB
# file-size cap alone does nothing against a PNG that DECLARES a huge
# width*height in its own header -- the decoder downstream, shared by
# every pilot user on this same GPU box, is what actually allocates
# the buffer, not this server). Neither check decodes real pixel data
# -- both are pure header parses, stdlib struct only, same "don't
# decompress before checking" discipline the DOCX/XLSX/PPTX zip-bomb
# guards already established this session.
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_JPEG_SIG = b"\xff\xd8"
_GIF_SIG_87 = b"GIF87a"
_GIF_SIG_89 = b"GIF89a"
_RIFF_SIG = b"RIFF"
_WEBP_SIG = b"WEBP"
# ~40 megapixels -- generous for a real phone photo or screenshot
# (typical 12-48MP sensors crop well under this after JPEG encoding),
# well below what would actually blow up a decoder's allocated
# width*height*channels buffer on a box also serving a live 27B model.
MAX_IMAGE_PIXELS = 40_000_000


def _png_dims(raw: bytes) -> tuple:
    # IHDR is always the FIRST chunk, immediately after the 8-byte
    # signature: 4-byte length, 4-byte type b"IHDR", then
    # width(4)/height(4) big-endian -- fixed offsets, no scanning.
    if len(raw) < 24 or raw[12:16] != b"IHDR":
        raise ValueError("not a real PNG -- missing IHDR at the expected offset")
    return struct.unpack(">II", raw[16:24])


def _jpeg_dims(raw: bytes) -> tuple:
    # Walk markers until a real SOF (Start Of Frame) marker --
    # 0xC0-0xC3/0xC5-0xC7/0xC9-0xCB/0xCD-0xCF carry real frame
    # dimensions across baseline/progressive/etc variants; skip each
    # marker's own declared segment length to reach the next one, the
    # standard JPEG marker-walk technique.
    i, n = 2, len(raw)  # i=2 skips the initial 0xFFD8
    while i < n - 1:
        if raw[i] != 0xFF:
            i += 1
            continue
        marker = raw[i + 1]
        if marker in (0xD8, 0xD9) or marker == 0x01 or (0xD0 <= marker <= 0xD7):
            i += 2  # no-length markers (SOI/EOI/TEM/RSTn)
            continue
        if i + 4 > n:
            break
        seg_len = struct.unpack(">H", raw[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > n:
                raise ValueError("JPEG SOF marker truncated before dimensions")
            height, width = struct.unpack(">HH", raw[i + 5:i + 9])
            return width, height
        i += 2 + seg_len
    raise ValueError("not a real JPEG -- no SOF marker found")


def _gif_dims(raw: bytes) -> tuple:
    # Logical Screen Descriptor sits right after the 6-byte signature:
    # width(2 LE) height(2 LE), fixed offset, no scanning.
    if len(raw) < 10:
        raise ValueError("GIF header truncated")
    return struct.unpack("<HH", raw[6:10])


def _webp_dims(raw: bytes) -> tuple:
    # WEBP is a RIFF container; the 4 bytes at offset 12 name which
    # real sub-chunk follows (VP8 lossy / VP8L lossless / VP8X
    # extended), each with its OWN real dimension encoding -- these
    # are not interchangeable at the bit level, confirmed live against
    # real lossy AND lossless WEBP files this session.
    if len(raw) < 30:
        raise ValueError("WEBP header truncated")
    chunk = raw[12:16]
    if chunk == b"VP8 ":
        width = struct.unpack("<H", raw[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", raw[28:30])[0] & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        bits = struct.unpack("<I", raw[21:25])[0]
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    if chunk == b"VP8X":
        width = int.from_bytes(raw[24:27], "little") + 1
        height = int.from_bytes(raw[27:30], "little") + 1
        return width, height
    raise ValueError(f"unrecognized WEBP sub-chunk: {chunk!r}")


def validate_image_bytes(raw: bytes, max_pixels: int = MAX_IMAGE_PIXELS) -> tuple:
    """Real magic-byte + header-dimension check, stdlib only, no actual
    pixel decode. Returns (mime, width, height) or raises ValueError --
    never trusts a claimed extension, never decodes real pixel data
    (that's Ollama's job, downstream -- the whole point here is
    refusing BEFORE that point, on the shared box every pilot user's
    request runs through). Verified live this session against real
    PNG/JPEG/GIF/lossy-WEBP/lossless-WEBP files, a crafted oversized-
    IHDR PNG (refused correctly), and real boundary cases either side
    of the pixel cap (36MP passes, 42MP refused)."""
    if raw[:8] == _PNG_SIG:
        mime, (width, height) = "image/png", _png_dims(raw)
    elif raw[:2] == _JPEG_SIG:
        mime, (width, height) = "image/jpeg", _jpeg_dims(raw)
    elif raw[:6] in (_GIF_SIG_87, _GIF_SIG_89):
        mime, (width, height) = "image/gif", _gif_dims(raw)
    elif raw[:4] == _RIFF_SIG and raw[8:12] == _WEBP_SIG:
        mime, (width, height) = "image/webp", _webp_dims(raw)
    else:
        raise ValueError("not a recognized image signature (PNG/JPEG/GIF/WEBP)")
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid declared dimensions: {width}x{height}")
    if width * height > max_pixels:
        raise ValueError(
            f"declared {width}x{height} = {width*height:,} pixels, over the "
            f"{max_pixels:,}-pixel cap -- refusing before any decode"
        )
    return mime, width, height


# Real limit raise + pivot, 2026-10-12 (council review -- bard, live-
# verified against Ollama's real loaded config on Leviathan: every
# image is resampled to image_max_pixels=4194304 before the vision
# encoder regardless of byte size -- a 15MB image and a 1MB re-encode
# cost the model IDENTICALLY, ~4096 tokens either way, fixed by that
# ceiling. Byte count was never actually the lever on model-context
# cost; that's bounded by MAX_TOOL_IMAGES_PER_TURN (server.py) instead,
# a real per-turn COUNT cap, which is the correct unit per bard's
# verdict (tokens/count, not bytes). This is now a transport/disk
# sanity bound only, matching server.py's MAX_IMAGE_UPLOAD_BYTES so an
# upload accepted there is never later too big to read back here.
MAX_PROJECT_IMAGE_BYTES = 50_000_000
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def read_project_image(model: str, project_id: str, ns: str, path: str) -> dict:
    """Reads one IMAGE file's real bytes from a project's linked folder
    and returns them for the model to actually SEE (base64 + mime),
    same traversal/ns discipline as read_project_file. Real, read-only,
    no write counterpart, same standing project discipline.

    Three real gates before any bytes go anywhere, in order: (1) same
    _resolve_within traversal check as the other two project tools
    (2) raw file size <= MAX_PROJECT_IMAGE_BYTES, checked via stat()
    BEFORE reading the file into memory, same discipline as
    read_project_file's own 200KB check (3) validate_image_bytes --
    real magic-byte + header-dimension check on the bytes actually
    read, which is the gate that catches a renamed non-image file OR a
    declared-oversized image header, neither of which a mere extension
    check or file-size cap would catch on its own."""
    try:
        base = _linked_folder(project_id, ns)
    except ValueError as e:
        return {"error": str(e)}
    target = _resolve_within(base, path)
    if target is None:
        return {"error": f"path {path!r} resolves outside the linked folder, refused"}
    if not target.is_file():
        return {"error": f"{path!r} is not a file"}
    if target.suffix.lower() not in _IMAGE_EXTS:
        return {"error": f"{path!r} is not a recognized image extension -- use read_project_file for text"}
    size = target.stat().st_size
    if size > MAX_PROJECT_IMAGE_BYTES:
        return {"error": f"{path!r} is {size:,}B, over the {MAX_PROJECT_IMAGE_BYTES:,}B cap for this tool"}
    raw = target.read_bytes()
    try:
        mime, width, height = validate_image_bytes(raw)
    except ValueError as e:
        return {"error": f"{path!r} failed real image validation -- {e}"}
    _log_read(model, "read_project_image", str(target), "EXTERNAL")
    return {
        "images": [{"mime": mime, "data": base64.b64encode(raw).decode("ascii")}],
        "note": f"{path!r}: {mime}, {width}x{height}",
    }


# web_search: real internet search, added 2026-10-08 (Devin: "this web
# chat needs to also be able to search online", explicit ask to compete
# feature-for-feature with Grok/Claude's web search for internal Org use).
#
# First attempt (plain urllib + regex against html.duckduckgo.com/html/)
# got real-world bot-detected same day -- a repeat query from this same
# box returned DuckDuckGo's own "Unfortunately, bots use DuckDuckGo too"
# anomaly-challenge page instead of results, not a one-off fluke (their
# CSP/anomaly.js infra is built to catch exactly this shape of plain
# scripted request). Real fix: shell out to `obscura` (Rust headless
# browser engine, github.com/h4ckf0r0day/obscura, binary installed at
# ~/.local/bin/obscura on Leviathan, x86_64-linux-no-render-stealth
# build -- no render target needed for text search, --stealth gives a
# consistent real-browser fingerprint that gets past the same anomaly
# check a bare urllib request trips). `obscura fetch --dump text` drives
# real JS rendering, confirmed live 2026-10-08 against two real queries.
#
# No new Python dependency (still stdlib-only, matches this project's
# own no-new-deps doctrine) -- the new dependency is the obscura binary
# itself on Leviathan's disk, not a pip package.
#
# Known risk, accepted explicitly by Devin same day: search queries
# leave Leviathan and are visible in DuckDuckGo's own logs -- a real,
# new egress path the other two (local-disk-only) tools don't have.
# Mitigated, not eliminated: every query is logged to the SAME
# sensitivity-read-ledger.csv the other tools use (detection, not
# prevention), and SYSTEM_PROMPT in server.py carries explicit guidance
# not to paste proprietary identifiers/figures into a search query.
OBSCURA_BIN = Path.home() / ".local" / "bin" / "obscura"


def _parse_ddg_text(raw: str) -> list:
    """Parses obscura's --dump text output for html.duckduckgo.com/html/.
    Real shape, confirmed live 2026-10-08: a region/time-filter preamble
    line ending in '...Vietnam (en)', then an 'Any Time ... Past Year'
    line, then repeating (title, blank, url+snippet) blocks separated by
    blank lines. Not a stable public API -- this parser can break if
    DuckDuckGo changes their HTML layout; the {note: ...} empty-result
    path below surfaces that instead of silently returning nothing."""
    lines = raw.split("\n")
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("Any Time"):
            start = i + 1
            break
    body = "\n".join(lines[start:]).strip()
    blocks = [b.strip() for b in re.split(r"\n\s*\n\s*\n+", body) if b.strip()]

    results = []
    i = 0
    while i + 1 < len(blocks):
        title = blocks[i]
        rest = blocks[i + 1]
        parts = rest.split("\n", 1)
        url_line = parts[0].strip()
        snippet = parts[1].strip() if len(parts) > 1 else ""
        # Real result blocks are (title, url+snippet) pairs; stray nav
        # blocks (the preamble itself, pagination links) don't look like
        # a bare domain/URL on their own line -- skip past those instead
        # of misreading them as a result.
        if "." in url_line and " " not in url_line:
            results.append({"title": title, "url": url_line, "snippet": snippet})
            i += 2
        else:
            i += 1
    return results


def web_search(model: str, query: str, max_results: int = 5) -> dict:
    """Real web search via DuckDuckGo, fetched through obscura (stealth
    headless browser) to get past DuckDuckGo's bot detection. Returns
    titles, URLs, and snippets -- no page content fetched beyond the
    results page itself. Read-only, no filesystem/repo access at all."""
    max_results = max(1, min(max_results, 10))  # real cap, not unbounded
    if not OBSCURA_BIN.exists():
        return {"error": f"obscura binary not found at {OBSCURA_BIN}"}

    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    try:
        result = subprocess.run(
            [str(OBSCURA_BIN), "fetch", url, "--stealth", "--dump", "text",
             "--quiet", "--timeout", "20"],
            capture_output=True, text=True, timeout=25,
        )
    except subprocess.TimeoutExpired:
        return {"error": "web search timed out"}
    if result.returncode != 0:
        return {"error": f"obscura fetch failed: {result.stderr.strip()[:300]}"}

    results = _parse_ddg_text(result.stdout)[:max_results]
    _log_read(model, "web_search", query, "EXTERNAL")
    if not results:
        return {"results": [], "note": "no results parsed -- query may have returned zero hits, or DuckDuckGo's HTML layout changed"}
    return {"results": results}


# Org/Leviathan instance (levi-webui, forked from webui 2026-09-14):
# get_meter_stats/fossil_log/grep_project deliberately REMOVED from this
# copy's TOOL_SCHEMAS/DISPATCH, not just left unwired -- grep_project's
# real path root is the whole Cairn repo (Assumption default path="."),
# which includes work/ (MILLPOND BEC case material, ledgers, everything).
# Per the 2026-09-14 council (bard+gilfoyle+grok, unanimous): an Org pilot
# user must never get a tool that can read the harness's own repo, full
# stop -- this isn't a per-request classify_path() gate, it's "don't hand
# the tool out at all" on this instance. Functions themselves stay
# defined above (unused, harmless) so this file can still be diffed
# against the original webui/tools.py without drift confusion; only the
# schema/dispatch exposure is cut.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_project_folder",
            "description": "List files/folders in the current chat's linked local folder (only works when the project has one set).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The current project's id"},
                    "path": {"type": "string", "description": "Subdirectory within the linked folder (default: its root)"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_project_file",
            "description": "Read one file's real content from the current chat's linked local folder (text files under 200KB only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The current project's id"},
                    "path": {"type": "string", "description": "File path relative to the linked folder's root"},
                },
                "required": ["project_id", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_project_image",
            "description": "Read one IMAGE file (PNG/JPEG/GIF/WEBP, under 8MB) from the current chat's linked local folder and actually SEE it -- use this instead of read_project_file for images (photos, screenshots, scanned pages saved as image files). Returns an error for non-image files; use read_project_file for those.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The current project's id"},
                    "path": {"type": "string", "description": "Image file path relative to the linked folder's root"},
                },
                "required": ["project_id", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the live internet (via DuckDuckGo) and get back titles, URLs, and snippets. Use for current events, facts outside training data, or anything needing a real up-to-date source.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "max_results": {"type": "integer", "description": "Max results to return, 1-10 (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
]

# get_meter_stats/fossil_log/grep_project deliberately absent -- see the
# TOOL_SCHEMAS comment above, same reasoning. web_search added 2026-10-08,
# see its own docstring/comment above -- internet-only, no repo exposure.
DISPATCH = {
    "list_project_folder": list_project_folder,
    "read_project_file": read_project_file,
    "read_project_image": read_project_image,
    "web_search": web_search,
}

# Tools in this set take a real per-identity namespace (ns) as a required
# kwarg, injected server-side by run_tool below -- NEVER added to
# TOOL_SCHEMAS or accepted from the model's own tool-call arguments. The
# model only ever sees project_id/path; ns comes from run_tool's own ns
# parameter, which server.py's call sites populate from self.ns (already
# verified-identity-derived at request auth time). This is the same
# "don't trust the caller with scoping, the auth layer already knows" bug
# class the 2026-09-14 isolation fix fixed for conversations/uploads --
# see _linked_folder's own 2026-10-08 bugfix comment for the real
# incident this closes.
_NEEDS_NS = {"list_project_folder", "read_project_file", "read_project_image"}


# Sage-MCP wiring (2026-10-20, real pilot users doing real Sage 100
# financial/inventory work, kept getting generic unverified "standard
# Sage 100 architecture" answers instead of grounded ones). Reuses the
# SAME already-deployed, already-tested query module this host runs for
# the Hermes/Cairn-side MCP server (~/sage-mcp/sage_mcp_server.py) rather
# than re-implementing the SQL/safety logic a second time here -- one
# query path, two callers.
#
# ACCESS CONTROL: Devin explicitly wants to gatekeep this himself, and
# expects the allowed-user count to grow past the initial few -- so this
# is a plain external JSON file he edits directly (no code change, no
# restart needed, re-read fresh on every single tool-schema build and
# every dispatch), NOT a hardcoded set in this file. Fails CLOSED: a
# missing/corrupt/unreadable allowlist file means nobody gets Sage access,
# never "open it up by accident".
import importlib.util as _ilu

SAGE_MCP_DIR = Path("/home/ai/sage-mcp")
SAGE_ALLOWLIST_PATH = SAGE_MCP_DIR / "sage_mcp_allowlist.json"
SAGE_QUERY_LOG = SAGE_MCP_DIR / "sage_query_log.csv"

_sage_spec = _ilu.spec_from_file_location("sage_mcp_server", SAGE_MCP_DIR / "sage_mcp_server.py")
_sage_mod = _ilu.module_from_spec(_sage_spec)
_sage_spec.loader.exec_module(_sage_mod)


def _sage_allowed(ns: str | None) -> bool:
    if not ns:
        return False
    try:
        with open(SAGE_ALLOWLIST_PATH) as f:
            data = json.load(f)
        return ns in set(data.get("allowed_namespaces", []))
    except Exception:
        return False


def _log_sage_query(model: str, ns: str | None, tool: str, detail: str, result: str = ""):
    """2026-10-21 (Devin: weekly log-review habit): logs the real outcome
    too, not just what was asked. Before this, the CSV only ever recorded
    the query text BEFORE it ran, so a weekly read of the log could see
    WHAT was asked but never whether it actually worked -- useless for
    spotting a wrong-guess pattern. Rows written before this date have
    only 5 columns; a reader has to handle both lengths."""
    SAGE_QUERY_LOG.parent.mkdir(parents=True, exist_ok=True)
    outcome = "error" if str(result).startswith("Error:") else "ok"
    err_snippet = str(result)[:200] if outcome == "error" else ""
    with open(SAGE_QUERY_LOG, "a", newline="") as f:
        csv.writer(f).writerow([date.today().isoformat(), model, ns or "", tool, detail[:300], outcome, err_snippet])


def sage_list_databases(model: str, ns: str | None = None) -> dict:
    result = _sage_mod.sage_list_databases()
    _log_sage_query(model, ns, "sage_list_databases", "", result)
    return {"result": result}


def sage_list_tables(model: str, ns: str | None = None, database: str = "", pattern: str | None = None) -> dict:
    result = _sage_mod.sage_list_tables(database, pattern)
    _log_sage_query(model, ns, "sage_list_tables", f"{database} {pattern or ''}", result)
    return {"result": result}


MAX_DESCRIBE_TABLES = 8
MAX_SHARED_COLUMNS = 40


def _columns_from_result(text: str) -> list:
    """Column names in one sage_describe_table result; [] if the text can't be parsed."""
    try:
        rows = json.loads(text[text.index("["):text.rindex("]") + 1])
        return [r["COLUMN_NAME"] for r in rows if isinstance(r, dict) and "COLUMN_NAME" in r]
    except (ValueError, KeyError, TypeError):
        return []


def sage_describe_table(model: str, ns: str | None = None, database: str = "", table: str = "",
                        tables: list | None = None) -> dict:
    names = []
    raw = ([table] if table else []) + (list(tables) if isinstance(tables, (list, tuple)) else
                                        ([tables] if tables else []))
    for item in raw:
        for piece in str(item).split(","):
            piece = piece.strip()
            if piece and piece not in names:
                names.append(piece)
    dropped = names[MAX_DESCRIBE_TABLES:]
    names = names[:MAX_DESCRIBE_TABLES]
    if len(names) <= 1:
        result = _sage_mod.sage_describe_table(database, names[0] if names else "")
    else:
        parts, cols, failed = [], {}, []
        for t in names:
            r = str(_sage_mod.sage_describe_table(database, t))
            if r.startswith("Error:") or r.startswith("No columns found"):
                failed.append(f"- {t}: {r[:200]}")
                continue
            parts.append(r)
            for c in _columns_from_result(r):
                cols.setdefault(c, []).append(t)
        if not parts:
            result = "Error: none of the requested tables could be described.\n" + "\n".join(failed)
        else:
            shared = sorted((c, ts) for c, ts in cols.items() if len(ts) >= 2)
            extra = ""
            if shared:
                extra += ("\n\nCOLUMNS SHARED BY 2 OR MORE OF THESE TABLES (candidate join keys; "
                          "confirm the meaning before joining):\n"
                          + "\n".join(f"- {c}: {', '.join(ts)}" for c, ts in shared[:MAX_SHARED_COLUMNS]))
            if failed:
                extra += "\n\nNOT DESCRIBED:\n" + "\n".join(failed)
            if dropped:
                extra += (f"\n\nNOT DESCRIBED (limit {MAX_DESCRIBE_TABLES} per call): "
                          + ", ".join(dropped))
            result = "\n\n".join(parts) + extra
    _log_sage_query(model, ns, "sage_describe_table", f"{database}.{','.join(names)}", result)
    return {"result": result}


def sage_search_columns(model: str, ns: str | None = None, database: str = "", pattern: str = "") -> dict:
    result = _sage_mod.sage_search_columns(database, pattern)
    _log_sage_query(model, ns, "sage_search_columns", f"{database} {pattern}", result)
    return {"result": result}


# Real incident 2026-10-27: traced every real Sage failure in the
# query log and found the actual pattern -- it is almost never "described
# a table, then guessed a wrong column on it." It is "queried a table that
# was NEVER described (or even listed) this turn at all" -- either a real
# table assumed to share another described table's columns
# (AR_InvoiceHistoryHeader queried for JobNo right after describing
# JC_Job/JC_JobHistoryCostCodeDtl instead), or an invented table name that
# never appeared in any sage_list_tables/sage_describe_table call at all
# (GLJobHdr, PMJob -- plausible-looking Sage-100-module-prefixed names,
# pure guesses). Council (Grok, 2026-10-27, same incident): "Bind the
# query tool to the last successful describe-table result in that
# session. Reject any table not in that set before execution." This is
# that gate -- table-level only (column-level SQL parsing is far more
# fragile to get right via regex and the table-level gate already covers
# the dominant real failure shape found in the actual logs).
_SQL_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?:\[?\w+\]?\.)*\[?(\w+)\]?", re.IGNORECASE)
# Standard SQL Server catalog views -- always real, never Sage-specific,
# exempt from the describe-first requirement (a model correctly using
# sys.tables to self-discover schema, as seen live in this same incident,
# should not be blocked by this gate).
_SQL_CATALOG_EXEMPT = {"tables", "columns", "objects", "schemas", "views"}


def _referenced_tables(sql: str) -> set:
    return {m.lower() for m in _SQL_TABLE_REF_RE.findall(sql or "")
            if m.lower() not in _SQL_CATALOG_EXEMPT}


def _tables_described_this_turn(tool_calls_made: list) -> set:
    """Every table name sage_describe_table was actually asked about this
    turn (success or failure both count as 'looked up' -- a failed
    describe on a nonexistent table is a different, already-handled
    failure mode than never looking at all)."""
    described = set()
    for call in tool_calls_made or []:
        if call.get("name") != "sage_describe_table":
            continue
        args = call.get("args") or {}
        raw = ([args["table"]] if args.get("table") else []) + (
            list(args["tables"]) if isinstance(args.get("tables"), (list, tuple))
            else ([args["tables"]] if args.get("tables") else []))
        for item in raw:
            for piece in str(item).split(","):
                piece = piece.strip().lower()
                if piece:
                    described.add(piece)
    return described


def sage_run_query(model: str, ns: str | None = None, database: str = "", sql: str = "",
                    max_rows: int = 100, tool_calls_made: list | None = None) -> dict:
    referenced = _referenced_tables(sql)
    described = _tables_described_this_turn(tool_calls_made or [])
    missing = sorted(t for t in referenced if t not in described)
    if missing:
        result = (
            "Error: query references table(s) never described in this "
            "session: " + ", ".join(missing) + ". Call sage_describe_table "
            "on each one first to get its real column names -- do not "
            "guess columns for a table you have not actually looked at, "
            "and do not assume a table shares another table's columns."
        )
    else:
        result = _sage_mod.sage_run_query(database, sql, max_rows)
    _log_sage_query(model, ns, "sage_run_query", f"{database}: {sql}", result)
    return {"result": result}


# "(Org runs Sage 100 Premium 2025)" in every description, per Devin
# 2026-10-20 -- grounds the model's answers in the real product version
# instead of generic "standard Sage 100" guesses that may not match this
# specific edition's schema/feature set.
SAGE_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "sage_list_databases",
        "description": "List Sage 100 Premium 2025 / DocLink company databases on DB02 that are actually readable (verified read access, not just visible). MAS_MNS is the main company (the company); MAS_MCO is a related company; other MAS_* databases are not mainstream.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "sage_list_tables",
        "description": "List tables in a Sage 100 Premium 2025 company database, optionally filtered by a name substring.",
        "parameters": {"type": "object", "properties": {
            "database": {"type": "string", "description": "Database name, e.g. MAS_MNS or MAS_MCO."},
            "pattern": {"type": "string", "description": "Optional substring to filter table names."}},
            "required": ["database"]},
    }},
    {"type": "function", "function": {
        "name": "sage_describe_table",
        "description": "Get column names/types/nullability for one or several tables in Sage 100 Premium 2025 in a single call (pass up to 8 names in `tables`). For several tables it also lists the columns they share, the likely join keys. Use after sage_list_tables to confirm the exact names.",
        "parameters": {"type": "object", "properties": {
            "database": {"type": "string", "description": "Database name."},
            "table": {"type": "string", "description": "Exact table name (use `tables` for several)."},
            "tables": {"type": "array", "items": {"type": "string"},
                       "description": "Up to 8 exact table names to describe at once."}},
            "required": ["database"]},
    }},
    {"type": "function", "function": {
        "name": "sage_search_columns",
        "description": "Find which tables in a Sage 100 Premium 2025 database have a column matching a name pattern.",
        "parameters": {"type": "object", "properties": {
            "database": {"type": "string", "description": "Database name."},
            "pattern": {"type": "string", "description": "Substring to match against column names."}},
            "required": ["database", "pattern"]},
    }},
    {"type": "function", "function": {
        "name": "sage_run_query",
        "description": "Run a read-only SELECT query against a Sage 100 Premium 2025 database. Only SELECT/WITH statements are allowed; write/DDL keywords are rejected before the query runs. Results are row-capped.",
        "parameters": {"type": "object", "properties": {
            "database": {"type": "string", "description": "Database name."},
            "sql": {"type": "string", "description": "A single SELECT (or WITH ... SELECT) statement."},
            "max_rows": {"type": "integer", "description": "Row cap, 1-500 (default 100)."}},
            "required": ["database", "sql"]},
    }},
]

SAGE_DISPATCH = {
    "sage_list_databases": sage_list_databases,
    "sage_list_tables": sage_list_tables,
    "sage_describe_table": sage_describe_table,
    "sage_search_columns": sage_search_columns,
    "sage_run_query": sage_run_query,
}
_SAGE_TOOL_NAMES = set(SAGE_DISPATCH.keys())


# meg-files-mcp wiring (2026-10-25): read-only Jobs / Sales files and DocLink (doclink2 + IMSTORE).
# Three independent gates live in ~/meg-files-mcp/allowlist.json ("jobs", "sales", "doclink"),
# re-read on every call and enforced inside the module as well as when schemas are built.
# A module that fails to load turns these tools OFF; it never blocks the webchat starting.
try:
    _mf_spec = _ilu.spec_from_file_location("meg_files_mcp", "/home/ai/meg-files-mcp/meg_files_mcp.py")
    _megfiles = _ilu.module_from_spec(_mf_spec)
    _mf_spec.loader.exec_module(_megfiles)
except Exception as _e:  # noqa: BLE001
    print(f"[meg-files] disabled: {type(_e).__name__}: {_e}")
    _megfiles = None


def set_document_extractor(fn) -> None:
    if _megfiles is not None:
        _megfiles.set_extractor(fn)


# Wrike-MCP wiring (2026-10-21, Engineering "drowning in work" in
# Wrike, real volume confirmed -- 1000+ tasks hit the API's page cap on the
# first unpaginated query). Same reuse pattern as Sage-MCP above: ONE query
# module (~/wrike-mcp/wrike_mcp_server.py), two callers (Hermes/Cairn via
# the fossil-tracked canonical copy, this webchat via this bridge).
#
# ACCESS CONTROL: same shape as Sage -- plain external JSON file Devin edits
# directly, re-read fresh every call, fails CLOSED on any read/parse error.
# Deliberately READ-ONLY (Devin's explicit call, 2026-10-21): no write/POST/
# PUT/DELETE tool exists here at all, even though the underlying personal
# token could technically write wherever Devin's own Wrike permissions
# allow -- write capability is a separate future decision, not something
# reachable by accident.
WRIKE_MCP_DIR = Path("/home/ai/wrike-mcp")
WRIKE_ALLOWLIST_PATH = WRIKE_MCP_DIR / "wrike_mcp_allowlist.json"
WRIKE_QUERY_LOG = WRIKE_MCP_DIR / "wrike_query_log.csv"

_wrike_spec = _ilu.spec_from_file_location("wrike_mcp_server", WRIKE_MCP_DIR / "wrike_mcp_server.py")
_wrike_mod = _ilu.module_from_spec(_wrike_spec)
_wrike_spec.loader.exec_module(_wrike_mod)


def _wrike_allowed(ns: str | None) -> bool:
    if not ns:
        return False
    try:
        with open(WRIKE_ALLOWLIST_PATH) as f:
            data = json.load(f)
        return ns in set(data.get("allowed_namespaces", []))
    except Exception:
        return False


def _log_wrike_query(model: str, ns: str | None, tool: str, detail: str, result: str = ""):
    """Outcome-aware from day one (2026-10-21 lesson, same day as the Sage
    log's own outcome-tracking retrofit) -- never ship a query log that can
    only show what was ASKED and not whether it actually worked."""
    WRIKE_QUERY_LOG.parent.mkdir(parents=True, exist_ok=True)
    outcome = "error" if str(result).startswith("Error:") else "ok"
    err_snippet = str(result)[:200] if outcome == "error" else ""
    with open(WRIKE_QUERY_LOG, "a", newline="") as f:
        csv.writer(f).writerow([date.today().isoformat(), model, ns or "", tool, detail[:300], outcome, err_snippet])


def wrike_list_spaces(model: str, ns: str | None = None) -> dict:
    result = _wrike_mod.wrike_list_spaces()
    _log_wrike_query(model, ns, "wrike_list_spaces", "", result)
    return {"result": result}


def wrike_list_folders(model: str, ns: str | None = None, space: str = "", pattern: str | None = None) -> dict:
    result = _wrike_mod.wrike_list_folders(space, pattern)
    _log_wrike_query(model, ns, "wrike_list_folders", f"{space} {pattern or ''}", result)
    return {"result": result}


def wrike_search_tasks(model: str, ns: str | None = None, space: str = "", query: str | None = None,
                        status: str | None = None, limit: int = 50) -> dict:
    result = _wrike_mod.wrike_search_tasks(space, query, status, limit)
    _log_wrike_query(model, ns, "wrike_search_tasks", f"{space}: q={query!r} status={status!r}", result)
    return {"result": result}


def wrike_get_task(model: str, ns: str | None = None, task_id: str = "") -> dict:
    result = _wrike_mod.wrike_get_task(task_id)
    _log_wrike_query(model, ns, "wrike_get_task", task_id, result)
    return {"result": result}


def wrike_list_attachments(model: str, ns: str | None = None, task_id: str = "") -> dict:
    result = _wrike_mod.wrike_list_attachments(task_id)
    _log_wrike_query(model, ns, "wrike_list_attachments", task_id, result)
    return {"result": result}


def wrike_recent_activity(model: str, ns: str | None = None, space: str = "", limit: int = 20) -> dict:
    result = _wrike_mod.wrike_recent_activity(space, limit)
    _log_wrike_query(model, ns, "wrike_recent_activity", space, result)
    return {"result": result}


# Real file retrieval (2026-10-21, Devin: "i want to be able to retrieve
# drawings" -- explicitly the real original file, not an image
# conversion). The webui has no existing route that serves raw bytes
# back to a browser (the /api/projects/.../file routes all return JSON-
# wrapped base64 for the model, not a clickable download) -- this adds
# one: a real file lands in DOWNLOADS_DIR under the REQUESTING user's
# own namespace + a random token, and the model hands back a real
# clickable link. server.py's new /downloads/<ns>/<token>/<filename>
# route (added alongside this) checks the request's own verified
# identity against <ns> before ever serving a byte -- same Cloudflare
# Access identity this whole webui already trusts, not a new mechanism.
DOWNLOADS_DIR = Path("/home/ai/levi-webui/downloads")
DOWNLOADS_PUBLIC_BASE = "https://leviathan.example.com"  # real
# Cloudflare Tunnel hostname for this box, confirmed live 2026-10-21
# (~/.cloudflared config: tunnel 81e98aa1-... -> 127.0.0.1:8420).


def wrike_download_attachment(model: str, ns: str | None = None, task_id: str = "", attachment_name: str | None = None) -> dict:
    if not ns:
        result = "Error: wrike_download_attachment requires a real identity namespace, none provided"
        _log_wrike_query(model, ns, "wrike_download_attachment", task_id, result)
        return {"result": result}
    raw_result = _wrike_mod.wrike_download_attachment(task_id, attachment_name)
    if "error" in raw_result:
        result = f"Error: {raw_result['error']}"
        _log_wrike_query(model, ns, "wrike_download_attachment", task_id, result)
        return {"result": result}
    token = secrets.token_urlsafe(16)
    dest_dir = DOWNLOADS_DIR / ns / token
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = raw_result["filename"]
    dest_path = dest_dir / filename
    dest_path.write_bytes(base64.b64decode(raw_result["data_b64"]))
    url = f"{DOWNLOADS_PUBLIC_BASE}/downloads/{ns}/{token}/{urllib.parse.quote(filename)}"
    result = (f"Real file ready: {filename} ({raw_result['size']:,} bytes, "
              f"{raw_result['content_type']}). Download link (expires in 48h, "
              f"only works for your own account): {url}")
    _log_wrike_query(model, ns, "wrike_download_attachment", f"{task_id}: {filename}", result)
    return {"result": result}


# Reused directly from the canonical module (not hand-duplicated like
# SAGE_TOOL_SCHEMAS above) -- no Org-specific framing divergence needed yet,
# so one real source of truth beats a second copy that can drift.
WRIKE_TOOL_SCHEMAS = [
    {"type": "function", "function": t} for t in _wrike_mod.TOOLS
]

WRIKE_DISPATCH = {
    "wrike_list_spaces": wrike_list_spaces,
    "wrike_list_folders": wrike_list_folders,
    "wrike_search_tasks": wrike_search_tasks,
    "wrike_get_task": wrike_get_task,
    "wrike_list_attachments": wrike_list_attachments,
    "wrike_recent_activity": wrike_recent_activity,
    "wrike_download_attachment": wrike_download_attachment,
}
_WRIKE_TOOL_NAMES = set(WRIKE_DISPATCH.keys())


# Office document generation (2026-10-29, Devin: "the same way that you
# help me" -- let the model build real .xlsx/.docx/.pptx files for a user
# mid-chat, not just read uploaded ones). Gated the same independent-
# allowlist way as Sage/Wrike above: own allowlist file, own _*_allowed
# helper, re-checked in BOTH get_tool_schemas and run_tool. Security-review
# findings baked in directly (not left as prompt-level hints the model
# could ignore):
#   - hard size caps enforced here, in code -- reject with an error
#     string, never silently truncate (truncation reads as success)
#   - every string cell/value that could start with =+-@ gets a leading
#     apostrophe forced on write -- otherwise Excel/LibreOffice treats it
#     as a live formula (classic DDE-payload vector), something a plain
#     text download could never carry
#   - filenames are ALWAYS server-slugified from the model's title, never
#     the model's raw string used as-is -- mirrors the read-side path-
#     escape guard in server.py's /downloads/ handler, applied here on
#     the write side instead
#   - output extension is chosen by the tool function itself, never by
#     the model -- only .xlsx/.docx/.pptx are ever written (no macro-
#     capable .xlsm/.docm/.pptm path exists to add by accident)
OFFICE_DOCS_DIR = Path("/home/ai/levi-webui")
OFFICE_DOCS_ALLOWLIST_PATH = OFFICE_DOCS_DIR / "office_docs_allowlist.json"

_OFFICE_CAPS = {
    "spreadsheet": {"max_sheets": 10, "max_cols": 40, "max_rows": 500, "max_cell": 500},
    "document": {"max_sections": 30, "max_heading": 150, "max_body": 3000},
    "presentation": {"max_slides": 40, "max_bullets": 10, "max_title": 120, "max_bullet": 200},
}


def _office_docs_allowed(ns: str | None) -> bool:
    if not ns:
        return False
    try:
        with open(OFFICE_DOCS_ALLOWLIST_PATH) as f:
            data = json.load(f)
        return ns in set(data.get("allowed_namespaces", []))
    except Exception:
        return False


def _slugify_filename(title: str, fallback: str, ext: str) -> str:
    """Server decides the real filename -- the model's title is only ever
    a hint. Strips to [A-Za-z0-9-_], collapses repeats, caps length, and
    NEVER trusts the model's string as a literal path component (same
    reasoning as the /downloads/ read-side escape guard, applied here on
    the write side)."""
    base = re.sub(r"[^A-Za-z0-9-_]+", "-", (title or "").strip()).strip("-")
    base = re.sub(r"-{2,}", "-", base)[:60] or fallback
    return f"{base}.{ext}"


def _sanitize_cell(v) -> str:
    """Numbers pass through as numbers (openpyxl writes them as real
    numeric cells); every string gets a leading apostrophe forced on if
    it starts with a character Excel/LibreOffice would otherwise parse
    as a formula (=+-@) or that can be used for a tab/CR injection trick
    -- turns a live-formula risk into plain literal text, every time,
    regardless of where the string originated (model's own words, or
    content laundered through an earlier tool result)."""
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _numeric_or_text(v):
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        try:
            return float(s) if "." in s else int(s)
        except ValueError:
            return _sanitize_cell(v)
    return s


def _write_download(ns: str, filename: str, data: bytes) -> str:
    token = secrets.token_urlsafe(16)
    dest_dir = DOWNLOADS_DIR / ns / token
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / filename).write_bytes(data)
    return f"{DOWNLOADS_PUBLIC_BASE}/downloads/{ns}/{token}/{urllib.parse.quote(filename)}"


def _office_disk_quota_ok(ns: str, incoming_bytes: int, cap_bytes: int = 200 * 1024 * 1024) -> bool:
    """Per-ns disk quota at write time (security review finding: the
    existing 48h sweep was sized for the extraction path's low,
    opportunistic file count, not a generator that can fire every turn).
    Sums real bytes already under this user's own downloads dir."""
    user_dir = DOWNLOADS_DIR / ns
    if not user_dir.exists():
        return True
    total = sum(f.stat().st_size for f in user_dir.rglob("*") if f.is_file())
    return (total + incoming_bytes) <= cap_bytes


def _log_office_doc(model: str, ns: str | None, tool: str, detail: str, result: str = ""):
    log_path = OFFICE_DOCS_DIR / "office_docs_log.csv"
    outcome = "error" if str(result).startswith("Error:") else "ok"
    with open(log_path, "a", newline="") as f:
        csv.writer(f).writerow([date.today().isoformat(), model, ns or "", tool, detail[:300], outcome])


def create_spreadsheet(model: str, ns: str | None = None, filename: str = "", sheets: list | None = None) -> dict:
    if not ns:
        return {"result": "Error: create_spreadsheet requires a real identity namespace, none provided"}
    sheets = sheets or []
    caps = _OFFICE_CAPS["spreadsheet"]
    if not sheets:
        return {"result": "Error: at least one sheet is required"}
    if len(sheets) > caps["max_sheets"]:
        return {"result": f"Error: {len(sheets)} sheets exceeds the max of {caps['max_sheets']} -- split into multiple calls"}
    for sh in sheets:
        headers = sh.get("headers") or []
        rows = sh.get("rows") or []
        if len(headers) > caps["max_cols"]:
            return {"result": f"Error: sheet {sh.get('name', '?')!r} has {len(headers)} columns, exceeds max {caps['max_cols']}"}
        if len(rows) > caps["max_rows"]:
            return {"result": f"Error: sheet {sh.get('name', '?')!r} has {len(rows)} rows, exceeds max {caps['max_rows']}"}
        for row in rows:
            for cell in row:
                if len(str(cell)) > caps["max_cell"]:
                    return {"result": f"Error: a cell in sheet {sh.get('name', '?')!r} exceeds {caps['max_cell']} chars"}

    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for sh in sheets:
        name = re.sub(r"[\\/*?:\[\]]", "-", str(sh.get("name") or "Sheet"))[:31] or "Sheet"
        ws = wb.create_sheet(name)
        headers = sh.get("headers") or []
        if headers:
            ws.append([_sanitize_cell(h) for h in headers])
        for row in sh.get("rows") or []:
            ws.append([_numeric_or_text(c) for c in row])

    import io
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    if not _office_disk_quota_ok(ns, len(data)):
        return {"result": "Error: you're at your download storage quota (200MB) -- clear old downloads or ask an admin"}
    fname = _slugify_filename(filename or sheets[0].get("name", ""), "spreadsheet", "xlsx")
    url = _write_download(ns, fname, data)
    result = f"Real file ready: {fname} ({len(data):,} bytes, {len(sheets)} sheet(s)). Download link (expires in 48h, only works for your own account): {url}"
    _log_office_doc(model, ns, "create_spreadsheet", fname, result)
    return {"result": result}


def create_document(model: str, ns: str | None = None, title: str = "", sections: list | None = None) -> dict:
    if not ns:
        return {"result": "Error: create_document requires a real identity namespace, none provided"}
    sections = sections or []
    caps = _OFFICE_CAPS["document"]
    if not sections:
        return {"result": "Error: at least one section is required"}
    if len(sections) > caps["max_sections"]:
        return {"result": f"Error: {len(sections)} sections exceeds the max of {caps['max_sections']}"}
    for s in sections:
        if len(str(s.get("heading", ""))) > caps["max_heading"]:
            return {"result": f"Error: a section heading exceeds {caps['max_heading']} chars"}
        if len(str(s.get("body", ""))) > caps["max_body"]:
            return {"result": f"Error: a section body exceeds {caps['max_body']} chars"}

    import docx
    doc = docx.Document()
    if title:
        doc.add_heading(title[:200], level=0)
    for s in sections:
        doc.add_heading(str(s.get("heading", ""))[:caps["max_heading"]], level=1)
        for para in str(s.get("body", ""))[:caps["max_body"]].split("\n\n"):
            if para.strip():
                doc.add_paragraph(para.strip())

    import io
    buf = io.BytesIO()
    doc.save(buf)
    data = buf.getvalue()
    if not _office_disk_quota_ok(ns, len(data)):
        return {"result": "Error: you're at your download storage quota (200MB) -- clear old downloads or ask an admin"}
    fname = _slugify_filename(title, "document", "docx")
    url = _write_download(ns, fname, data)
    result = f"Real file ready: {fname} ({len(data):,} bytes, {len(sections)} section(s)). Download link (expires in 48h, only works for your own account): {url}"
    _log_office_doc(model, ns, "create_document", fname, result)
    return {"result": result}


def create_presentation(model: str, ns: str | None = None, title: str = "", slides: list | None = None) -> dict:
    if not ns:
        return {"result": "Error: create_presentation requires a real identity namespace, none provided"}
    slides = slides or []
    caps = _OFFICE_CAPS["presentation"]
    if not slides:
        return {"result": "Error: at least one slide is required"}
    if len(slides) > caps["max_slides"]:
        return {"result": f"Error: {len(slides)} slides exceeds the max of {caps['max_slides']}"}
    for sl in slides:
        bullets = sl.get("bullets") or []
        if len(bullets) > caps["max_bullets"]:
            return {"result": f"Error: slide {sl.get('title', '?')!r} has {len(bullets)} bullets, exceeds max {caps['max_bullets']}"}
        if len(str(sl.get("title", ""))) > caps["max_title"]:
            return {"result": f"Error: a slide title exceeds {caps['max_title']} chars"}
        for b in bullets:
            if len(str(b)) > caps["max_bullet"]:
                return {"result": f"Error: a bullet in slide {sl.get('title', '?')!r} exceeds {caps['max_bullet']} chars"}

    import pptx
    deck = pptx.Presentation()
    title_layout = deck.slide_layouts[0]
    s0 = deck.slides.add_slide(title_layout)
    s0.shapes.title.text = (title or "Presentation")[:caps["max_title"]]
    bullet_layout = deck.slide_layouts[1]
    for sl in slides:
        slide = deck.slides.add_slide(bullet_layout)
        slide.shapes.title.text = str(sl.get("title", ""))[:caps["max_title"]]
        body = slide.placeholders[1].text_frame
        bullets = sl.get("bullets") or []
        body.text = str(bullets[0])[:caps["max_bullet"]] if bullets else ""
        for b in bullets[1:]:
            p = body.add_paragraph()
            p.text = str(b)[:caps["max_bullet"]]

    import io
    buf = io.BytesIO()
    deck.save(buf)
    data = buf.getvalue()
    if not _office_disk_quota_ok(ns, len(data)):
        return {"result": "Error: you're at your download storage quota (200MB) -- clear old downloads or ask an admin"}
    fname = _slugify_filename(title, "presentation", "pptx")
    url = _write_download(ns, fname, data)
    result = f"Real file ready: {fname} ({len(data):,} bytes, {len(slides)} slide(s)). Download link (expires in 48h, only works for your own account): {url}"
    _log_office_doc(model, ns, "create_presentation", fname, result)
    return {"result": result}


OFFICE_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "create_spreadsheet",
        "description": ("Create a real .xlsx workbook with one or more sheets and give back a "
                         "download link. Each sheet is a header row plus data rows (plain text/"
                         "numbers, no formulas). Max 10 sheets, 40 columns and 500 rows per sheet, "
                         "500 chars per cell -- over the cap is rejected with an error, not silently "
                         "truncated."),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Base filename without extension, e.g. 'Q3-Inventory'. Defaults to the first sheet name if omitted."},
                "sheets": {
                    "type": "array",
                    "description": "1-10 sheets, in the order they should appear in the workbook.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Sheet tab name, max 31 characters (Excel's own limit)."},
                            "headers": {"type": "array", "description": "Column header labels, in order. Max 40 columns.", "items": {"type": "string"}},
                            "rows": {"type": "array", "description": "Data rows, each a flat list of cell values in the same order as headers. Numbers/dates as plain strings, e.g. \"42\" or \"2026-10-29\". Max 500 rows.", "items": {"type": "array", "items": {"type": "string"}}},
                        },
                        "required": ["name", "headers", "rows"],
                    },
                },
            },
            "required": ["sheets"],
        },
    }},
    {"type": "function", "function": {
        "name": "create_document",
        "description": ("Create a real .docx document (a title plus a sequence of heading+body "
                         "sections, like a memo or short report) and give back a download link. "
                         "No tables or images in v1. Max 30 sections, 150 chars per heading, 3000 "
                         "chars per section body -- over the cap is rejected, not truncated."),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Document title, shown at the top of the page and used as the filename. Max 200 characters."},
                "sections": {
                    "type": "array",
                    "description": "1-30 sections, in the order they should appear.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string", "description": "Section heading text, max 150 characters."},
                            "body": {"type": "string", "description": "Section body prose, max 3000 characters. Use a blank line between paragraphs if more than one."},
                        },
                        "required": ["heading", "body"],
                    },
                },
            },
            "required": ["title", "sections"],
        },
    }},
    {"type": "function", "function": {
        "name": "create_presentation",
        "description": ("Create a real .pptx slide deck (title + bullet slides only, no images/"
                         "custom layouts in v1) and give back a download link. Max 40 slides, 10 "
                         "bullets per slide, 120 chars per slide title, 200 chars per bullet -- "
                         "over the cap is rejected, not truncated."),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Deck title, used as the title slide's text and as the filename. Max 120 characters."},
                "slides": {
                    "type": "array",
                    "description": "1-40 content slides after the title slide, in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Slide title, max 120 characters."},
                            "bullets": {"type": "array", "description": "1-10 bullet lines for this slide, max 200 characters each.", "items": {"type": "string"}},
                        },
                        "required": ["title", "bullets"],
                    },
                },
            },
            "required": ["title", "slides"],
        },
    }},
]

OFFICE_DISPATCH = {
    "create_spreadsheet": create_spreadsheet,
    "create_document": create_document,
    "create_presentation": create_presentation,
}
_OFFICE_TOOL_NAMES = set(OFFICE_DISPATCH.keys())


def get_tool_schemas(ns: str | None) -> list:
    """Per-identity tool list -- base TOOL_SCHEMAS for everyone, plus
    Sage-MCP's and/or Wrike-MCP's schemas for ns in their respective
    allowlists (independent gates, a user can be on one, both, or
    neither). Re-reads both allowlist files fresh every call (Devin
    edits them live, no restart)."""
    schemas = TOOL_SCHEMAS
    if _sage_allowed(ns):
        schemas = schemas + SAGE_TOOL_SCHEMAS
    if _wrike_allowed(ns):
        schemas = schemas + WRIKE_TOOL_SCHEMAS
    if _office_docs_allowed(ns):
        schemas = schemas + OFFICE_TOOL_SCHEMAS
    if _megfiles is not None:
        schemas = schemas + _megfiles.schemas_for(ns)
    return schemas


def run_tool(name: str, args: dict, model: str, ns: str | None = None,
             tool_calls_made: list | None = None) -> dict:
    if name in _SAGE_TOOL_NAMES:
        # Gated here too, not just in get_tool_schemas -- never trust
        # that hiding a schema is the only thing stopping a call; a
        # model occasionally emits a tool_call for something not in its
        # own tools list, so the dispatcher re-checks the SAME allowlist.
        if not _sage_allowed(ns):
            return {"error": f"{name} is not available for this account"}
        fn = SAGE_DISPATCH[name]
        try:
            if name == "sage_run_query":
                # Needs this turn's already-described tables for the
                # pre-flight gate above -- the only sage_* tool that does.
                return fn(model=model, ns=ns, tool_calls_made=tool_calls_made, **args)
            return fn(model=model, ns=ns, **args)
        except Exception as e:
            return {"error": f"{name} failed: {e}"}
    if name in _WRIKE_TOOL_NAMES:
        # Same independent re-check discipline as Sage above.
        if not _wrike_allowed(ns):
            return {"error": f"{name} is not available for this account"}
        fn = WRIKE_DISPATCH[name]
        try:
            return fn(model=model, ns=ns, **args)
        except Exception as e:
            return {"error": f"{name} failed: {e}"}
    if name in _OFFICE_TOOL_NAMES:
        # Same independent re-check discipline as Sage/Wrike above --
        # never trust that hiding a schema is the only gate.
        if not _office_docs_allowed(ns):
            return {"error": f"{name} is not available for this account"}
        fn = OFFICE_DISPATCH[name]
        try:
            return fn(model=model, ns=ns, **args)
        except Exception as e:
            return {"error": f"{name} failed: {e}"}
    if _megfiles is not None and name in _megfiles.DISPATCH:
        # The module re-checks its own allowlist per call (fails closed); nothing is trusted
        # just because a schema was or was not offered.
        try:
            return _megfiles.DISPATCH[name](model=model, ns=ns, **args)
        except Exception as e:
            return {"error": f"{name} failed: {e}"}
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        if name in _NEEDS_NS:
            if not ns:
                return {"error": f"{name} requires a real identity namespace, none provided"}
            return fn(model=model, ns=ns, **args)
        return fn(model=model, **args)
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


# Mechanical post-answer verification (2026-10-20, same real incident as
# the sage_list_tables aggregation fix above). The data-shape fix stops
# the WORST case (a 1,324-row dump inviting free synthesis), but a model
# can still name a specific table/column in ANY sentence, not just a
# summary -- this is a second, independent layer that checks whatever
# the model actually said against whatever a sage_* tool actually
# returned THIS turn. Deliberately NOT another LLM call judging the
# first one (that's the same unreliability one level up) -- plain
# regex extraction + set membership against real cached tool output.
#
# Pattern matches Sage's real naming convention (UPPERCASE module
# prefix, 2-6 letters, optional digits, underscore, then the rest --
# e.g. GL_Account, AP_ACHCheckEntry, MAS_MNS) and deliberately excludes
# ordinary lowercase English words-with-underscores, which this
# convention never produces.
# Broadened 2026-10-27 (real incident: a real session invented
# table names "GLJobHdr" and "PMJob", never seen in any sage_list_tables
# output that session -- the old underscore-only form never saw them,
# they're structurally invisible to it). Sage 100's real convention is
# a 2-6 char module prefix (AR_, JC_, GL_, ...) but the model sometimes
# guesses a no-underscore CamelCase variant of the same shape instead.
# Known tradeoff, not claimed complete: the no-underscore branch can
# false-positive on an ordinary CamelCase-shaped word (rare in practice,
# and only evaluated in Sage-tooled turns) -- costs one wasted retry
# round at worst, bounded by the same retry cap every other gate here
# uses, never a silent miss the way the narrower regex was.
_SAGE_IDENTIFIER_RE = re.compile(
    r"\b([A-Z]{2,6}[0-9]{0,3}_[A-Za-z][A-Za-z0-9_]*"
    r"|[A-Z]{2,6}[0-9]{0,3}[A-Z][a-z]{2,}(?:[A-Z][a-z0-9]{2,})*)\b"
)


def collect_real_sage_identifiers(tool_calls_made: list) -> set:
    """Every real table/column/database-shaped identifier a sage_* tool
    actually returned this turn -- the ground truth for verify_sage_claims
    below. Reads tool_calls_made's own \"output\"[\"result\"] text, the
    same structure run_tool's sage_* wrappers produce."""
    real = set()
    for call in tool_calls_made or []:
        if not str(call.get("name", "")).startswith("sage_"):
            continue
        output = call.get("output") or {}
        text = output.get("result", "") if isinstance(output, dict) else ""
        real.update(_SAGE_IDENTIFIER_RE.findall(str(text)))
    return real


# Real incident 2026-10-27: a runaway-generation guard correctly fired
# and told the user the truth ("[Reply stopped: the model started
# rambling or repeating itself...]"), sitting right there in the
# model's OWN prior turn. The user then asked "why did you break?"
# three separate times, and three separate times the model ignored its
# own real prior statement and fabricated a different, increasingly
# specific, FALSE technical explanation instead -- one of them an
# outright false identity claim ("I am a large language model trained
# by Google", for an open-weights model running locally). Same bug
# class as the Sage identifier hallucination: a real fact already
# existed, and free generation ignored it under self-referential
# pressure. Same fix shape: extract the real fact mechanically, inject
# it as ground truth the model must use, don't trust a prompt
# instruction alone to make it look (that already failed once, see
# verify_sage_claims's own history).
_SELF_REF_QUESTION_RE = re.compile(
    r"why (did|do|does|would) (you|it|that) (break|stop|cut off|fail|"
    r"error|glitch)|what (happened|went wrong)|what (model|llm|ai) are "
    r"you|who (made|built|trained|created) you|what company (are you "
    r"from|made you|built you)", re.I)

# The exact harness-injected marker strings that can appear in an
# assistant turn -- if any of these are present in the LAST assistant
# message in history, that turn was HARNESS-STATED fact, not the
# model's own free generation, and is safe to quote back as ground
# truth. Literal substrings from server.py's own constants, not
# re-derived/guessed here (grep server.py for _RUNAWAY_NOTICE,
# "no response for that message", "stopped after repeated tool" if
# these ever drift -- this list has to stay in sync with the real
# strings, it does not infer them).
_HARNESS_DISRUPTION_MARKERS = (
    "the model started rambling or repeating itself",
    "no response came back for that message",
    "stopped after repeated tool calls",
    "the model produced no response for this",
)


def last_harness_disruption_notice(history_messages: list) -> tuple[str | None, int]:
    """Returns (real disruption text or None, how many assistant turns
    back it was -- 0 = the most recent one). Scans the FULL history, not
    just the last assistant turn.

    Broadened 2026-10-27 after a real gap found in further testing: the
    original narrower version (stop at the first/most-recent assistant
    message) correctly declined to re-surface an old, already-superseded
    disruption as if it just happened -- but the actual consequence was
    worse than that caution bought: asked "why did you break?" about a
    disruption from a few turns back, the model had nothing to ground on
    at all and fabricated a brand-new FALSE technical explanation
    (invented WebSocket/schema-validation claims that time, no real
    Google-identity claim, same underlying confident-fabrication bug).
    Any real disruption anywhere in history is better grounding than
    none -- server.py's caller uses the turn-distance to phrase it
    correctly ("a few turns back" vs "just now") rather than implying
    false recency."""
    assistant_idx = 0
    for msg in reversed(history_messages or []):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(str(x) for x in content)
        content = str(content or "")
        for marker in _HARNESS_DISRUPTION_MARKERS:
            if marker in content:
                return content, assistant_idx
        assistant_idx += 1
    return None, -1


# Real incident 2026-10-27 (same day, found by Devin live): the
# standing model-identity fact injected into the system prompt every
# turn (server.py's _build_system_prompt) REDUCES wrong identity claims
# but does not eliminate them -- reproduced directly: same real fact
# given every time, same question, 1 of 4 real generations still said
# "Qwen 2.5 Coder" (wrong family/version) instead of the real
# qwen3.6:27b, while 3 of 4 got it right. A standing prompt fact is
# probabilistic, not a guarantee -- the model can still override it with
# its own training-prior guess under self-referential pressure, same
# lesson the Sage identifier gate already proved for database names.
# Same fix shape: don't trust generation to get a stated fact right,
# mechanically check the OUTPUT against the real fact and reject/retry
# if it's wrong, the same discipline verify_sage_claims already applies
# to database identifiers, now applied to the model's own identity.
_WRONG_VENDOR_RE = re.compile(
    r"\b(OpenAI|GPT-?\d|ChatGPT|Anthropic|Claude|Google|"
    r"Gemini|PaLM|Meta'?s?\s+Llama|Llama\s?\d|Mistral(?:\s?AI)?|DeepSeek|xAI|Grok)\b",
    re.I)
# Two deliberately NARROW extraction shapes, not a broad "self-reference
# phrase somewhere near a vendor word" proximity check -- that looser
# version (tried first) false-positived on "I'm happy to help, and I run
# the Google search tool" (same sentence, "I'm" + "Google" both present,
# but not a self-identity claim at all). Real incidents both match one
# of these two specific, well-anchored shapes instead:
_MODEL_LABEL_VALUE_RE = re.compile(
    r"\*{0,2}(?:underlying |base )?model\*{0,2}:?\s*\*{0,2}\s*([A-Za-z0-9][\w .:/\-]{0,40})",
    re.I)
_DIRECT_VENDOR_CLAIM_RE = re.compile(
    r"\b(?:trained|built|created|made|developed|hosted)\s+by\s+([A-Za-z][\w .&\-]{0,30})|"
    r"\b(?:running|runs)\s+on\s+([A-Za-z][\w .&\-]{0,30})", re.I)


def _identity_mismatch_in_snippet(snippet: str, real_model: str) -> str | None:
    if not snippet:
        return None
    m = _WRONG_VENDOR_RE.search(snippet)
    if m:
        return f"claims vendor/family {m.group(0)!r}, the real model is {real_model!r}"
    if re.search(r"\bqwen\s*[\d.]+", snippet, re.I) and real_model.lower() not in snippet.lower():
        return f"claims a Qwen version that does not match the real model {real_model!r}"
    return None


def model_identity_mismatch(content: str, real_model: str) -> str | None:
    """None if no real mismatch; otherwise a short description of what
    was wrongly claimed. Two failure shapes, both seen live: (1) claims
    a totally different vendor as its own identity (real incident:
    'trained by Google'), (2) claims the right family but a specific
    wrong version (real incident: 'Qwen 2.5 Coder' when the real model
    is qwen3.6:27b). Only checks text captured by the two narrow shapes
    above, never a loose "mentions a vendor word somewhere in this
    message" scan -- that's what caused the false positives this went
    through three iterations to kill."""
    if not content or not real_model:
        return None
    for m in _MODEL_LABEL_VALUE_RE.finditer(content):
        result = _identity_mismatch_in_snippet(m.group(1), real_model)
        if result:
            return result
    for m in _DIRECT_VENDOR_CLAIM_RE.finditer(content):
        snippet = m.group(1) or m.group(2) or ""
        result = _identity_mismatch_in_snippet(snippet, real_model)
        if result:
            return result
    return None


def unverified_sage_identifiers(content: str, real_identifiers: set) -> list[str]:
    """Pure check, no mutation: Sage-shaped identifiers the text states
    that do NOT appear in this turn's actual sage_* tool output. Empty
    list when no sage tool ran this turn (empty real_identifiers) or
    nothing's unverified. Split out 2026-10-27 so server.py's chat loop
    can GATE on this (reject the round, force a retry) instead of only
    ever finding out after the fact -- see verify_sage_claims below,
    now a thin footnote-only wrapper kept for the retry-budget-exhausted
    fallback, same role last_unresolved_sage_error's harness message
    plays for real Sage errors."""
    if not real_identifiers or not content:
        return []
    claimed = set(_SAGE_IDENTIFIER_RE.findall(content))
    return sorted(n for n in claimed if n not in real_identifiers)


def verify_sage_claims(content: str, real_identifiers: set) -> str:
    """Append a flat correction note for any Sage-shaped identifier the
    final answer states that does NOT appear in this turn's actual
    sage_* tool output. Last-resort fallback once the harness-owned
    retry budget (server.py's chat loop, using
    unverified_sage_identifiers above) is exhausted -- never the first
    line of defense anymore, see that function's docstring."""
    unverified = unverified_sage_identifiers(content, real_identifiers)
    if not unverified:
        return content
    return content + (
        "\n\n\u26a0\ufe0f Unverified against this turn's real Sage query results: "
        + ", ".join(unverified)
        + ". Treat these as unconfirmed, not fact -- ask me to look them up directly."
    )


# Unsupported access/status claims (real incident 2026-10-26): asked "how about Jobs, Sales and our Doclink
# things?" the model said "All three systems stand active and verified" after calling only the Jobs/Sales
# file tools -- no DocLink call at all. A prompt rule would not hold a 27B model (same lesson as the Sage
# fabrication), so this is a deterministic post-check in the same style as verify_sage_claims.
_CLAIM_WORD_RE = re.compile(
    r"\b(verified|accessible|healthy|confirmed|operational|fully (?:working|functional)|"
    r"(?:is|are|remains?) (?:active|up|working|available|connected))\b", re.I)
_ALREADY_RE = re.compile(r"\b(above|earlier|previous(?:ly)?|before)\b", re.I)
_ALL_N_RE = re.compile(r"\b(all (?:three|3|of them|systems)|both (?:systems|connections))\b", re.I)
_CLAIM_SYSTEMS = (
    ("DocLink", re.compile(r"\bdoc\s?link\b", re.I), "doclink_"),
    ("Wrike", re.compile(r"\bwrike\b", re.I), "wrike_"),
    ("Sage/DB02", re.compile(r"\b(?:db02|sage)\b", re.I), "sage_"),
    ("Jobs/Sales files", re.compile(
        r"\b(?:jobs|sales)\b[\w ]{0,12}\b(?:file area|folders?|share|files?|connection|access|systems?)\b", re.I), "meg_files_"),
)
_FAMILY_NAMES = {p: n for n, _, p in _CLAIM_SYSTEMS}


def _checked_families(tool_calls_made: list) -> set:
    """Tool families that ran WITHOUT an error in this reply."""
    fams = set()
    for tc in tool_calls_made or []:
        out = tc.get("output")
        if isinstance(out, dict):
            res = out.get("result")
            if out.get("error") or (isinstance(res, str) and res.lstrip().startswith("Error")):
                continue
        for _, _, prefix in _CLAIM_SYSTEMS:
            if str(tc.get("name", "")).startswith(prefix):
                fams.add(prefix)
    return fams


def verify_access_claims(content: str, tool_calls_made: list) -> str:
    """Append a flat note when the reply says a system is verified/accessible/working but no tool for that
    system ran (successfully) in this reply. Sentence-level, ignores 'as shown above' wording."""
    if not content or not _CLAIM_WORD_RE.search(content):
        return content
    checked = _checked_families(tool_calls_made)
    unchecked, generic = set(), None
    for sent in re.split(r"(?<=[.!?])\s+|\n+", content):
        if not _CLAIM_WORD_RE.search(sent) or _ALREADY_RE.search(sent):
            continue
        for name, rx, prefix in _CLAIM_SYSTEMS:
            if rx.search(sent) and prefix not in checked:
                unchecked.add(name)
        m = _ALL_N_RE.search(sent)
        if m:
            need = 3 if re.search(r"three|3", m.group(0), re.I) else 2
            if len(checked) < need:
                generic = (m.group(0).lower(), need)
    if not unchecked and not generic:
        return content
    ran = ", ".join(sorted(_FAMILY_NAMES[p] for p in checked)) or "none"
    parts = []
    if unchecked:
        parts.append("no tool call was made for: " + ", ".join(sorted(unchecked)))
    if generic:
        parts.append(f"it says '{generic[0]}' were verified, but tools ran for: {ran}")
    return content + ("\n\n\u26a0\ufe0f Not confirmed in this reply -- " + "; ".join(parts)
                      + ". Treat any 'verified / accessible / working' wording about those as unconfirmed; "
                        "ask me to check them directly.")


def verify_all_claims(content: str, tool_calls_made: list) -> str:
    """Sage identifier check, then the access-claim check. Single call site for server.py."""
    content = verify_sage_claims(content, collect_real_sage_identifiers(tool_calls_made))
    return verify_access_claims(content, tool_calls_made)


# Harness-owned error handling (2026-10-20, WORSE incident than the
# identifier-verification one above): a sage_run_query call errored
# (real SQL Server error, guessed wrong column name), and the model's
# free-text answer presented a full fake table of invented PO numbers,
# vendor codes, and dollar amounts as if the query had succeeded --
# despite the literal "Error: ..." text sitting right there in its own
# context. Council + Grok (both independently, 2026-10-20) agreed: a
# louder error string or a post-hoc "looks like a table" filter does
# NOT close this, only reduces frequency -- the loop itself has to own
# the next state. After ANY sage_* error, the model must never be
# trusted to free-generate a normal "here's your answer" reply without
# either successfully retrying or the HARNESS (not the model) stating
# the failure verbatim. See server.py's _chat_with_tools/
# _chat_with_tools_stream for where this gets enforced.
def last_unresolved_sage_error(tool_calls_made: list) -> str | None:
    """The real error text of the LAST sage_* call this turn, IF no
    later sage_* call succeeded after it (a later success clears it --
    that's a legitimate retry-and-recover, not a failure to flag).
    None if no sage_* call ran, or the turn ends on a success."""
    last_error = None
    for call in tool_calls_made or []:
        name = str(call.get("name", ""))
        if not name.startswith("sage_"):
            continue
        output = call.get("output") or {}
        result = output.get("result", "") if isinstance(output, dict) else ""
        if str(result).startswith("Error:"):
            last_error = str(result)
        else:
            last_error = None
    return last_error
