import unittest

from jabber import auth_local
from tests.test_server import Client, FakeBackend, multi_cfg, start, stop

PW = "correct horse battery"


class AdminTests(unittest.TestCase):
    def setUp(self):
        auth_local.SCRYPT["n"] = 2**10
        self.srv, self.app = start(multi_cfg(default_model="m1"), FakeBackend())
        self.addCleanup(stop, self.srv)
        self.app.local.create_user("boss", PW, role="admin")
        self.app.local.create_user("bob", PW)
        self.admin = self.login("boss", PW)
        self.bob = self.login("bob", PW)

    def login(self, u, p):
        c = Client(self.srv.server_port)
        s, r = c.json("POST", "/api/login", {"username": u, "password": p})
        return c if s == 200 else None

    def test_only_admins(self):
        self.assertEqual(self.bob.json("GET", "/api/admin/users")[0], 403)
        self.assertEqual(self.bob.json("POST", "/api/admin/users", {"username": "x"})[0], 403)
        self.assertEqual(self.bob.json("POST", "/api/admin/users/boss/reset", {})[0], 403)
        self.assertEqual(Client(self.srv.server_port).json("GET", "/api/admin/users")[0], 401)

    def test_list_never_exposes_secrets(self):
        s, r = self.admin.json("GET", "/api/admin/users")
        self.assertEqual({u["username"] for u in r["users"]}, {"boss", "bob"})
        self.assertTrue(next(u for u in r["users"] if u["username"] == "boss")["you"])
        blob = str(r)
        self.assertNotIn("pwhash", blob)
        self.assertNotIn("salt", blob)

    def test_let_someone_in_temp_password_forces_change(self):
        s, r = self.admin.json("POST", "/api/admin/users", {"username": "Carol", "name": "Carol C", "email": "c@x.org"})
        self.assertEqual((s, r["username"]), (200, "carol"))
        tmp = r["temp_password"]
        carol = self.login("carol", tmp)
        self.assertIsNotNone(carol)
        me = carol.json("GET", "/api/me")[1]
        self.assertTrue(me["must_change"])
        s, r = carol.json("GET", "/api/conversations")
        self.assertEqual((s, r.get("must_change")), (403, True))
        self.assertEqual(carol.json("POST", "/api/password", {"current": tmp, "new": "short"})[0], 400)
        self.assertEqual(carol.json("POST", "/api/password", {"current": "wrong-wrong-wrong", "new": "a brand new passphrase"})[0], 400)
        self.assertEqual(carol.json("POST", "/api/password", {"current": tmp, "new": "a brand new passphrase"})[0], 200)
        self.assertEqual(carol.json("GET", "/api/conversations")[0], 200)
        self.assertFalse(carol.json("GET", "/api/me")[1]["must_change"])
        self.assertIsNone(self.login("carol", tmp))
        self.assertIsNotNone(self.login("carol", "a brand new passphrase"))

    def test_duplicate_and_invalid_usernames(self):
        self.assertEqual(self.admin.json("POST", "/api/admin/users", {"username": "bob"})[0], 400)
        self.assertEqual(self.admin.json("POST", "/api/admin/users", {"username": "a b!"})[0], 400)

    def test_reset_kills_old_sessions_and_password(self):
        s, r = self.admin.json("POST", "/api/admin/users/bob/reset", {})
        self.assertEqual(s, 200)
        self.assertEqual(self.bob.json("GET", "/api/conversations")[0], 401)
        self.assertIsNone(self.login("bob", PW))
        b2 = self.login("bob", r["temp_password"])
        self.assertTrue(b2.json("GET", "/api/me")[1]["must_change"])

    def test_disable_enable_delete(self):
        self.assertEqual(self.admin.json("POST", "/api/admin/users/bob/disable", {})[0], 200)
        self.assertEqual(self.bob.json("GET", "/api/conversations")[0], 401)
        self.assertIsNone(self.login("bob", PW))
        self.assertEqual(self.admin.json("POST", "/api/admin/users/bob/enable", {})[0], 200)
        self.assertIsNotNone(self.login("bob", PW))
        self.assertEqual(self.admin.json("POST", "/api/admin/users/bob/delete", {})[0], 200)
        self.assertIsNone(self.login("bob", PW))

    def test_cannot_lock_yourself_out(self):
        for a in ("disable", "delete"):
            self.assertEqual(self.admin.json("POST", f"/api/admin/users/boss/{a}", {})[0], 400)
        self.assertEqual(self.admin.json("POST", "/api/admin/users/boss/role", {"role": "user"})[0], 400)

    def test_promote_and_unknown_user(self):
        self.assertEqual(self.admin.json("POST", "/api/admin/users/bob/role", {"role": "admin"})[0], 200)
        self.assertEqual(self.bob.json("GET", "/api/admin/users")[0], 200)
        self.assertEqual(self.admin.json("POST", "/api/admin/users/nobody/reset", {})[0], 400)

    def test_log_has_actions_not_names_or_passwords(self):
        s, r = self.admin.json("POST", "/api/admin/users", {"username": "dave"})
        with open(self.app.telemetry.path) as fh:
            raw = fh.read()
        self.assertIn('"action":"create"', raw)
        self.assertNotIn("dave", raw)
        self.assertNotIn(r["temp_password"], raw)


if __name__ == "__main__":
    unittest.main()
