"""Real ns-isolation + traversal-safety test for tools.py's project-
scoped read tools (list_project_folder/read_project_file/
read_project_image).

Added 2026-10-12 (council review -- gilfoyle, both legs of the
2026-10-12 consults, core/decision-ledger.csv): "Verified there's no
existing test file anywhere in this project proving that discipline
holds (zero test_*.py in levi-webui/)... write the real test before
this specific tool goes live, not after." This is that test -- the
FIRST real automated test in levi-webui/, not a retrofit.

Covers, all against the REAL functions (not mocks), using real temp
directories and real on-disk project JSON files in the exact shape
server.py's own _save_proj/_proj_path produce:

  1. Cross-namespace isolation: identity A's tool call for identity
     B's real project_id gets "no such project", not B's real data --
     the exact invariant that silently broke once already (2026-10-08,
     a real project-lookup bug, caught by hand not by a test).
  2. Traversal refused: "../" components, absolute paths.
  3. Sibling-directory-prefix attack refused -- the REAL bug fixed
     2026-10-12 (council review -- bard): a linked folder at
     /tmp/.../proj and an attacker-reachable-shaped path resolving to
     /tmp/.../proj2 (same string prefix, different real directory)
     used to pass the OLD str.startswith() check; _resolve_within's
     Path.is_relative_to() must refuse it.
  4. read_project_image: refuses non-image extensions, refuses
     malformed/non-image bytes (magic-byte check), refuses a declared-
     oversized image header (pixel-dimension decompression-bomb
     guard), accepts a real valid image and returns real base64.
  5. read_project_file: real text reads correctly, honors its own size
     cap.

Run directly: python3 test_ns_isolation.py
(stdlib unittest only, zero new dependencies, matches this project's
own doctrine)
"""
import base64
import json
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tools


# A real, valid PNG (8x8 solid color) -- built directly via struct
# packing, not a fixture file, same stdlib-only discipline as the rest
# of this project. Used as "real valid image" in several tests below.
def _build_real_png(width=8, height=8):
    import zlib

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw_row = b"\x00" + b"\xff\x00\x00" * width  # filter byte 0 + solid red row
    raw = raw_row * height
    idat = zlib.compress(raw)
    iend = b""
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", iend)


