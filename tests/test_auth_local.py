import os
import stat
import tempfile
import unittest

from jabber import auth_local
from jabber.auth_local import AuthError, LocalAuth


class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        auth_local.SCRYPT["n"] = 2**10  # fast hashing for tests only
        self.dir = tempfile.mkdtemp()
        self.a = LocalAuth(self.dir)

    def test_first_user_is_admin_second_is_user(self):
        self.assertEqual(self.a.create_user("alice", "correct horse battery").role, "admin")
        self.assertEqual(self.a.create_user("bob", "another long passphrase").role, "user")

    def test_login_and_identify(self):
        self.a.create_user("alice", "correct horse battery", name="Alice")
        tok = self.a.login("Alice", "correct horse battery")
        ident = self.a.identify(tok)
        self.assertEqual((ident.name, ident.role), ("Alice", "admin"))

    def test_wrong_password_and_unknown_user_same_error(self):
        self.a.create_user("alice", "correct horse battery")
        msgs = set()
        for u, p in (("alice", "wrong password!!"), ("nobody", "wrong password!!")):
            with self.assertRaises(AuthError) as c:
                self.a.login(u, p)
            msgs.add(str(c.exception))
        self.assertEqual(len(msgs), 1)

    def test_lockout_after_repeated_failures(self):
        self.a.create_user("alice", "correct horse battery")
        for _ in range(auth_local.MAX_FAILS):
            with self.assertRaises(AuthError):
                self.a.login("alice", "nope nope nope")
        with self.assertRaises(AuthError) as c:
            self.a.login("alice", "correct horse battery")
        self.assertIn("too many", str(c.exception))

    def test_short_password_and_duplicate_rejected(self):
        with self.assertRaises(AuthError):
            self.a.create_user("alice", "short")
        self.a.create_user("alice", "correct horse battery")
        with self.assertRaises(AuthError):
            self.a.create_user("ALICE", "correct horse battery")

    def test_logout_and_expiry(self):
        self.a.create_user("alice", "correct horse battery")
        tok = self.a.login("alice", "correct horse battery")
        self.a.logout(tok)
        self.assertIsNone(self.a.identify(tok))
        tok = self.a.login("alice", "correct horse battery")
        self.a.db.execute("UPDATE sessions SET expires=0")
        self.assertIsNone(self.a.identify(tok))

    def test_token_not_stored_plain_and_db_private(self):
        self.a.create_user("alice", "correct horse battery")
        tok = self.a.login("alice", "correct horse battery")
        stored = [r[0] for r in self.a.db.execute("SELECT tokhash FROM sessions")]
        self.assertNotIn(tok, stored)
        mode = stat.S_IMODE(os.stat(os.path.join(self.dir, "auth.db")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_garbage_token(self):
        self.assertIsNone(self.a.identify("x"))
        self.assertIsNone(self.a.identify(""))


if __name__ == "__main__":
    unittest.main()


class ManageTests(unittest.TestCase):
    def setUp(self):
        auth_local.SCRYPT["n"] = 2**10
        self.a = LocalAuth(tempfile.mkdtemp())
        self.a.create_user("alice", "correct horse battery")
        self.a.create_user("bob", "another long passphrase")

    def test_list(self):
        self.assertEqual([u["username"] for u in self.a.list_users()], ["alice", "bob"])

    def test_passwd_reset_kills_sessions(self):
        tok = self.a.login("bob", "another long passphrase")
        self.a.set_password("bob", "brand new passphrase")
        self.assertIsNone(self.a.identify(tok))
        self.a.login("bob", "brand new passphrase")
        with self.assertRaises(AuthError):
            self.a.login("bob", "another long passphrase")

    def test_disable_blocks_login_and_session(self):
        tok = self.a.login("bob", "another long passphrase")
        self.a.set_disabled("bob", True)
        self.assertIsNone(self.a.identify(tok))
        with self.assertRaises(AuthError):
            self.a.login("bob", "another long passphrase")
        self.a.set_disabled("bob", False)
        self.a.login("bob", "another long passphrase")

    def test_last_admin_protected(self):
        for fn in (lambda: self.a.set_role("alice", "user"),
                   lambda: self.a.set_disabled("alice", True),
                   lambda: self.a.delete_user("alice")):
            with self.assertRaises(AuthError):
                fn()
        self.a.set_role("bob", "admin")
        self.a.delete_user("alice")
        self.assertEqual([u["username"] for u in self.a.list_users()], ["bob"])

    def test_unknown_user(self):
        with self.assertRaises(AuthError):
            self.a.set_role("ghost", "admin")
