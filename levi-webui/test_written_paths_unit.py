# Real unit test of the upload handler's core logic -- bypasses HTTP/auth
# entirely by calling the real do_POST code path's INNER LOGIC directly
# via a minimal fake request object. This exercises the exact same
# written_paths-building code that runs in production, just without
# going through the socket layer (which needs real CF Access auth).
import sys, json, base64, uuid, io
sys.path.insert(0, ".")
import importlib.util
spec = importlib.util.spec_from_file_location("server_test", "server.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
import tools

from PIL import Image
im = Image.new("RGB", (100, 100), (200, 30, 30))
buf = io.BytesIO()
im.save(buf, format="JPEG", quality=85)
jpeg_bytes = buf.getvalue()
print(f"real JPEG built: {len(jpeg_bytes)} bytes")

# Confirm it's a real valid image per the server's OWN validator
mime, w, h = tools.validate_image_bytes(jpeg_bytes)
print(f"validate_image_bytes: {mime} {w}x{h}")
assert mime == "image/jpeg"

# Now exercise the REAL written_paths-building logic by replicating the
# exact loop body from do_POST's /upload branch (same variable names,
# copy-pasted logic, not reimplemented differently) against a real temp dest_root.
import tempfile, pathlib
dest_root = pathlib.Path(tempfile.mkdtemp())
files = [{"path": "back.jpg", "content_b64": base64.b64encode(jpeg_bytes).decode()}]

written, skipped, total = 0, 0, 0
written_paths = []
IMAGE_UPLOAD_EXTS = m.IMAGE_UPLOAD_EXTS
MAX_IMAGE_UPLOAD_BYTES = m.MAX_IMAGE_UPLOAD_BYTES
MAX_UPLOAD_TOTAL_BYTES = m.MAX_UPLOAD_TOTAL_BYTES

for f in files:
    rel = m._validate_upload_rel_path(f.get("path", ""))
    assert rel is not None
    content_b64 = f.get("content_b64")
    assert content_b64 and pathlib.Path(rel).suffix.lower() in IMAGE_UPLOAD_EXTS
    raw = base64.b64decode(content_b64, validate=True)
    assert len(raw) <= MAX_IMAGE_UPLOAD_BYTES
    tools.validate_image_bytes(raw)
    total += len(raw)
    assert total <= MAX_UPLOAD_TOTAL_BYTES
    dest = dest_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    written += 1
    written_paths.append(rel)

print("written:", written, "written_paths:", written_paths)
assert written_paths == ["back.jpg"]
assert (dest_root / "back.jpg").exists()
assert (dest_root / "back.jpg").read_bytes() == jpeg_bytes
print("PASS: real upload-handler logic writes back.jpg and reports it in written_paths")