def _build_fake_oversized_png():
    """Real PNG signature + real IHDR structure, but DECLARES a huge
    width/height -- the exact pixel-dimension-bomb shape the guard
    exists for. No real pixel data follows (doesn't need to -- the
    guard must refuse BEFORE reading IDAT)."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 50000, 50000, 8, 2, 0, 0, 0)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + b"\x00\x00\x00\x00"
    return sig + ihdr


class ToolsNsIsolationTest(unittest.TestCase):
    def setUp(self):
        # Real temp root, patched onto tools.PROJ_DIR for the duration
        # of each test -- never touches the real levi-webui/projects/
        # directory or any real pilot user's real data.
        self.tmp_root = Path(tempfile.mkdtemp(prefix="levi_test_"))
        self.addCleanup(shutil.rmtree, self.tmp_root, ignore_errors=True)
        self._orig_proj_dir = tools.PROJ_DIR
        tools.PROJ_DIR = self.tmp_root / "projects"
        tools.PROJ_DIR.mkdir(parents=True)

        # Two real, separate identities -- same shape real Cloudflare
        # Access emails produce (server.py's _identity_ns).
        self.ns_a = "alice_example.com"
        self.ns_b = "bob_example.com"

        # Real linked folders, one per identity, OUTSIDE PROJ_DIR (same
        # as real deployments -- linked_folder points at UPLOAD_DIR,
        # a sibling, not nested under PROJ_DIR).
        self.folder_a = self.tmp_root / "uploads_a"
        self.folder_b = self.tmp_root / "uploads_b"
        self.folder_a.mkdir()
        self.folder_b.mkdir()

        # A REAL sibling directory with a name-prefix collision, for
        # the is_relative_to traversal test -- folder_a's own name
        # ("proj") is a string-prefix of this one ("proj2").
        self.prefix_collision_dir = self.tmp_root / (self.folder_a.name + "2")
        self.prefix_collision_dir.mkdir()
        (self.prefix_collision_dir / "secret.txt").write_text("identity B should never see this")

        self.proj_id_a = "a" * 32
        self.proj_id_b = "b" * 32
        self._write_project(self.ns_a, self.proj_id_a, self.folder_a)
        self._write_project(self.ns_b, self.proj_id_b, self.folder_b)

    def tearDown(self):
        tools.PROJ_DIR = self._orig_proj_dir

    def _write_project(self, ns, proj_id, linked_folder):
        d = tools.PROJ_DIR / ns
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{proj_id}.json").write_text(json.dumps({
            "id": proj_id, "name": "test project",
            "linked_folder": str(linked_folder.resolve()),
            "instructions": "", "upload_snapshot": True,
        }))

    # ---- 1. Cross-namespace isolation ----

    def test_wrong_ns_cannot_see_right_projects_data(self):
        (self.folder_a / "secret_a.txt").write_text("Alice's real confidential content")
        # Identity B calling with Alice's REAL project_id, under B's
        # OWN ns -- must get "no such project", never Alice's data.
        out = tools.run_tool(
            "read_project_file",
            {"project_id": self.proj_id_a, "path": "secret_a.txt"},
            model="qwen3.6:27b", ns=self.ns_b,
        )
        self.assertIn("error", out)
        self.assertIn("no such project", out["error"])
        self.assertNotIn("content", out)

    def test_right_ns_can_see_own_data(self):
        (self.folder_a / "real.txt").write_text("real alice content")
        out = tools.run_tool(
            "read_project_file",
            {"project_id": self.proj_id_a, "path": "real.txt"},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertEqual(out.get("content"), "real alice content")

    def test_wrong_ns_list_project_folder_refused(self):
        (self.folder_b / "bob_file.txt").write_text("bob content")
        out = tools.run_tool(
            "list_project_folder",
            {"project_id": self.proj_id_b},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertIn("error", out)
        self.assertNotIn("entries", out)

    def test_missing_ns_refused_not_silently_defaulted(self):
        out = tools.run_tool(
            "read_project_file", {"project_id": self.proj_id_a, "path": "x"},
            model="qwen3.6:27b", ns=None,
        )
        self.assertIn("error", out)
        self.assertIn("identity namespace", out["error"])

    # ---- 2. Traversal refused ----

    def test_dotdot_traversal_refused(self):
        out = tools.run_tool(
            "read_project_file",
            {"project_id": self.proj_id_a, "path": "../uploads_b/anything"},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertIn("error", out)
        self.assertIn("outside", out["error"])

    def test_absolute_path_traversal_refused(self):
        out = tools.run_tool(
            "read_project_file",
            {"project_id": self.proj_id_a, "path": "/etc/passwd"},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertIn("error", out)

    # ---- 3. Sibling-directory-prefix attack (the real 2026-10-12 bugfix) ----

    def test_sibling_prefix_directory_not_reachable(self):
        """REAL regression test for the str.startswith() -> is_relative_to()
        fix. Before the fix, a resolved path landing in a DIFFERENT
        directory that merely shares a string prefix with the real
        linked folder would have passed the old check. This constructs
        exactly that shape and confirms _resolve_within refuses it."""
        target = tools._resolve_within(self.folder_a, "../" + self.prefix_collision_dir.name + "/secret.txt")
        self.assertIsNone(target, "sibling dir with a name-prefix collision must NOT resolve as inside the linked folder")

    def test_resolve_within_accepts_real_subpath(self):
        (self.folder_a / "sub").mkdir()
        (self.folder_a / "sub" / "f.txt").write_text("x")
        target = tools._resolve_within(self.folder_a, "sub/f.txt")
        self.assertIsNotNone(target)
        self.assertTrue(target.is_relative_to(self.folder_a.resolve()))

    # ---- 4. read_project_image ----

    def test_read_project_image_real_valid_png(self):
        png = _build_real_png()
        (self.folder_a / "real.png").write_bytes(png)
        out = tools.run_tool(
            "read_project_image",
            {"project_id": self.proj_id_a, "path": "real.png"},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertIn("images", out)
        img = out["images"][0]
        self.assertEqual(img["mime"], "image/png")
        self.assertEqual(base64.b64decode(img["data"]), png)

    def test_read_project_image_refuses_non_image_extension(self):
        (self.folder_a / "notes.md").write_text("just text")
        out = tools.run_tool(
            "read_project_image",
            {"project_id": self.proj_id_a, "path": "notes.md"},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertIn("error", out)
        self.assertIn("not a recognized image extension", out["error"])

    def test_read_project_image_refuses_renamed_non_image(self):
        (self.folder_a / "fake.png").write_bytes(b"this is not a real png at all")
        out = tools.run_tool(
            "read_project_image",
            {"project_id": self.proj_id_a, "path": "fake.png"},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertIn("error", out)
        self.assertIn("image validation", out["error"])

    def test_read_project_image_refuses_declared_oversized_header(self):
        (self.folder_a / "bomb.png").write_bytes(_build_fake_oversized_png())
        out = tools.run_tool(
            "read_project_image",
            {"project_id": self.proj_id_a, "path": "bomb.png"},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertIn("error", out)
        self.assertIn("pixel", out["error"].lower())

    def test_read_project_image_cross_ns_refused(self):
        png = _build_real_png()
        (self.folder_b / "bob.png").write_bytes(png)
        out = tools.run_tool(
            "read_project_image",
            {"project_id": self.proj_id_b, "path": "bob.png"},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertIn("error", out)
        self.assertNotIn("images", out)

    # ---- 5. read_project_file size cap ----

    def test_read_project_file_size_cap(self):
        under_cap = "x" * 150_000
        (self.folder_a / "under.txt").write_text(under_cap)
        out = tools.run_tool(
            "read_project_file",
            {"project_id": self.proj_id_a, "path": "under.txt"},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertEqual(out.get("content"), under_cap)  # under the real 200KB cap, should succeed

        too_big = "x" * 250_000
        (self.folder_a / "toobig.txt").write_text(too_big)
        out2 = tools.run_tool(
            "read_project_file",
            {"project_id": self.proj_id_a, "path": "toobig.txt"},
            model="qwen3.6:27b", ns=self.ns_a,
        )
        self.assertIn("error", out2)
        self.assertIn("cap", out2["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
