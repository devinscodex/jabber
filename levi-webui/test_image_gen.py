# Real unit tests for the image-gen bridge (generate_image, edit_image,
# generate_qr_code, allowlist gating, NSFW soft-reject, and the
# _strip_tool_output_for_wire image lane in server.py). Same style as
# this codebase's other test_*.py files: plain assertions against the
# real module loaded exactly like production loads it, no pytest (not
# installed in this venv). Run with:
#   cd ~/levi-webui && .venv/bin/python test_image_gen.py
#
# subprocess.run is mocked for the fast/no-GPU cases below so this suite
# runs in seconds and needs no GPU -- see test_photo_cases.py (separate
# file) for the real-GPU photo-case verification this doesn't replace.
import sys
import json
import base64
import tempfile
import pathlib
import importlib.util
from unittest.mock import patch, MagicMock

sys.path.insert(0, ".")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tools = _load("tools", "tools.py")
server = _load("server", "server.py")

PASS = 0


def check(label, cond):
    global PASS
    assert cond, f"FAIL: {label}"
    PASS += 1
    print(f"PASS: {label}")


def _tiny_png(size: int) -> bytes:
    """A real, valid tiny PNG -- unlike a fake byte string, this passes
    tools.validate_image_bytes's real header-dimension parse."""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


# ---- allowlist gating (fail-closed discipline) ----
ALLOWED_NS = "alice_example.com"
check("allowlisted ns is allowed", tools._image_gen_allowed(ALLOWED_NS) is True)
check("random ns is NOT allowed", tools._image_gen_allowed("nobody_example.com") is False)
check("empty ns is NOT allowed", tools._image_gen_allowed("") is False)
check("None ns is NOT allowed", tools._image_gen_allowed(None) is False)

with patch.object(tools, "IMAGE_GEN_ALLOWLIST_PATH", pathlib.Path("/nonexistent/path.json")):
    check("missing allowlist file fails CLOSED, not open", tools._image_gen_allowed(ALLOWED_NS) is False)

with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
    f.write("{ not valid json")
    corrupt_path = f.name
with patch.object(tools, "IMAGE_GEN_ALLOWLIST_PATH", pathlib.Path(corrupt_path)):
    check("corrupt allowlist file fails CLOSED, not open", tools._image_gen_allowed(ALLOWED_NS) is False)

# ---- soft NSFW prompt reject (cheap pre-filter, not the real guarantee) ----
nsfw_prompts = ["fully nude woman", "explicit sex scene", "NSFW content please",
                "naked man on a beach", "pornographic image of"]
for p in nsfw_prompts:
    check(f"soft filter catches {p!r}", bool(tools._SOFT_NSFW_RE.search(p)))

benign_prompts = ["a lighthouse at sunset", "a cat wearing a hat",
                  "a business report cover image", "a mountain landscape in winter"]
for p in benign_prompts:
    check(f"soft filter passes benign {p!r}", not tools._SOFT_NSFW_RE.search(p))

# ---- generate_image: required-field validation ----
check("generate_image rejects missing ns",
      tools.generate_image("qwen3.6:27b", ns=None, prompt="a cat")["result"].startswith("Error:"))
check("generate_image rejects missing prompt",
      tools.generate_image("qwen3.6:27b", ns=ALLOWED_NS, prompt="")["result"].startswith("Error:"))

# ---- edit_image: required-field + path-safety validation (reuses the
# SAME _linked_folder/_resolve_within helpers read_project_image already
# uses, so this also regression-tests that shared path stays safe) ----
check("edit_image rejects missing ns",
      tools.edit_image("qwen3.6:27b", ns=None, project_id="x", path="a.png", prompt="p")["result"].startswith("Error:"))
check("edit_image rejects missing prompt",
      tools.edit_image("qwen3.6:27b", ns=ALLOWED_NS, project_id="x", path="a.png", prompt="")["result"].startswith("Error:"))

with patch.object(tools, "_linked_folder", return_value=pathlib.Path(tempfile.mkdtemp())):
    out = tools.edit_image("qwen3.6:27b", ns=ALLOWED_NS, project_id="x", path="../../etc/passwd", prompt="p")
    check("edit_image refuses a path-traversal escape", "resolves outside" in out["result"])

    out2 = tools.edit_image("qwen3.6:27b", ns=ALLOWED_NS, project_id="x", path="does-not-exist.png", prompt="p")
    check("edit_image errors cleanly on a missing file", "not a file" in out2["result"])

