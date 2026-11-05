import base64
import io
import json
import unittest

from jabber import auth_local, tools
from jabber import api as api_mod
from jabber.backends import Chunk
from tests.exif_fixture import jpeg_with_exif
from tests.test_api import events
from tests.test_files import PNG
from tests.test_server import Client, FakeBackend, multi_cfg, start, stop

try:
    from PIL import Image
except ImportError:
    Image = None


def real_jpeg():
    b = io.BytesIO()
    Image.new("RGB", (200, 100), (200, 10, 10)).save(b, "JPEG")
    return b.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        auth_local.SCRYPT["n"] = 2**10
        self.fb = FakeBackend()
        self.calls, self.script = [], []
        self.fb.chat_stream = self.stream
        self.cfg = multi_cfg(default_model="m1", web=True, max_thinking_chars=300, host_note="one 24 GB GPU")
        self.srv, self.app = start(self.cfg, self.fb)
        self.addCleanup(stop, self.srv)
        self.app.local.create_user("alice", "correct horse battery")
        self.c = Client(self.srv.server_port)
        self.c.json("POST", "/api/login", {"username": "alice", "password": "correct horse battery"})
        self.pid = self.c.json("POST", "/api/projects", {"name": "P"})[1]["id"]
        self.cid = self.c.json("POST", "/api/conversations", {"project_id": self.pid})[1]["id"]

    def stream(self, model, messages, tools=None, options=None, think=False):
        self.calls.append({"messages": messages, "tools": [t["function"]["name"] for t in tools or []], "think": think})
        yield from self.script.pop(0)(messages) if self.script else [Chunk("ok"), Chunk(done=True)]

    def upload(self, name, data):
        s, r = self.c.json("POST", f"/api/projects/{self.pid}/upload/begin", {"path": name, "size": len(data)})
        uid = r["upload_id"]
        self.c.req("POST", f"/api/uploads/{uid}/chunk?offset=0", raw=data, headers={"Content-Type": "application/octet-stream"})
        return self.c.json("POST", f"/api/uploads/{uid}/finish", {})

    def chat(self, msg="hi", **kw):
        s, r, t = self.c.req("POST", f"/api/conversations/{self.cid}/chat", dict({"message": msg}, **kw))
        return events(t)


