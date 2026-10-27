import http.client
import json
import tempfile
import threading
import unittest

from jabber import auth_local, config, server
from jabber.backends import BackendError, Chunk


class FakeBackend:
    kind = "fake"
    def __init__(self):
        self.calls = []
        self.fail = False

    def list_models(self, timeout=15):
        return ["m1", "m2", "secret"]

    def chat_stream(self, model, messages, tools=None, options=None, think=False):
        self.calls.append((model, messages))
        if self.fail:
            raise BackendError("backend down")
        yield Chunk("Hel", "hm")
        yield Chunk("lo")
        yield Chunk(done=True)


def start(cfg, backend=None):
    srv, app = server.make_server(cfg, backend or FakeBackend())
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return srv, app


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = ""

    def req(self, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie:
            h["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        text = r.read().decode()
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0] if not sc.startswith(f"{server.COOKIE}=;") else ""
        return r.status, r, text

    def json(self, *a, **kw):
        s, r, t = self.req(*a, **kw)
        return s, (json.loads(t) if t else {})


def stop(srv):
    srv.shutdown()
    srv.server_close()


def multi_cfg(**kw):
    d = tempfile.mkdtemp()
    cfg = config.Config(mode="multi", port=0, data_dir=d, **kw)
    cfg.auth.provider = "local"
    return config.validate(cfg)


class ServerTests(unittest.TestCase):
    def setUp(self):
        auth_local.SCRYPT["n"] = 2**10

    def test_solo_needs_no_login(self):
        srv, _ = start(config.validate(config.Config(port=0)))
        self.addCleanup(stop, srv)
        c = Client(srv.server_port)
        s, me = c.json("GET", "/api/me")
        self.assertEqual((s, me["authenticated"], me["name"], me["mode"]), (200, True, "You", "solo"))
        s, d = c.json("GET", "/api/models")
        self.assertEqual(d["models"], ["m1", "m2", "secret"])

    def test_multi_requires_login_then_setup_once(self):
        srv, _ = start(multi_cfg())
        self.addCleanup(stop, srv)
        c = Client(srv.server_port)
        s, me = c.json("GET", "/api/me")
        self.assertEqual((me["authenticated"], me["needs_setup"]), (False, True))
        self.assertEqual(c.json("GET", "/api/models")[0], 401)
        self.assertEqual(c.json("POST", "/api/chat", {"model": "m1", "messages": []})[0], 401)
        s, _ = c.json("POST", "/api/setup", {"username": "alice", "password": "correct horse battery"})
        self.assertEqual(s, 200)
        self.assertEqual(c.json("GET", "/api/me")[1]["role"], "admin")
        self.assertEqual(Client(srv.server_port).json("POST", "/api/setup",
                         {"username": "eve", "password": "another long passphrase"})[0], 403)

    def test_cookie_flags_and_logout(self):
        srv, app = start(multi_cfg(secure_cookies=True))
        self.addCleanup(stop, srv)
        app.local.create_user("alice", "correct horse battery")
        c = Client(srv.server_port)
        s, r, _ = c.req("POST", "/api/login", {"username": "alice", "password": "correct horse battery"})
        sc = r.getheader("Set-Cookie")
        for flag in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/"):
            self.assertIn(flag, sc)
        self.assertEqual(c.json("GET", "/api/me")[1]["authenticated"], True)
        old = c.cookie
        self.assertEqual(c.json("POST", "/api/logout", {})[0], 200)
        c.cookie = old
        self.assertEqual(c.json("GET", "/api/me")[1]["authenticated"], False)

    def test_bad_login(self):
        srv, app = start(multi_cfg())
        self.addCleanup(stop, srv)
        app.local.create_user("alice", "correct horse battery")
        c = Client(srv.server_port)
        self.assertEqual(c.json("POST", "/api/login", {"username": "alice", "password": "wrong password!"})[0], 401)

    def test_csrf_defences(self):
        srv, app = start(multi_cfg())
        self.addCleanup(stop, srv)
        app.local.create_user("alice", "correct horse battery")
        c = Client(srv.server_port)
        body = {"username": "alice", "password": "correct horse battery"}
        self.assertEqual(c.json("POST", "/api/login", body, headers={"Origin": "http://evil.example"})[0], 403)
        s, _, _ = c.req("POST", "/api/login", raw="username=alice&password=x",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(s, 403)
        ok = c.json("POST", "/api/login", body, headers={"Origin": f"http://127.0.0.1:{srv.server_port}"})
        self.assertEqual(ok[0], 200)

    def _login(self, srv, app):
        app.local.create_user("alice", "correct horse battery")
        c = Client(srv.server_port)
        c.json("POST", "/api/login", {"username": "alice", "password": "correct horse battery"})
        return c

    def test_chat_streams_and_prepends_system_prompt(self):
        fb = FakeBackend()
        srv, app = start(multi_cfg(system_prompt="Be brief."), fb)
        self.addCleanup(stop, srv)
        c = self._login(srv, app)
        s, r, t = c.req("POST", "/api/chat", {"model": "m1", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual((s, r.getheader("Content-Type")), (200, "text/event-stream"))
        self.assertEqual(r.getheader("X-Accel-Buffering"), "no")
        events = [json.loads(l[6:]) for l in t.split("\n\n") if l.startswith("data: ")]
        self.assertEqual(events[0], {"status": "loading"})
        events = [e for e in events if "content" in e]
        self.assertEqual("".join(e["content"] for e in events), "Hello")
        self.assertEqual("".join(e["thinking"] for e in events), "hm")
        self.assertTrue(events[-1]["done"])
        self.assertEqual(fb.calls[0][1][0], {"role": "system", "content": "Be brief."})

    def test_chat_validation(self):
        srv, app = start(multi_cfg(model_allowlist=["m1"]))
        self.addCleanup(stop, srv)
        c = self._login(srv, app)
        good = [{"role": "user", "content": "hi"}]
        self.assertEqual(c.json("POST", "/api/chat", {"model": "secret", "messages": good})[0], 400)
        self.assertEqual(c.json("POST", "/api/chat", {"model": "m1", "messages": [{"role": "system", "content": "x"}]})[0], 400)
        self.assertEqual(c.json("POST", "/api/chat", {"model": "m1", "messages": []})[0], 400)
        self.assertEqual(c.json("POST", "/api/chat", {"model": "", "messages": good})[0], 400)
        self.assertEqual(c.json("GET", "/api/models")[1]["models"], ["m1"])

    def test_backend_error_arrives_as_stream_event(self):
        fb = FakeBackend()
        fb.fail = True
        srv, app = start(multi_cfg(), fb)
        self.addCleanup(stop, srv)
        c = self._login(srv, app)
        s, r, t = c.req("POST", "/api/chat", {"model": "m1", "messages": [{"role": "user", "content": "hi"}]})
        events = [json.loads(l[6:]) for l in t.split("\n\n") if l.startswith("data: ")]
        self.assertEqual(s, 200)
        self.assertEqual(events[0], {"status": "loading"})
        self.assertEqual((events[-1]["error"], events[-1]["done"]), ("backend down", True))

    def test_model_choice_off_forces_default_and_hides_list(self):
        fb = FakeBackend()
        srv, app = start(multi_cfg(default_model="m2", model_choice=False), fb)
        self.addCleanup(stop, srv)
        c = self._login(srv, app)
        self.assertFalse(c.json("GET", "/api/me")[1]["model_choice"])
        self.assertEqual(c.json("GET", "/api/models")[1]["models"], ["m2"])
        c.req("POST", "/api/chat", {"model": "secret", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(fb.calls[-1][0], "m2")

    def test_body_limit_and_bad_json(self):
        srv, _ = start(multi_cfg())
        self.addCleanup(stop, srv)
        c = Client(srv.server_port)
        s, _, _ = c.req("POST", "/api/login", raw="{" * 10, headers={"Content-Type": "application/json"})
        self.assertEqual(s, 400)
        s, _, _ = c.req("POST", "/api/login", raw="x", headers={"Content-Type": "application/json",
                                                              "Content-Length": str(server.MAX_BODY + 1)})
        self.assertEqual(s, 413)

    def test_static_and_security_headers(self):
        srv, _ = start(multi_cfg())
        self.addCleanup(stop, srv)
        c = Client(srv.server_port)
        s, r, t = c.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn("Jabber", t)
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        csp = r.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp.split("style-src-attr")[0])
        self.assertNotIn("script-src 'self' 'unsafe", csp)
        self.assertEqual(c.req("GET", "/static/app.js")[0], 200)
        for bad in ("/static/../server.py", "/static/..%2fserver.py", "/static/config.py", "/static/nope.js"):
            self.assertEqual(c.req("GET", bad)[0], 404, bad)

    def test_unimplemented_provider_refuses_to_start(self):
        cfg = config.Config(mode="multi", port=0)
        cfg.auth.provider = "proxy"
        with self.assertRaises(config.ConfigError):
            server.make_server(config.validate(cfg), FakeBackend())


if __name__ == "__main__":
    unittest.main()