# ---- edit_image: pasted/uploaded-image fallback (2026-11-18 -- the far
# more common real case than a project-folder file, per Devin's own ask
# to make sure casual paste/upload works) ----
check("_last_pasted_image finds nothing in empty history", tools._last_pasted_image([]) is None)
check("_last_pasted_image finds nothing in None history", tools._last_pasted_image(None) is None)

_real_png = base64.b64encode(_tiny_png(10)).decode()
history_one_image = [
    {"role": "user", "content": "here's a photo", "images": [{"mime": "image/png", "data": _real_png}]},
]
check("_last_pasted_image decodes a real pasted image",
      tools._last_pasted_image(history_one_image) == base64.b64decode(_real_png))

_older_png = base64.b64encode(_tiny_png(11)).decode()
_newer_png = base64.b64encode(_tiny_png(12)).decode()
history_two_images = [
    {"role": "user", "content": "first photo", "images": [{"mime": "image/png", "data": _older_png}]},
    {"role": "assistant", "content": "ok"},
    {"role": "user", "content": "second photo", "images": [{"mime": "image/png", "data": _newer_png}]},
]
check("_last_pasted_image picks the MOST RECENT of several",
      tools._last_pasted_image(history_two_images) == base64.b64decode(_newer_png))

# Real incident (server.py's own 2026-10-08 comment): a flaky frontend
# paste can persist an images entry with real mime but an EMPTY data
# string -- must be skipped, not crash.
history_empty_data = [
    {"role": "user", "content": "broken paste", "images": [{"mime": "image/png", "data": ""}]},
    {"role": "user", "content": "good paste", "images": [{"mime": "image/png", "data": _real_png}]},
]
check("_last_pasted_image skips a known-broken empty-data entry",
      tools._last_pasted_image(history_empty_data) == base64.b64decode(_real_png))

check("edit_image with no project_id/path and no history errors cleanly",
      "no project_id/path given" in tools.edit_image(
          "qwen3.6:27b", ns=ALLOWED_NS, prompt="p", history_messages=[])["result"])

with patch.object(tools, "_run_qwen_image") as mock_run:
    mock_run.return_value = {"result": "Real image ready: fake.png ..."}
    out3 = tools.edit_image("qwen3.6:27b", ns=ALLOWED_NS, prompt="make it sunset",
                             history_messages=history_one_image)
    check("edit_image uses the real pasted image bytes when no project_id/path given",
          mock_run.call_args.kwargs["input_bytes"] == base64.b64decode(_real_png))
    check("edit_image result passes through from _run_qwen_image", "Real image ready" in out3["result"])

# ---- run_tool dispatch: the REAL per-call re-check, independent of
# whatever get_tool_schemas hid/showed (never trust hiding a schema
# alone -- same discipline Sage/Wrike/Office already follow) ----
out = tools.run_tool("generate_image", {"prompt": "a cat"}, model="qwen3.6:27b", ns="nobody_example.com")
check("run_tool rejects disallowed account even if called directly",
      out == {"error": "generate_image is not available for this account"})

out = tools.run_tool("edit_image", {"project_id": "x", "path": "a.png", "prompt": "p"},
                      model="qwen3.6:27b", ns="nobody_example.com")
check("run_tool rejects disallowed account for edit_image too",
      out == {"error": "edit_image is not available for this account"})

# ---- get_tool_schemas: per-identity exposure ----
allowed_names = {s["function"]["name"] for s in tools.get_tool_schemas(ALLOWED_NS)}
denied_names = {s["function"]["name"] for s in tools.get_tool_schemas("nobody_example.com")}
check("generate_image/edit_image offered to allowed account",
      {"generate_image", "edit_image"} <= allowed_names)
check("generate_image/edit_image NOT offered to disallowed account",
      not ({"generate_image", "edit_image"} & denied_names))
check("generate_qr_code offered to EVERYONE (no allowlist)",
      "generate_qr_code" in allowed_names and "generate_qr_code" in denied_names)

