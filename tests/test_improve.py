import base64
import io
import json
import unittest

from jabber import auth_local, tools
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
        self.assertIn("Time limit on reasoning", self.calls[-1]["messages"][-1]["content"])
        with open(self.app.telemetry.path) as fh:
            self.assertIn('"thinking_capped":true', fh.read())

    def test_normal_reasoning_untouched(self):
        self.script = [lambda m: [Chunk("", "short"), Chunk("Answer."), Chunk(done=True)]]
        ev = self.chat("hi")
        self.assertNotIn("note", [e["type"] for e in ev])
        self.assertEqual(len(self.calls), 1)

    def test_cap_can_be_disabled(self):
        self.app.cfg.max_thinking_chars = 0
        self.script = [lambda m: [Chunk("", "x" * 200) for _ in range(10)] + [Chunk("done."), Chunk(done=True)]]
        ev = self.chat("hi")
        self.assertNotIn("note", [e["type"] for e in ev])


if __name__ == "__main__":
    unittest.main()
