import io
import json
import os
import stat
import tempfile
import time
import unittest

from jabber import auth_local, context, stats, telemetry
from tests.test_api import events
from tests.test_server import Client, FakeBackend, multi_cfg, start, stop


class ContextTests(unittest.TestCase):
    def m(self, role, n):
        return {"role": role, "content": "x" * n}

    def test_everything_fits(self):
        kept, info = context.fit([self.m("system", 40)], [self.m("user", 80), self.m("assistant", 80), self.m("user", 40)], 16384)
        self.assertEqual((len(kept), info["dropped"]), (3, 0))

    def test_oldest_dropped_newest_kept(self):
        hist = [self.m("user" if i % 2 == 0 else "assistant", 400) for i in range(40)]
        hist.append(self.m("user", 400))
        kept, info = context.fit([self.m("system", 40)], hist, 2048)
        self.assertEqual(kept[-1], hist[-1])
        self.assertGreater(info["dropped"], 0)
        self.assertEqual(info["dropped"], len(hist) - len(kept))
        self.assertLessEqual(info["used"], int(2048 * 0.75))
        self.assertEqual(kept[0]["role"], "user")

    def test_single_message_too_big(self):
        self.assertIsNone(context.fit([], [self.m("user", 40000)], 2048))

    def test_disabled_sends_all(self):
        hist = [self.m("user", 4000)] * 10
        kept, info = context.fit([], hist, 0)
        self.assertEqual((len(kept), info["dropped"], info["limit"]), (10, 0, 0))

    def test_images_cost_tokens(self):
        a = {"role": "user", "content": "hi", "images": ["x", "x"]}
        self.assertGreater(context.msg_tokens(a), 2000)


class TelemetryTests(unittest.TestCase):
    def test_event_written_private_and_rotates(self):
        d = tempfile.mkdtemp()
        t = telemetry.Telemetry(d, max_bytes=300, keep=2)
        for i in range(20):
            t.event("chat", n=i, none=None)
        self.assertEqual(stat.S_IMODE(os.stat(t.path).st_mode), 0o600)
        names = sorted(os.listdir(d))
        self.assertIn("events.jsonl.1", names)
        self.assertNotIn("events.jsonl.3", names)
        got = list(t.read())
        self.assertTrue(all("none" not in e for e in got))
        self.assertEqual(max(e["n"] for e in got), 19)

    def test_disabled_writes_nothing(self):
        d = tempfile.mkdtemp()
        telemetry.Telemetry(d, enabled=False).event("chat", n=1)
        self.assertEqual(os.listdir(d), [])

    def test_unwritable_dir_never_raises(self):
        telemetry.Telemetry("/proc/definitely/not/writable").event("chat", n=1)


class ChatLoggingTests(unittest.TestCase):
    def setUp(self):
        auth_local.SCRYPT["n"] = 2**10
        self.fb = FakeBackend()
        self.srv, self.app = start(multi_cfg(default_model="m1", context_tokens=2048), self.fb)
        self.addCleanup(stop, self.srv)
        self.app.local.create_user("alice", "correct horse battery")
        self.c = Client(self.srv.server_port)
        self.c.json("POST", "/api/login", {"username": "alice", "password": "correct horse battery"})
        self.cid = self.c.json("POST", "/api/conversations", {})[1]["id"]

    def chat(self, msg):
        s, r, t = self.c.req("POST", f"/api/conversations/{self.cid}/chat", {"message": msg})
        return s, events(t)

    def logged(self, kind):
        return [e for e in self.app.telemetry.read() if e["kind"] == kind]

    def test_chat_event_has_numbers_and_no_text(self):
        secret = "my-secret-question-text"
        s, ev = self.chat(secret)
        e = self.logged("chat")[-1]
        for k in ("ts", "user", "conv", "model", "think", "status", "ttft_ms", "total_ms", "ctx_used", "ctx_limit", "dropped", "out_chars"):
            self.assertIn(k, e, k)
        self.assertEqual((e["status"], e["model"], e["ctx_limit"], e["dropped"]), ("ok", "m1", 2048, 0))
        raw = open(self.app.telemetry.path).read()
        self.assertNotIn(secret, raw)
        self.assertNotIn("Hello", raw)
        self.assertNotIn("alice", [e.get("user") for e in self.logged("chat")])

    def test_done_event_carries_context_and_drops_are_reported(self):
        for i in range(12):
            s, ev = self.chat(("long message %d " % i) * 60)
        self.assertEqual(s, 200)
        done = ev[-1]
        self.assertEqual(done["type"], "done")
        self.assertEqual(done["context"]["limit"], 2048)
        self.assertGreater(done["context"]["dropped"], 0)
        sent = self.fb.calls[-1][1]
        self.assertLess(len(sent), 12 * 2 + 1)
        self.assertEqual(sent[-1]["content"], ("long message 11 " * 60))
        conv = self.c.json("GET", f"/api/conversations/{self.cid}")[1]
        self.assertEqual(len(conv["messages"]), 24)
        self.assertGreater(conv["context"]["dropped"], 0)
        self.assertTrue(any(e["dropped"] > 0 for e in self.logged("chat")))

    def test_oversize_message_refused_without_saving(self):
        s, r, t = self.c.req("POST", f"/api/conversations/{self.cid}/chat", {"message": "z" * 50000})
        self.assertEqual(s, 413)
        conv = self.c.json("GET", f"/api/conversations/{self.cid}")[1]
        self.assertEqual(conv["messages"], [])

    def test_auth_events_logged_without_passwords(self):
        bad = Client(self.srv.server_port)
        bad.json("POST", "/api/login", {"username": "alice", "password": "WRONG-PASSWORD-123"})
        bad.json("POST", "/api/login", {"username": "ghost", "password": "x" * 12})
        acts = [(e["action"], e.get("user"), e.get("user_hash")) for e in self.logged("auth")]
        self.assertIn(("login_ok", "alice", None), acts)
        fails = [a for a in acts if a[0] == "login_fail"]
        self.assertEqual(len(fails), 2)
        self.assertTrue(all(a[1] is None and a[2] for a in fails))
        raw = open(self.app.telemetry.path).read()
        self.assertNotIn("WRONG-PASSWORD", raw)
        self.assertNotIn("ghost", raw)

    def test_error_chat_logged(self):
        self.fb.fail = True
        self.chat("hi")
        e = self.logged("chat")[-1]
        self.assertEqual((e["status"], e["error"]), ("error", "backend down"))

    def test_stats_summary(self):
        for i in range(3):
            self.chat("q%d" % i)
        out = io.StringIO()
        names = {k: v for k, v in [(__import__("jabber.store", fromlist=["x"]).ns_for(i), u) for i, u in self.app.local.id_map().items()]}
        stats.summarize(list(self.app.telemetry.read()), names, out=lambda s="": out.write(s + "\n"))
        text = out.getvalue()
        for needle in ("Chat requests: 3", "alice", "m1", "Latency", "Context:", "Sign-ins"):
            self.assertIn(needle, text)

    def test_stats_empty(self):
        out = io.StringIO()
        stats.summarize([], out=lambda s="": out.write(s + "\n"))
        self.assertIn("No events", out.getvalue())


if __name__ == "__main__":
    unittest.main()