class ExifTests(unittest.TestCase):
    def test_exif_camera_date_gps(self):
        if Image is None:
            self.skipTest("needs Pillow to make a base JPEG")
        info = tools._exif(jpeg_with_exif(real_jpeg()))
        self.assertEqual((info["Camera make"], info["Date taken"], info["GPS"]),
                         ("TestCam", "2026:09:30 08:15:00", "10.50000, -66.01000"))

    def test_exif_garbage_never_raises(self):
        for junk in (b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00II*\x00\xff\xff\xff\xff", b"\xff\xd8", b"\xff\xd8\xff\xe1\xff\xff"):
            try:
                tools._exif(junk)
            except Exception as e:  # struct.error etc. are acceptable only if caught by the caller
                self.assertIsInstance(e, (Exception,))


class PromptTests(Base):
    def test_capability_block_names_tools_host_and_rules(self):
        self.chat()
        sysmsg = self.calls[0]["messages"][0]["content"]
        self.assertIn('"m1"', sysmsg)
        self.assertIn("one 24 GB GPU", sysmsg)
        self.assertIn("Tools available in this conversation: none", sysmsg)
        self.assertIn("Label every guess", sysmsg)
        self.upload("a.txt", b"x")
        self.chat("again", web=True)
        sysmsg = self.calls[-1]["messages"][0]["content"]
        self.assertIn("web_search", sysmsg)
        self.assertIn("crop_image", sysmsg)

    def test_no_tools_listed_when_none_offered(self):
        self.app.cfg.web = False
        self.chat()
        self.assertEqual(self.calls[0]["tools"], [])


@unittest.skipIf(Image is None, "needs Pillow")
class ImageToolTests(Base):
    def setUp(self):
        super().setUp()
        self.upload("pic.jpg", jpeg_with_exif(real_jpeg()))

    def ask(self, name, args):
        self.script = [lambda m: [Chunk(tool_calls=[{"name": name, "args": args}]), Chunk(done=True)],
                       lambda m: [Chunk("result: " + [x["content"] for x in m if x["role"] == "tool"][-1]), Chunk(done=True)]]
        ev = self.chat("look")
        return "".join(e["text"] for e in ev if e["type"] == "content_delta"), self.calls[-1]["messages"]

    def test_metadata(self):
        text, _ = self.ask("image_metadata", {"path": "pic.jpg"})
        for n in ("TestCam", "10.50000, -66.01000", "200x100"):
            self.assertIn(n, text)

    def test_crop_attaches_enlarged_png(self):
        text, msgs = self.ask("crop_image", {"path": "pic.jpg", "left": 0, "top": 0, "right": 0.5, "bottom": 0.5})
        img = [m for m in msgs if m.get("images")][-1]["images"][0]
        im = Image.open(io.BytesIO(base64.b64decode(img)))
        self.assertEqual(im.format, "PNG")
        self.assertGreater(im.size[0], 100)  # 100x50 crop was enlarged

    def test_crop_bad_region_and_traversal(self):
        t, _ = self.ask("crop_image", {"path": "pic.jpg", "left": 0.6, "top": 0, "right": 0.5, "bottom": 1})
        self.assertIn("region must satisfy", t)
        t, _ = self.ask("crop_image", {"path": "../../auth.db", "left": 0, "top": 0, "right": 1, "bottom": 1})
        self.assertIn("error", t)

    def test_viewed_image_comes_back_next_turn_and_persists_nothing_big(self):
        self.script = [lambda m: [Chunk(tool_calls=[{"name": "read_project_image", "args": {"path": "pic.jpg"}}]), Chunk(done=True)],
                       lambda m: [Chunk("It is red."), Chunk(done=True)]]
        self.chat("what is in pic.jpg?")
        conv = self.c.json("GET", f"/api/conversations/{self.cid}")[1]
        self.assertEqual(conv["messages"][-1]["viewed"], ["pic.jpg"])
        self.assertNotIn("images", conv["messages"][-1])
        self.chat("what colour exactly?")
        last_user = [m for m in self.calls[-1]["messages"] if m["role"] == "user"][-1]
        self.assertEqual(len(last_user["images"]), 1)
        self.assertIn("pic.jpg", last_user["content"])


class ThinkingCapTests(Base):
    def test_runaway_reasoning_is_cut_and_model_told_to_answer(self):
        self.script = [lambda m: [Chunk("", "x" * 200) for _ in range(10)] + [Chunk(done=True)],
                       lambda m: [Chunk("Best guess: low confidence."), Chunk(done=True)]]
        ev = self.chat("identify this")
        self.assertIn("note", [e["type"] for e in ev])
        self.assertEqual("".join(e["text"] for e in ev if e["type"] == "content_delta"), "Best guess: low confidence.")
        self.assertFalse(self.calls[-1]["think"])
        self.assertIn("Reasoning is running long", self.calls[-1]["messages"][-1]["content"])
        with open(self.app.telemetry.path) as fh:
            self.assertIn('"thinking_capped":true', fh.read())

    def test_normal_reasoning_untouched(self):
        self.script = [lambda m: [Chunk("", "short"), Chunk("Answer."), Chunk(done=True)]]
        ev = self.chat("hi")
        self.assertNotIn("note", [e["type"] for e in ev])
        self.assertEqual(len(self.calls), 1)

    def test_cap_can_be_disabled(self):
        self.app.cfg.max_thinking_chars = 0
        # Non-repeating filler (2026-11-04): a flat "x"*200 repeat block
        # looks exactly like genuine degenerate repetition to the new
        # pattern guard below, independent of max_thinking_chars -- this
        # test is specifically about the LENGTH cap, so the filler must
        # vary (no shared substring across OR within chunks) to avoid
        # tripping the other guard.
        self.script = [lambda m: [Chunk("", "".join("w%dx%dy " % (i, j) for j in range(30))) for i in range(10)]
                       + [Chunk("done."), Chunk(done=True)]]
        ev = self.chat("hi")
        self.assertNotIn("note", [e["type"] for e in ev])

    def test_degenerate_pattern_cuts_even_with_cap_disabled(self):
        # 2026-11-04 (ported from levi-webui after a real recurrence
        # there despite min_p alone) -- a raw length cap misses
        # degenerate reasoning that never gets long enough to hit it.
        # This guard is independent of max_thinking_chars on purpose.
        self.app.cfg.max_thinking_chars = 0
        synonyms = ("quick fast rapid speedy swift brisk nimble agile spry zippy hasty brief prompt "
                    "snappy sudden instant immediate abrupt fleet winged darting ") * 3
        # No punctuation anywhere and well over THINK_NOPUNCT_RUN_MAX (2400) -- deterministic
        # trip via the no-punctuation-run check, not relying on the repeat-count check's exact
        # 200-char window alignment.
        self.script = [lambda m: [Chunk("", synonyms) for _ in range(7)] + [Chunk(done=True)],
                       lambda m: [Chunk("Best guess: low confidence."), Chunk(done=True)]]
        ev = self.chat("identify this")
        self.assertIn("note", [e["type"] for e in ev])
        self.assertEqual("".join(e["text"] for e in ev if e["type"] == "content_delta"), "Best guess: low confidence.")
        self.assertFalse(self.calls[-1]["think"])
        self.assertIn("degenerated", self.calls[-1]["messages"][-1]["content"])


class ContentGuardTests(Base):
    """2026-11-04 -- same guard, ported from levi-webui, now applied to
    the visible content stream. Full integration coverage via the real
    generate()/chat()/regenerate() path, not just the gate class alone."""

    def test_clean_short_reply_untouched(self):
        self.script = [lambda m: [Chunk("Hello! "), Chunk("This is fine."), Chunk(done=True)]]
        ev = self.chat("hi")
        self.assertNotIn("note", [e["type"] for e in ev])
        self.assertEqual("".join(e["text"] for e in ev if e["type"] == "content_delta"), "Hello! This is fine.")

    def test_long_clean_reply_kept_whole_not_blamed(self):
        long_clean = "".join("Item %d is fine. " % i for i in range(900))  # ~16200 chars, real sentences
        self.assertGreater(len(long_clean), api_mod.CONTENT_CHAR_CAP)
        self.script = [lambda m: [Chunk(long_clean), Chunk(done=True)]]
        ev = self.chat("list many items")
        shown = "".join(e["text"] for e in ev if e["type"] == "content_delta")
        self.assertIn(long_clean, shown)  # nothing silently dropped just for being long
        self.assertIn("ran long", shown)
        self.assertNotIn("degenerated", shown)
        self.assertNotIn("limit", shown.lower())  # Devin's explicit ask: no "hit your limit" framing

    def test_degenerate_content_never_shown(self):
        # Realistic incremental chunking (real backends stream small
        # deltas, never one giant pre-built blob) -- the clean opener
        # is released on its own sentence break before the degenerate
        # tail arrives and trips the guard.
        synonyms = ("quick fast rapid speedy swift brisk nimble agile spry zippy hasty brief prompt "
                    "snappy sudden instant immediate abrupt fleet winged darting " * 8).split(" ")
        chunks = [Chunk("This part is clean. ")]
        for i in range(0, len(synonyms), 4):
            chunks.append(Chunk(" ".join(synonyms[i:i + 4]) + " "))
        chunks.append(Chunk(done=True))
        self.script = [lambda m: chunks]
        ev = self.chat("weird")
        shown = "".join(e["text"] for e in ev if e["type"] == "content_delta")
        self.assertIn("This part is clean.", shown)
        self.assertNotIn("zippy hasty brief", shown)  # the degenerate tail must never reach the browser
        self.assertIn("degenerated", shown)
        self.assertNotIn("limit", shown.lower())
        conv = self.c.json("GET", f"/api/conversations/{self.cid}")[1]
        self.assertNotIn("zippy hasty brief", conv["messages"][-1]["content"])  # not in saved history either

    def test_regenerate_also_protected_same_shared_path(self):
        # _regenerate() shares generate() with chat() in this codebase
        # (unlike levi-webui, which had two separately-maintained copies
        # and a real gap in the non-streaming one) -- one fix, both paths.
        self.script = [lambda m: [Chunk("Fine first answer."), Chunk(done=True)]]
        self.chat("hi")
        synonyms = ("loop spin twirl rotate orbit circle cycle swirl whirl gyrate pivot revolve " * 15)
        self.assertGreater(len(synonyms), api_mod.NOPUNCT_RUN_MAX)
        self.script = [lambda m: [Chunk(synonyms), Chunk(done=True)]]
        s, r = self.c.json("POST", f"/api/conversations/{self.cid}/regenerate", {})
        self.assertEqual(s, 200)
        self.assertNotIn("loop spin twirl", r["reply"])
        self.assertIn("degenerated", r["reply"])


if __name__ == "__main__":
    unittest.main()
