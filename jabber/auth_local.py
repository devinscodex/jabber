"""Built-in accounts and sessions (auth.provider = "local").

SQLite in the data dir. Passwords: scrypt (stdlib) with a per-user salt.
Sessions: random 256-bit token handed to the browser; only its SHA-256 is
stored. Login attempts are throttled per username.
"""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from functools import wraps
from dataclasses import dataclass

SCRYPT = dict(n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
SESSION_TTL = 14 * 24 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 300
MIN_PASSWORD = 10


class AuthError(Exception):
    pass


def _sync(fn):
    @wraps(fn)
    def run(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return run


@dataclass
class Identity:
    id: str
    name: str
    email: str = ""
    role: str = "user"
    must_change: bool = False


def temp_password() -> str:
    return "-".join(secrets.token_hex(2) for _ in range(4))


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)


class LocalAuth:
    def __init__(self, data_dir):
        self._lock = threading.RLock()
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "auth.db"), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users(
              id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
              email TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'user',
              salt BLOB NOT NULL, pwhash BLOB NOT NULL, created REAL NOT NULL,
              disabled INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(
              tokhash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS fails(
              username TEXT PRIMARY KEY, count INTEGER NOT NULL, since REAL NOT NULL);
        """)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(users)")]
        if "must_change" not in cols:
            self.db.execute("ALTER TABLE users ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0")
        if "last_login" not in cols:
            self.db.execute("ALTER TABLE users ADD COLUMN last_login REAL")
        self.db.commit()
        os.chmod(os.path.join(data_dir, "auth.db"), 0o600)

    @_sync
    def has_users(self) -> bool:
        return self.db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    @_sync
    def create_user(self, username, password, name="", email="", role="user", must_change=False) -> Identity:
        username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9._-]{1,64}", username):
            raise AuthError("usernames may use letters, numbers, dot, dash and underscore only")
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
        if role not in ("user", "admin"):
            raise AuthError("invalid role")
        if not self.has_users():
            role = "admin"
        uid = secrets.token_hex(8)
        salt = os.urandom(16)
        try:
            self.db.execute(
                "INSERT INTO users(id,username,name,email,role,salt,pwhash,created,must_change) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, username, name or username, email, role, salt, _hash(password, salt), time.time(), 1 if must_change else 0))
            self.db.commit()
        except sqlite3.IntegrityError:
            raise AuthError("username taken")
        return Identity(uid, name or username, email, role, must_change)

    def _locked(self, username) -> bool:
        row = self.db.execute("SELECT count, since FROM fails WHERE username=?", (username,)).fetchone()
        return bool(row and row[0] >= MAX_FAILS and time.time() - row[1] < LOCK_SECONDS)

    @_sync
    def login(self, username, password) -> str:
        """Returns a session token, or raises AuthError."""
        username = username.strip().lower()
        if self._locked(username):
            raise AuthError("too many attempts, try again later")
        row = self.db.execute(
            "SELECT id, salt, pwhash, disabled FROM users WHERE username=?", (username,)).fetchone()
        salt = row[1] if row else b"\0" * 16
        ok = hmac.compare_digest(_hash(password, salt), row[2] if row else b"\0" * 32) and row is not None and not row[3]
        if not ok:
            now = time.time()
            cur = self.db.execute("SELECT count, since FROM fails WHERE username=?", (username,)).fetchone()
            if cur and now - cur[1] < LOCK_SECONDS:
                self.db.execute("UPDATE fails SET count=count+1 WHERE username=?", (username,))
            else:
                self.db.execute("INSERT OR REPLACE INTO fails VALUES(?,1,?)", (username, now))
            self.db.commit()
            raise AuthError("invalid username or password")
        self.db.execute("DELETE FROM fails WHERE username=?", (username,))
        self.db.execute("UPDATE users SET last_login=? WHERE id=?", (time.time(), row[0]))
        token = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO sessions VALUES(?,?,?)",
                        (hashlib.sha256(token.encode()).hexdigest(), row[0], time.time() + SESSION_TTL))
        self.db.commit()
        return token

    @_sync
    def identify(self, token) -> Identity | None:
        if not token:
            return None
        row = self.db.execute(
            "SELECT u.id,u.name,u.email,u.role,s.expires,u.must_change FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.tokhash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row[4] < time.time():
            return None
        if self.db.execute("SELECT disabled FROM users WHERE id=?", (row[0],)).fetchone()[0]:
            return None
        return Identity(row[0], row[1], row[2], row[3], bool(row[5]))

    @_sync
    def id_map(self) -> dict:
        return dict(self.db.execute("SELECT id, username FROM users"))

    @_sync
    def list_users(self) -> list:
        return [dict(zip(("username", "name", "email", "role", "disabled", "created", "last_login", "must_change"), r))
                for r in self.db.execute(
                    "SELECT username, name, email, role, disabled, created, last_login, must_change FROM users ORDER BY created")]

    def _admins(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0").fetchone()[0]

    def _get(self, username):
        row = self.db.execute("SELECT id, role, disabled FROM users WHERE username=?",
                              (username.strip().lower(),)).fetchone()
        if not row:
            raise AuthError("no such user")
        return row

    @_sync
    def set_password(self, username, password, must_change=False):
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
        uid = self._get(username)[0]
        salt = os.urandom(16)
        self.db.execute("UPDATE users SET salt=?, pwhash=?, must_change=? WHERE id=?", (salt, _hash(password, salt), 1 if must_change else 0, uid))
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        self.db.execute("DELETE FROM fails WHERE username=?", (username.strip().lower(),))
        self.db.commit()

    @_sync
    def set_role(self, username, role):
        if role not in ("user", "admin"):
            raise AuthError("invalid role")
        uid, cur, disabled = self._get(username)
        if cur == "admin" and role != "admin" and not disabled and self._admins() <= 1:
            raise AuthError("cannot demote the last admin")
        self.db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        self.db.commit()

    @_sync
    def set_disabled(self, username, disabled: bool):
        uid, role, cur = self._get(username)
        if disabled and role == "admin" and not cur and self._admins() <= 1:
            raise AuthError("cannot disable the last admin")
        self.db.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, uid))
        if disabled:
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        self.db.commit()

    @_sync
    def delete_user(self, username):
        uid, role, disabled = self._get(username)
        if role == "admin" and not disabled and self._admins() <= 1:
            raise AuthError("cannot delete the last admin")
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        self.db.execute("DELETE FROM users WHERE id=?", (uid,))
        self.db.commit()

    @_sync
    def change_password(self, user_id, current, new, keep_token):
        row = self.db.execute("SELECT username, salt, pwhash FROM users WHERE id=?", (user_id,)).fetchone()
        if not row or not hmac.compare_digest(_hash(current, row[1]), row[2]):
            raise AuthError("current password is wrong")
        if len(new) < MIN_PASSWORD:
            raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
        if new == current:
            raise AuthError("choose a different password")
        salt = os.urandom(16)
        self.db.execute("UPDATE users SET salt=?, pwhash=?, must_change=0 WHERE id=?", (salt, _hash(new, salt), user_id))
        self.db.execute("DELETE FROM sessions WHERE user_id=? AND tokhash<>?",
                        (user_id, hashlib.sha256(keep_token.encode()).hexdigest()))
        self.db.commit()

    @_sync
    def logout(self, token):
        self.db.execute("DELETE FROM sessions WHERE tokhash=?",
                        (hashlib.sha256(token.encode()).hexdigest(),))
        self.db.commit()