# ---- generate_qr_code: real, deterministic, no GPU ----
out = tools.generate_qr_code("qwen3.6:27b", data="https://example.com")
check("generate_qr_code requires data", tools.generate_qr_code("qwen3.6:27b", data="")["result"].startswith("Error:"))
check("generate_qr_code enforces its char cap",
      tools.generate_qr_code("qwen3.6:27b", data="x" * 3000)["result"].startswith("Error:"))
png_bytes = base64.b64decode(out["images"][0]["data"])
check("generate_qr_code returns a real PNG (magic bytes)", png_bytes[:8] == b"\x89PNG\r\n\x1a\n")

# ---- _run_qwen_image with a MOCKED subprocess (fast, no GPU): the real
# plumbing -- download link, disk quota, thumbnail, blocked/error paths ----
def _fake_subprocess_ok(cmd, **kwargs):
    out_path = pathlib.Path(cmd[-1])
    from PIL import Image
    Image.new("RGB", (64, 64), (200, 30, 30)).save(out_path)
    result = MagicMock()
    result.stdout = json.dumps({"ok": True, "width": 64, "height": 64, "gen_seconds": 1.2})
    result.stderr = ""
    return result


with patch.object(tools, "subprocess") as mock_sp:
    mock_sp.run.side_effect = _fake_subprocess_ok
    out = tools._run_qwen_image("generate", ALLOWED_NS, "a red square")
    check("mocked success returns a real download link",
          "Library link" in out["result"] and "/library/" in out["result"] and "https://" in out["result"])
    check("mocked success returns a capped inline preview",
          out["images"] and out["images"][0]["mime"] == "image/png")
    check("mocked success has no expiry/quota wording -- Devin's 2026-11-18 decision",
          "expires" not in out["result"].lower() and "quota" not in out["result"].lower())


def _fake_subprocess_blocked(cmd, **kwargs):
    result = MagicMock()
    result.stdout = json.dumps({"ok": False, "blocked": True, "reason": "flagged content"})
    result.stderr = ""
    return result


with patch.object(tools, "subprocess") as mock_sp:
    mock_sp.run.side_effect = _fake_subprocess_blocked
    out = tools._run_qwen_image("generate", ALLOWED_NS, "irrelevant, worker decided")
    check("blocked output never produces a download link",
          out["result"].startswith("Error:") and "flagged content" in out["result"])


def _fake_subprocess_garbage(cmd, **kwargs):
    result = MagicMock()
    result.stdout = "not json at all"
    result.stderr = "traceback here"
    return result


with patch.object(tools, "subprocess") as mock_sp:
    mock_sp.run.side_effect = _fake_subprocess_garbage
    out = tools._run_qwen_image("generate", ALLOWED_NS, "irrelevant")
    check("garbage worker output errors cleanly, never crashes the caller",
          out["result"].startswith("Error:"))

# ---- work dir cleanup: no leftover temp files regardless of outcome ----
tmp_root = tools.IMAGE_GEN_DIR / "tmp"
leftover = list(tmp_root.iterdir()) if tmp_root.exists() else []
check("no leftover work dirs after any of the above (success, blocked, or garbage)", leftover == [])

# ---- server.py: the image-lane fix actually does what it claims ----
img_output = {"result": "ok", "images": [{"mime": "image/png", "data": "QUJDRA=="}]}
stripped_default = server._strip_tool_output_for_wire(img_output)  # no name -- old behavior
stripped_lane = server._strip_tool_output_for_wire(img_output, name="generate_image")
stripped_other = server._strip_tool_output_for_wire(img_output, name="read_project_image")
check("default (no name) call still strips image data -- unchanged for _tool_result_message's own use",
      "data" not in stripped_default["images"][0])
check("generate_image is IN the image lane -- real bytes pass through unstripped",
      stripped_lane["images"][0]["data"] == "QUJDRA==")
check("a DIFFERENT tool name (read_project_image) is NOT in the lane -- still stripped as before",
      "data" not in stripped_other["images"][0])
check("_IMAGE_LANE_TOOLS contains exactly the 3 image-producing tools",
      server._IMAGE_LANE_TOOLS == {"generate_image", "edit_image", "generate_qr_code"})

print(f"\n{PASS} checks passed.")
