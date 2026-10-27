import json
import os
import stat
import unittest

from jabber import auth_local, store
from tests.test_server import Client, FakeBackend, multi_cfg, start, stop


def events(text):
    return [json.loads(l) for l in text.split("\n") if l.strip()]


class ApiTests(unittest.TestCase):
    def setUp(self):
        auth_local.SCRYPT["n"] = 2**10
        self.fb = FakeBackend()
        self.srv, self.app = start(multi_cfg(system_prompt="Base.", default_model="m1"), self.fb)
        self.addCleanup(stop, self.srv)
        self.alice = self.login("alice")
        self.bob = self.login("bob")

    def login(self, name):
        self.app.local.create_user(name, "correct horse battery")
        c = Client(self.srv.server_port)
        c.json("POST", "/api/login", {"username": name, "password": "correct horse battery"})
        return c

    def new_conv(self, c, **kw):
        s, conv = c.json("POST", "/api/conversations", kw)
        self.assertEqual(s, 200)
        return conv

    def chat(self, c, cid, msg="hi", **kw):
        s, r, t = c.req("POST", f"/api/conversations/{cid}/chat", {"message": msg, **kw})
        return s, events(t)

    def test_conversation_lifecycle(self):
        conv = self.new_conv(self.alice)
        self.assertEqual(conv["title"], "New chat")
        cid = conv["id"]
        s, got = self.alice.json("GET", f"/api/conversations/{cid}")
        self.assertEqual((s, got["id"]), (200, cid))
        s, got = self.alice.json("POST", f"/api/conversations/{cid}/rename", {"title": "Plans"})
        self.assertEqual((s, got["title"]), (200, "Plans"))
        self.assertEqual(self.alice.json("POST", f"/api/conversations/{cid}/rename", {"title": " "})[0], 400)
        s, lst = self.alice.json("GET", "/api/conversations")
        self.assertEqual([c["title"] for c in lst["conversations"]], ["Plans"])
        self.assertEqual(self.alice.json("DELETE", f"/api/conversations/{cid}")[0], 200)
        self.assertEqual(self.alice.json("GET", f"/api/conversations/{cid}")[0], 404)

    def test_chat_streams_and_persists(self):
        cid = self.new_conv(self.alice)["id"]
        s, ev = self.chat(self.alice, cid, "What is up\nsecond line")
        self.assertEqual(s, 200)
        self.assertEqual(ev[0], {"type": "status", "status": "loading"})
        self.assertEqual("".join(e["text"] for e in ev if e["type"] == "content_delta"), "Hello")
        self.assertEqual("".join(e["text"] for e in ev if e["type"] == "thinking_delta"), "hm")
        self.assertEqual((ev[-1]["type"], ev[-1]["title"]), ("done", "What is up"))
        conv = self.alice.json("GET", f"/api/conversations/{cid}")[1]
        self.assertEqual([m["role"] for m in conv["messages"]], ["user", "assistant"])
        self.assertEqual((conv["messages"][1]["content"], conv["messages"][1]["thinking"]), ("Hello", "hm"))
        sent = self.fb.calls[-1][1]
        self.assertEqual(sent[0]["role"], "system")
        self.assertTrue(sent[0]["content"].startswith("Base."))
        self.assertEqual(sent[-1]["content"], "What is up\nsecond line")

    def test_history_is_sent_on_followup(self):
        cid = self.new_conv(self.alice)["id"]
        self.chat(self.alice, cid, "one")
        self.chat(self.alice, cid, "two")
        roles = [m["role"] for m in self.fb.calls[-1][1]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])

    def test_users_cannot_touch_each_others_data(self):
        cid = self.new_conv(self.alice)["id"]
        pid = self.alice.json("POST", "/api/projects", {"name": "Secret"})[1]["id"]
        self.assertEqual(self.bob.json("GET", "/api/conversations")[1]["conversations"], [])
        self.assertEqual(self.bob.json("GET", "/api/projects")[1]["projects"], [])
        for method, path, body in (
            ("GET", f"/api/conversations/{cid}", None),
            ("POST", f"/api/conversations/{cid}/rename", {"title": "x"}),
            ("POST", f"/api/conversations/{cid}/truncate", {"index": 0}),
            ("POST", f"/api/conversations/{cid}/chat", {"message": "hi"}),
            ("DELETE", f"/api/conversations/{cid}", None),
            ("GET", f"/api/projects/{pid}", None),
            ("POST", f"/api/projects/{pid}/update", {"name": "x"}),
            ("DELETE", f"/api/projects/{pid}", None),
        ):
            self.assertEqual(self.bob.json(method, path, body)[0], 404, (method, path))
        mine = self.new_conv(self.bob)["id"]
        self.assertEqual(self.bob.json("POST", f"/api/conversations/{mine}/set-project", {"project_id": pid})[0], 404)
        self.assertEqual(self.alice.json("GET", f"/api/conversations/{cid}")[0], 200)

    def test_bad_ids_are_not_found(self):
        for bad in ("..%2f..%2fauth", "ABCDEF", "x" * 32, "0" * 31, "%00"):
            self.assertEqual(self.alice.json("GET", f"/api/conversations/{bad}")[0], 404, bad)
            self.assertEqual(self.alice.json("DELETE", f"/api/projects/{bad}")[0], 404, bad)

    def test_unauthenticated(self):
        anon = Client(self.srv.server_port)
        for method, path, body in (("GET", "/api/conversations", None), ("POST", "/api/conversations", {}),
                                   ("GET", "/api/projects", None), ("DELETE", "/api/conversations/" + "a" * 32, None)):
            self.assertEqual(anon.json(method, path, body)[0], 401, (method, path))

    def test_project_instructions_reach_the_model_and_delete_unfiles(self):
        proj = self.alice.json("POST", "/api/projects", {"name": "Docs"})[1]
        s, upd = self.alice.json("POST", f"/api/projects/{proj['id']}/update", {"instructions": "Answer in French."})
        self.assertEqual(upd["instructions"], "Answer in French.")
        cid = self.new_conv(self.alice, project_id=proj["id"])["id"]
        self.chat(self.alice, cid, "hello")
        self.assertIn("Answer in French.", self.fb.calls[-1][1][0]["content"])
        self.assertTrue(self.fb.calls[-1][1][0]["content"].startswith("Base."))
        self.assertEqual(self.alice.json("DELETE", f"/api/projects/{proj['id']}")[0], 200)
        self.assertIsNone(self.alice.json("GET", f"/api/conversations/{cid}")[1]["project_id"])

    def test_set_project_and_unknown_project_on_create(self):
        proj = self.alice.json("POST", "/api/projects", {"name": "P"})[1]
        cid = self.new_conv(self.alice, project_id="0" * 32)["id"]
        self.assertIsNone(self.alice.json("GET", f"/api/conversations/{cid}")[1]["project_id"])
        s, c = self.alice.json("POST", f"/api/conversations/{cid}/set-project", {"project_id": proj["id"]})
        self.assertEqual(c["project_id"], proj["id"])
        s, c = self.alice.json("POST", f"/api/conversations/{cid}/set-project", {"project_id": None})
        self.assertIsNone(c["project_id"])
        self.assertEqual(self.alice.json("POST", "/api/projects", {"name": " "})[0], 400)

    def test_regenerate_replaces_last_reply(self):
        cid = self.new_conv(self.alice)["id"]
        self.assertEqual(self.alice.json("POST", f"/api/conversations/{cid}/regenerate", {})[0], 400)
        self.chat(self.alice, cid, "q")
        s, d = self.alice.json("POST", f"/api/conversations/{cid}/regenerate", {})
        self.assertEqual((s, d["reply"], d["model"]), (200, "Hello", "m1"))
        conv = self.alice.json("GET", f"/api/conversations/{cid}")[1]
        self.assertEqual([m["role"] for m in conv["messages"]], ["user", "assistant"])
        self.assertEqual([m["role"] for m in self.fb.calls[-1][1]], ["system", "user"])
        self.assertEqual(self.bob.json("POST", f"/api/conversations/{cid}/regenerate", {})[0], 404)

    def test_me_has_display_name_and_models_current(self):
        self.app.local.db.execute("UPDATE users SET name='Alice Smith' WHERE username='alice'")
        me = self.alice.json("GET", "/api/me")[1]
        self.assertEqual((me["display_name"], me["name"]), ("Alice", "Alice Smith"))
        self.assertEqual(self.alice.json("GET", "/api/models")[1]["current"], "m1")

    def test_truncate(self):
        cid = self.new_conv(self.alice)["id"]
        self.chat(self.alice, cid, "a")
        self.assertEqual(self.alice.json("POST", f"/api/conversations/{cid}/truncate", {"index": 99})[0], 400)
        self.assertEqual(self.alice.json("POST", f"/api/conversations/{cid}/truncate", {"index": "x"})[0], 400)
        s, c = self.alice.json("POST", f"/api/conversations/{cid}/truncate", {"index": 1})
        self.assertEqual(len(c["messages"]), 1)

    def test_backend_error_keeps_user_turn_only(self):
        self.fb.fail = True
        cid = self.new_conv(self.alice)["id"]
        s, ev = self.chat(self.alice, cid, "hi")
        self.assertEqual(ev[-1], {"type": "error", "error": "backend down"})
        conv = self.alice.json("GET", f"/api/conversations/{cid}")[1]
        self.assertEqual([m["role"] for m in conv["messages"]], ["user"])

    def test_chat_validation(self):
        cid = self.new_conv(self.alice)["id"]
        for body in ({}, {"message": ""}, {"message": 5}, {"message": "x", "images": "no"},
                     {"message": "x", "images": [{"data": "!!!"}]}):
            s, _, _ = self.alice.req("POST", f"/api/conversations/{cid}/chat", body)
            self.assertEqual(s, 400, body)

    def test_delete_requires_same_origin(self):
        cid = self.new_conv(self.alice)["id"]
        s, _ = self.alice.json("DELETE", f"/api/conversations/{cid}", headers={"Origin": "http://evil.example"})
        self.assertEqual(s, 403)
        self.assertEqual(self.alice.json("GET", f"/api/conversations/{cid}")[0], 200)

    def test_files_are_private_and_namespaced(self):
        cid = self.new_conv(self.alice)["id"]
        root = self.app.store.root
        files = [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs]
        self.assertEqual(len(files), 1)
        self.assertNotIn("alice", files[0])
        self.assertEqual(stat.S_IMODE(os.stat(files[0]).st_mode), 0o600)
        self.assertTrue(files[0].endswith(cid + ".json"))


if __name__ == "__main__":
    unittest.main()
