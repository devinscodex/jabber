"""Model-facing tools over a project's uploaded files. Read-only.

Every call is scoped to one user's one project; paths are resolved inside that
project's file tree and nothing else is reachable.
"""
import base64
import io
import struct

from . import extract, files as filesmod

SCHEMAS = [
    {"type": "function", "function": {
        "name": "list_project_files",
        "description": "List files and folders in the current project's uploaded files. Folders end with '/'.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Folder to list, relative to the project root. Default '.'"}}}}},
    {"type": "function", "function": {
        "name": "read_project_file",
        "description": ("Read a text file from the project (documents are stored as '<name>.extracted.txt'). "
                        "Long files are returned in pages; if next_offset is set, call again with that offset."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path relative to the project root"},
            "offset": {"type": "integer", "description": "Character offset to continue from. Default 0"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search_project_files",
        "description": "Case-insensitive text search across the project's files. Returns file, line number and a snippet.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "path": {"type": "string", "description": "Limit to this file or folder. Default whole project"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "read_project_image",
        "description": "Look at an image file (png, jpg, gif, webp) from the project. The image is attached to the conversation.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "crop_image",
        "description": ("Zoom into part of a project image to read small text, logos or signs. Give the region as fractions "
                        "of the image from 0 to 1 (left, top, right, bottom). The enlarged crop is attached to the conversation."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "left": {"type": "number"}, "top": {"type": "number"}, "right": {"type": "number"}, "bottom": {"type": "number"}},
            "required": ["path", "left", "top", "right", "bottom"]}}},
    {"type": "function", "function": {
        "name": "image_metadata",
        "description": "Read an image's embedded metadata: size, camera, date taken, software and GPS location when present.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
]

IMAGE_MAX_BYTES = 6 * 1024 * 1024


# ---------------------------------------------------------------- EXIF (stdlib)
_TAGS = {0x010F: "Camera make", 0x0110: "Camera model", 0x0131: "Software", 0x0132: "Modified", 0x9003: "Date taken",
         0x829A: "Exposure", 0x829D: "F-number", 0x8827: "ISO", 0x920A: "Focal length", 0xA434: "Lens", 0x013B: "Artist",
         0x8298: "Copyright", 0x010E: "Description"}


def _exif(raw: bytes) -> dict:
    i, out = 2, {}
    while i + 4 < len(raw) and raw[i] == 0xFF:
        m, seg = raw[i + 1], struct.unpack(">H", raw[i + 2:i + 4])[0]
        if m == 0xE1 and raw[i + 4:i + 10] == b"Exif\x00\x00":
            t = raw[i + 10:i + 2 + seg]
            e = "<" if t[:2] == b"II" else ">"
            u16 = lambda o: struct.unpack(e + "H", t[o:o + 2])[0]
            u32 = lambda o: struct.unpack(e + "I", t[o:o + 4])[0]

            def ifd(off, gps=False):
                vals = {}
                for n in range(min(u16(off), 200)):
                    o = off + 2 + n * 12
                    tag, typ, cnt = u16(o), u16(o + 2), u32(o + 4)
                    size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}.get(typ, 1) * cnt
                    d = u32(o + 8) if size > 4 else o + 8
                    if typ == 2:
                        vals[tag] = t[d:d + cnt].split(b"\x00")[0].decode("latin-1").strip()
                    elif typ == 3:
                        vals[tag] = u16(d)
                    elif typ == 4:
                        vals[tag] = u32(d)
                    elif typ == 5:
                        vals[tag] = [(u32(d + 8 * k), u32(d + 8 * k + 4)) for k in range(min(cnt, 3))]
                    if tag in (0x8769, 0x8825) and typ == 4:
                        vals[tag] = ifd(u32(o + 8), gps=tag == 0x8825)
                return vals
            main = ifd(u32(4))
            sub, gps = main.pop(0x8769, {}), main.pop(0x8825, {})
            for src in (main, sub):
                for tag, name in _TAGS.items():
                    v = src.get(tag)
                    if v not in (None, "") and name not in out:
                        if isinstance(v, list) and len(v) == 1 and v[0][1]:
                            v = f"{v[0][0] / v[0][1]:.4g}"
                        if isinstance(v, str) or isinstance(v, (int, float)) or isinstance(v, str):
                            out[name] = v
            def deg(r):
                return r[0][0] / r[0][1] + r[1][0] / r[1][1] / 60 + r[2][0] / r[2][1] / 3600
            try:
                if 2 in gps and 4 in gps:
                    lat = deg(gps[2]) * (-1 if gps.get(1) == "S" else 1)
                    lon = deg(gps[4]) * (-1 if gps.get(3) == "W" else 1)
                    out["GPS"] = f"{lat:.5f}, {lon:.5f}"
            except (ZeroDivisionError, KeyError, TypeError):
                pass
            break
        i += 2 + seg
    return out


def _image_file(fl, ns, pid, path):
    target = fl.resolve(ns, pid, path)
    if not target.is_file() or extract.kind_of(target.name) != "image":
        raise filesmod.FileError(400, f"{path!r} is not an image file")
    return target


def image_b64(fl, ns, pid, path):
    t = _image_file(fl, ns, pid, path)
    if t.stat().st_size > IMAGE_MAX_BYTES:
        raise filesmod.FileError(413, "image is too large to show the model")
    return base64.b64encode(t.read_bytes()).decode(), t.name


def run(fl, ns, pid, name, args, char_budget):
    """Returns (result_text, image_b64_or_None, label). Never raises on bad input."""
    args = args if isinstance(args, dict) else {}
    path = str(args.get("path") or ".")
    try:
        if name == "list_project_files":
            r = fl.list_dir(ns, pid, path)
            text = "\n".join(r["entries"]) or "(empty folder)"
            if r["truncated"]:
                text += f"\n(only the first {filesmod.LIST_MAX} entries are shown)"
            return text, None, path
        if name == "read_project_file":
            r = fl.read(ns, pid, path, args.get("offset") or 0, max(500, min(char_budget, 20000)))
            text = r["content"]
            if r.get("next_offset") is not None:
                text += f"\n[more follows: call read_project_file again with offset={r['next_offset']}]"
            return text, None, path
        if name == "search_project_files":
            r = fl.search(ns, pid, str(args.get("query") or ""), path if args.get("path") else ".")
            if not r["matches"]:
                return "no matches", None, str(args.get("query"))[:60]
            lines = [f"{m['path']}:{m['line']}: {m['text']}" for m in r["matches"]]
            if r["truncated"]:
                lines.append("(more matches exist; narrow the search)")
            return "\n".join(lines), None, str(args.get("query"))[:60]
        if name == "image_metadata":
            t = _image_file(fl, ns, pid, path)
            raw = t.read_bytes()
            w, h = filesmod.validate_image(raw)
            info = {"Format": t.suffix.lstrip(".").upper(), "Size": f"{w}x{h} pixels", "File size": f"{len(raw):,} bytes"}
            if raw[:2] == b"\xff\xd8":
                info.update(_exif(raw))
            lines = [f"{k}: {v}" for k, v in info.items()]
            if len(info) <= 3:
                lines.append("No camera, date or location metadata is embedded (it may have been removed).")
            return "\n".join(lines), None, path
        if name == "crop_image":
            try:
                from PIL import Image
            except ImportError:
                return "error: cropping needs the Pillow library, which is not installed on this server", None, path
            t = _image_file(fl, ns, pid, path)
            try:
                box = [float(args[k]) for k in ("left", "top", "right", "bottom")]
            except (KeyError, TypeError, ValueError):
                return "error: left, top, right and bottom are required numbers between 0 and 1", None, path
            if not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
                return "error: the region must satisfy 0 <= left < right <= 1 and 0 <= top < bottom <= 1", None, path
            with Image.open(t) as im:
                im.load()
                W, H = im.size
                crop = im.crop((int(box[0] * W), int(box[1] * H), max(int(box[2] * W), int(box[0] * W) + 1), max(int(box[3] * H), int(box[1] * H) + 1)))
                if crop.width * crop.height < 640 * 640:
                    k = min(4, max(1, 1024 / max(crop.size)))
                    crop = crop.resize((int(crop.width * k), int(crop.height * k)), Image.LANCZOS)
                crop.thumbnail((1536, 1536))
                buf = io.BytesIO()
                crop.convert("RGB").save(buf, "PNG")
            return f"cropped region ({box[0]:.2f},{box[1]:.2f})-({box[2]:.2f},{box[3]:.2f}) of {path}, attached to the next message", \
                base64.b64encode(buf.getvalue()).decode(), path
        if name == "read_project_image":
            target = fl.resolve(ns, pid, path)
            if not target.is_file() or extract.kind_of(target.name) != "image":
                return f"error: {path!r} is not an image file", None, path
            if target.stat().st_size > IMAGE_MAX_BYTES:
                return f"error: image is over {IMAGE_MAX_BYTES // 2**20} MB and cannot be shown to the model", None, path
            return "the image is attached to the next message", base64.b64encode(target.read_bytes()).decode(), path
        return f"error: unknown tool {name!r}", None, name
    except (ValueError, TypeError):
        return "error: invalid arguments", None, path
    except filesmod.FileError as e:
        return f"error: {e.message}", None, path
    except OSError:
        return "error: could not read that file", None, path
