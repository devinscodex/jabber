"""Real test for the default-on core-tool flip (2026-11-23, Devin: document/
image generation should be a default core tool new users get automatically,
not something requiring an explicit admin grant -- real incident: a brand
new pilot user asked to export a list to Excel on her first real task and
was told "I am strictly read-only", purely because nobody had opted her in
yet). office_docs/image_gen allowlists flipped from opt-in to opt-out --
this tests the gate functions directly against real temp allowlist files,
same pattern as test_ns_isolation.py.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tools as t


class CoreToolDefaultOnTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._orig_office = t.OFFICE_DOCS_ALLOWLIST_PATH
        self._orig_image = t.IMAGE_GEN_ALLOWLIST_PATH
        self.addCleanup(lambda: setattr(t, "OFFICE_DOCS_ALLOWLIST_PATH", self._orig_office))
        self.addCleanup(lambda: setattr(t, "IMAGE_GEN_ALLOWLIST_PATH", self._orig_image))

        self.office_path = Path(self._tmpdir.name) / "office.json"
        self.image_path = Path(self._tmpdir.name) / "image.json"
        self.office_path.write_text(json.dumps({"disabled_namespaces": []}))
        self.image_path.write_text(json.dumps({"disabled_namespaces": ["xdisabled_martinenergygroup.com"]}))
        t.OFFICE_DOCS_ALLOWLIST_PATH = self.office_path
        t.IMAGE_GEN_ALLOWLIST_PATH = self.image_path

    def test_brand_new_namespace_gets_both_by_default(self):
        self.assertTrue(t._office_docs_allowed("nobody_martinenergygroup.com"))
        self.assertTrue(t._image_gen_allowed("nobody_martinenergygroup.com"))

    def test_explicitly_disabled_namespace_loses_only_that_one(self):
        self.assertTrue(t._office_docs_allowed("xdisabled_martinenergygroup.com"))
        self.assertFalse(t._image_gen_allowed("xdisabled_martinenergygroup.com"))

    def test_no_identity_still_refused(self):
        self.assertFalse(t._office_docs_allowed(None))
        self.assertFalse(t._image_gen_allowed(None))

    def test_missing_file_fails_open_not_closed(self):
        # Deliberately different posture from sage/wrike/file-search:
        # these tools never read company data, so a broken disabled-
        # list file should default to "nobody disabled" (everyone has
        # it), not silently take the feature away from everyone.
        t.OFFICE_DOCS_ALLOWLIST_PATH = Path(self._tmpdir.name) / "does_not_exist.json"
        t.IMAGE_GEN_ALLOWLIST_PATH = Path(self._tmpdir.name) / "also_missing.json"
        self.assertTrue(t._office_docs_allowed("anyone_martinenergygroup.com"))
        self.assertTrue(t._image_gen_allowed("anyone_martinenergygroup.com"))


if __name__ == "__main__":
    unittest.main()
