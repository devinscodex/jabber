"""Jabber HTTP server: static UI, sessions, models, streaming chat.

Security model:
  * Sessions are an HttpOnly, SameSite=Lax cookie holding a random token.
  * State-changing requests must be application/json and, when the browser
    sends an Origin header, that origin must match the Host (CSRF defence).
  * Responses carry a strict Content-Security-Policy; the UI renders model
    output with textContent only.
  * mode=solo with auth.provider=none identifies everyone as the local user,
    which is only allowed on a loopback bind (enforced by config.validate).
"""
import argparse
import json
import os
import queue
import re
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import admin, api, backends, config as cfgmod, files as filesmod, extract, store, telemetry
from .auth_local import AuthError, Identity, LocalAuth

STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8"}
COOKIE = "jabber_session"
MAX_BODY = 8 * 1024 * 1024
MAX_CONTENT = 200_000
MAX_IMAGES = 8
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; style-src-attr 'unsafe-inline'; script-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
SOLO_USER = Identity("local", "You", "", "admin")


class App:
    def __init__(self, cfg, backend=None):
        self.cfg = cfg
        self._backend = backend
        self._lock = threading.Lock()
        self.local = None
        self.store = store.Store(cfg.data_path())
        self.telemetry = telemetry.Telemetry(cfg.log_path(), cfg.log_enabled)
        self.files = filesmod.Files(cfg.data_path(), cfg.max_upload_bytes, cfg.user_quota_bytes)
        if cfg.auth.provider == "local":
            self.local = LocalAuth(str(cfg.data_path()))
        elif cfg.auth.provider != "none":
            raise cfgmod.ConfigError(f"auth.provider={cfg.auth.provider!r} is not implemented yet")

    def backend(self):
        with self._lock:
            if self._backend is None:
                b = self.cfg.backend
                key_env = b.api_key_env
                self._backend = backends.make(b.kind, b.url, key_env)
            return self._backend

    def needs_setup(self):
        return self.local is not None and not self.local.has_users()


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "Jabber"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---- helpers
    def _headers_common(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")

    def _json(self, code, obj, extra=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._headers_common()
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return ""
        try:
            c = SimpleCookie(raw)
        except Exception:
            return ""
        return c[COOKIE].value if COOKIE in c else ""

    def _identity(self):
        if self.app.cfg.auth.provider == "none":
            return SOLO_USER
        return self.app.local.identify(self._token())

    def _must_change(self, path):
        """True (and answers 403) if this person must set their own password before doing anything else."""
        if not path.startswith("/api/") or path in ("/api/me", "/api/password", "/api/logout", "/api/login", "/api/setup", "/api/health"):
            return False
        ident = self._identity()
        if ident and ident.must_change:
            self._json(403, {"error": "choose your own password first", "must_change": True})
            return True
        return False

    def _password(self):
        ident = self._identity()
        if not ident or not self.app.local:
            return self._json(401, {"error": "sign in required"})
        body = self._body()
        if body is None:
            return
        try:
            self.app.local.change_password(ident.id, str(body.get("current", "")), str(body.get("new", "")), self._token())
        except AuthError as e:
            return self._json(400, {"error": str(e)})
        self.app.telemetry.event("auth", action="password_changed", user_ns=store.ns_for(ident.id), **self._peer())
        self._json(200, {"ok": True})

    def _peer(self):
        return {"peer": self.client_address[0], "xff": (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or None}

    def _cookie(self, value, max_age):
        parts = [f"{COOKIE}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if self.app.cfg.secure_cookies or self.headers.get("X-Forwarded-Proto") == "https":
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def _csrf_ok(self, need_json=True):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if need_json and ctype != "application/json":
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return False
        return True

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"error": "Content-Length required"})
            return None
        if n < 0 or n > MAX_BODY:
            self._json(413, {"error": "request too large"})
            return None
        try:
            obj = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return None
        if not isinstance(obj, dict):
            self._json(400, {"error": "JSON object expected"})
            return None
        return obj

    def _static(self, name):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or Path(name).suffix not in TYPES:
            return self._json(404, {"error": "not found"})
        f = STATIC / name
        if not f.is_file():
            return self._json(404, {"error": "not found"})
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES[f.suffix])
        self.send_header("Content-Length", str(len(body)))
        self._headers_common()
        self.end_headers()
        self.wfile.write(body)

    # ---- routing
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/health":
            return self._json(200, {"ok": True})
        if path == "/api/me":
            ident = self._identity()
            cfg = self.app.cfg
            out = {"authenticated": ident is not None, "mode": cfg.mode,
                   "provider": cfg.auth.provider, "needs_setup": self.app.needs_setup(),
                   "instance_name": cfg.instance_name, "model_choice": cfg.model_choice,
                   "features": {"files": True, "tools": cfg.tools, "web": cfg.web, "extract": extract.capabilities(),
                                "max_upload_bytes": cfg.max_upload_bytes}}
            if ident:
                first = (ident.name or "").split()
                out.update(must_change=ident.must_change, name=ident.name, email=ident.email, role=ident.role, id=ident.id,
                           display_name=first[0] if first else ident.name)
            return self._json(200, out)
        if path == "/api/models":
            if not self._identity():
                return self._json(401, {"error": "sign in required"})
            return self._models()
        if self._must_change(path):
            return
        if admin.handle(self, "GET", path):
            return
        if filesmod.handle(self, "GET", path):
            return
        if api.handle(self, "GET", path):
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        raw_chunk = path.startswith("/api/uploads/") and path.endswith("/chunk")
        if not self._csrf_ok(need_json=not raw_chunk):
            return self._json(403, {"error": "request must be same-origin application/json"})
        if path == "/api/password":
            return self._password()
        if self._must_change(path):
            return
        if admin.handle(self, "POST", path):
            return
        if filesmod.handle(self, "POST", path):
            return
        if api.handle(self, "POST", path):
            return
        body = self._body()
        if body is None:
            return
        if path == "/api/setup":
            return self._setup(body)
        if path == "/api/login":
            return self._login(body)
        ident = self._identity()
        if not ident:
            return self._json(401, {"error": "sign in required"})
        if path == "/api/logout":
            if self.app.local:
                self.app.local.logout(self._token())
            self.app.telemetry.event("auth", action="logout", user_ns=store.ns_for(ident.id), **self._peer())
            return self._json(200, {"ok": True}, [self._cookie("", 0)])
        if path == "/api/chat":
            return self._chat(body)
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._csrf_ok(need_json=False):
            return self._json(403, {"error": "request must be same-origin"})
        if api.handle(self, "DELETE", path):
            return
        self._json(404, {"error": "not found"})

    # ---- handlers
    def _setup(self, body):
        if not self.app.needs_setup():
            return self._json(403, {"error": "already set up"})
        try:
            self.app.local.create_user(str(body.get("username", "")), str(body.get("password", "")),
                                       str(body.get("name", "")), str(body.get("email", "")), "admin")
        except AuthError as e:
            return self._json(400, {"error": str(e)})
        self.app.telemetry.event("auth", action="setup", user=str(body.get("username", ""))[:64].lower(), **self._peer())
        self._login(body)

    def _login(self, body):
        if not self.app.local:
            return self._json(400, {"error": "this instance has no login"})
        uname = str(body.get("username", "")).strip().lower()[:64]
        try:
            tok = self.app.local.login(uname, str(body.get("password", "")))
        except AuthError as e:
            import hashlib
            self.app.telemetry.event("auth", action="locked" if "too many" in str(e) else "login_fail",
                                     user_hash=hashlib.sha256(uname.encode()).hexdigest()[:8], **self._peer())
            return self._json(401, {"error": str(e)})
        self.app.telemetry.event("auth", action="login_ok", user=uname, **self._peer())
        self._json(200, {"ok": True}, [self._cookie(tok, 14 * 24 * 3600)])

    def _allowed(self, names):
        allow = self.app.cfg.model_allowlist
        return [n for n in names if n in allow] if allow else list(names)

    def _models(self):
        cfg = self.app.cfg
        if not cfg.model_choice:
            return self._json(200, {"models": [cfg.default_model], "default": cfg.default_model,
                                    "current": cfg.default_model})
        try:
            names = self._allowed(self.app.backend().list_models())
        except backends.BackendError as e:
            return self._json(502, {"error": str(e)})
        self._json(200, {"models": names, "default": cfg.default_model,
                         "current": cfg.default_model if cfg.default_model in names else (names[0] if names else "")})

    def _clean_messages(self, raw):
        if not isinstance(raw, list) or not raw:
            return None
        out = []
        for m in raw:
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                return None
            content = m.get("content", "")
            if not isinstance(content, str) or len(content) > MAX_CONTENT:
                return None
            msg = {"role": m["role"], "content": content}
            imgs = m.get("images")
            if imgs is not None:
                if not isinstance(imgs, list) or len(imgs) > MAX_IMAGES or not all(isinstance(i, str) for i in imgs):
                    return None
                msg["images"] = imgs
            out.append(msg)
        return out

    def _chat(self, body):
        cfg = self.app.cfg
        model = body.get("model") if cfg.model_choice else cfg.default_model
        msgs = self._clean_messages(body.get("messages"))
        allow = cfg.model_allowlist
        if not isinstance(model, str) or not model or msgs is None or (allow and model not in allow):
            return self._json(400, {"error": "invalid model or messages"})
        if cfg.system_prompt:
            msgs = [{"role": "system", "content": cfg.system_prompt}] + msgs
        think = bool(body.get("think"))
        q = queue.Queue()
        stop = threading.Event()

        def pump():
            gen = None
            try:
                gen = self.app.backend().chat_stream(model, msgs, think=think)
                for chunk in gen:
                    if stop.is_set():
                        break
                    q.put(("chunk", chunk))
                q.put(("end", None))
            except backends.BackendError as e:
                q.put(("error", str(e)))
            except Exception as e:
                sys.stderr.write("chat error: %r\n" % (e,))
                q.put(("error", "internal error"))
            finally:
                if gen is not None:
                    gen.close()

        threading.Thread(target=pump, daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("X-Accel-Buffering", "no")
        self._headers_common()
        self.end_headers()

        def emit(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()
        try:
            emit({"status": "loading"})
            while True:
                try:
                    kind, val = q.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if kind == "chunk":
                    emit({"content": val.content, "thinking": val.thinking,
                          "tool_calls": val.tool_calls, "done": val.done})
                elif kind == "error":
                    emit({"error": val, "done": True})
                    break
                else:
                    break
        except OSError:
            pass
        finally:
            stop.set()


def make_server(cfg, backend=None):
    app = App(cfg, backend)
    handler = type("BoundHandler", (Handler,), {"app": app})
    return ThreadingHTTPServer((cfg.bind, cfg.port), handler), app


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jabber")
    ap.add_argument("--config", default=os.environ.get("JABBER_CONFIG"))
    args = ap.parse_args(argv)
    try:
        cfg = cfgmod.load(args.config)
        srv, app = make_server(cfg)
    except cfgmod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    loop = cfg.bind in cfgmod.LOOPBACK
    print(f"{cfg.instance_name}: mode={cfg.mode} auth={cfg.auth.provider} on http://{cfg.bind}:{srv.server_port}")
    if cfg.mode == "multi" and not loop and not cfg.secure_cookies:
        print("warning: multi-user on a network address without secure_cookies; "
              "serve it over https and set secure_cookies = true", file=sys.stderr)
    if app.needs_setup():
        print("no accounts yet: open the page to create the first admin, or run: python -m jabber.cli add NAME --admin")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
