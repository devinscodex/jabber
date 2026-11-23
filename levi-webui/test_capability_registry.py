"""Real test for the capability-registry fix (2026-11-23, Devin + 5-leg
council + Grok -- see tools.get_capability_blurbs' own docstring for the
real incident: two different new users, each with real write-capable
tools granted, were told by a hand-written, never-updated system-prompt
paragraph that they flatly did not have them). get_tool_schemas and
get_capability_blurbs now share one registry so the schema and the
prompt's capability description can never independently drift again --
this asserts that invariant directly, plus the real rendered prompt text.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tools as t
import server as s


class CapabilityRegistryConsistencyTests(unittest.TestCase):
    def test_every_blurb_group_corresponds_to_a_real_schema_group(self):
        # Walking _CAPABILITY_REGISTRY itself: whatever ns has a blurb
        # for, it must also have real schemas for -- same allowed_fn.
        for cid, allowed_fn, schemas, blurb in t._CAPABILITY_REGISTRY:
            self.assertTrue(blurb)
            self.assertTrue(schemas)
            self.assertTrue(callable(allowed_fn))

    def test_blurbs_and_schemas_agree_for_a_real_granted_namespace(self):
        # Any ns with a capability blurb must also see that tool
        # group's schemas in get_tool_schemas -- the two can't drift.
        import tempfile, json
        office_path = Path(tempfile.mktemp())
        image_path = Path(tempfile.mktemp())
        office_path.write_text(json.dumps({"disabled_namespaces": []}))
        image_path.write_text(json.dumps({"disabled_namespaces": []}))
        orig_office, orig_image = t.OFFICE_DOCS_ALLOWLIST_PATH, t.IMAGE_GEN_ALLOWLIST_PATH
        t.OFFICE_DOCS_ALLOWLIST_PATH = office_path
        t.IMAGE_GEN_ALLOWLIST_PATH = image_path
        try:
            ns = "regtest_martinenergygroup.com"
            blurbs = t.get_capability_blurbs(ns)
            schemas = t.get_tool_schemas(ns)
            schema_names = {s_["function"]["name"] for s_ in schemas}
            self.assertIn("generate real downloadable Word/Excel/PowerPoint files when asked", blurbs)
            self.assertIn("create_spreadsheet", schema_names)
            self.assertIn("generate or edit images when asked", blurbs)
            self.assertIn("generate_image", schema_names)
        finally:
            t.OFFICE_DOCS_ALLOWLIST_PATH, t.IMAGE_GEN_ALLOWLIST_PATH = orig_office, orig_image
            office_path.unlink(missing_ok=True)
            image_path.unlink(missing_ok=True)

    def test_no_identity_gets_no_capability_block(self):
        self.assertEqual(t.get_capability_blurbs(None), [])
        prompt = s._build_system_prompt(None, "qwen3.6:27b", None)
        self.assertNotIn("You ALSO currently have", prompt)

    def test_system_prompt_no_longer_claims_blanket_no_write_access(self):
        # The real incident's exact false sentence must be gone from
        # the static base text -- regression guard against reverting.
        self.assertNotIn("You do NOT have write access", s.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
