import unittest
from unittest import mock

from jabber import auth_local, web
from jabber.backends import Chunk
from tests.test_api import events
from tests.test_server import Client, FakeBackend, multi_cfg, start, stop

DDG_PAGE = '''<div class="result results_links web-result "><div class="links_main result__body">
<h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FEiffel_Tower&amp;rut=abc">Eiffel Tower - Wikipedia</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">The tower is <b>330 metres</b> tall.</a></div></div>
<div class="result results_links web-result "><h2 class="result__title"><a class="result__a" href="https://example.org/ad">Second &amp; result</a></h2>
<a class="result__snippet" href="x">Another snippet</a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Sponsored</a></div>'''


class ParseTests(unittest.TestCase):
    def test_parse_ddg(self):
        r = web.parse_ddg(DDG_PAGE)
        self.assertEqual([x["url"] for x in r], ["https://en.wikipedia.org/wiki/Eiffel_Tower", "https://example.org/ad"])
        self.assertEqual(r[0]["snippet"], "The tower is 330 metres tall.")
        self.assertEqual(r[1]["title"], "Second & result")

    def test_blocked_page_gives_no_results(self):
        self.assertEqual(web.parse_ddg("<html>Unfortunately, bots use DuckDuckGo too</html>"), [])

    def test_html_to_text_drops_scripts_and_nav(self):
        t, body = web.html_to_text(b"<html><head><title>T</title></head><body><nav>menu</nav><script>x()</script><p>Hello</p><p>World</p></body></html>")
        self.assertEqual((t, body), ("T", "Hello\n\nWorld"))


class GuardTests(unittest.TestCase):
    def test_private_addresses_refused(self):
        for u in ("http://127.0.0.1:8080/", "http://169.254.169.254/latest/meta-data/",
                  "http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/"):
            with self.assertRaisesRegex(web.WebError, "private"):
                web.fetch_page(u)

    def test_non_http_refused(self):
        for u in ("file:///etc/passwd", "ftp://example.com/x"):
            with self.assertRaises(web.WebError):
                web.fetch_page(u)

    def test_redirect_to_private_refused(self):
        calls = []

        def fake_check(host):
            calls.append(host)
            if host == "internal.test":
                raise web.WebError("that address is on a private network and is blocked")
        err = web.urllib.error.HTTPError("http://public.test/", 302, "Found", {"Location": "http://internal.test/admin"}, None)
        with mock.patch.object(web, "_check_host", fake_check), mock.patch.object(web._opener, "open", side_effect=err):
            with self.assertRaisesRegex(web.WebError, "private"):
                web.fetch_page("http://public.test/")
        self.assertEqual(calls, ["public.test", "internal.test"])


class WebChatTests(unittest.TestCase):
    def setUp(self):
        auth_local.SCRYPT["n"] = 2**10
        self.fb = FakeBackend()
        self.offered = []

        def stream(model, messages, tools=None, options=None, think=False):
            self.fb.calls.append((model, messages))
            self.offered.append([t["function"]["name"] for t in tools or []])
            if tools and not any(m["role"] == "tool" for m in messages):
                yield Chunk(tool_calls=[{"name": "web_search", "args": {"query": "SECRET-QUERY-TEXT tower"}},
                                        {"name": "fetch_page", "args": {"url": "http://127.0.0.1/x"}}])
                yield Chunk(done=True)
                return
            yield Chunk("Sources: " + " | ".join(m["content"][:70].replace("\n", " ") for m in messages if m["role"] == "tool"))
            yield Chunk(done=True)
        self.fb.chat_stream = stream
        self.srv, self.app = start(multi_cfg(default_model="m1", web=True), self.fb)
        self.addCleanup(stop, self.srv)
        self.app.local.create_user("alice", "correct horse battery")
        self.c = Client(self.srv.server_port)
        self.c.json("POST", "/api/login", {"username": "alice", "password": "correct horse battery"})
        self.cid = self.c.json("POST", "/api/conversations", {})[1]["id"]

    def chat(self, **extra):
        s, r, t = self.c.req("POST", f"/api/conversations/{self.cid}/chat", dict({"message": "how tall?"}, **extra))
        return events(t)

    def test_off_by_default_per_chat(self):
        self.chat()
        self.assertEqual(self.offered[-1], [])

    def test_on_offers_tools_runs_them_and_log_has_no_query(self):
        with mock.patch.object(web, "search", return_value=([{"title": "Eiffel", "url": "https://en.wikipedia.org/x", "snippet": "330 m"}], "ddg")):
            ev = self.chat(web=True)
        self.assertEqual(self.offered[0], ["web_search", "fetch_page"])
        text = "".join(e["text"] for e in ev if e["type"] == "content_delta")
        self.assertIn("Eiffel", text)
        self.assertIn("private network", text)
        self.assertEqual([e["name"] for e in ev if e["type"] == "tool"], ["web_search", "fetch_page"])
        self.assertTrue(self.c.json("GET", f"/api/conversations/{self.cid}")[1]["web"])
        self.assertIn("untrusted", self.fb.calls[0][1][0]["content"])
        with open(self.app.telemetry.path) as fh:
            raw = fh.read()
        self.assertNotIn("SECRET-QUERY-TEXT", raw)
        self.assertNotIn("127.0.0.1/x", raw)
        self.assertIn('"provider":"ddg"', raw)

    def test_flag_sticks_then_can_be_turned_off(self):
        with mock.patch.object(web, "search", return_value=([], "ddg")):
            self.chat(web=True)
            self.chat()
            self.assertEqual(self.offered[-2][:1], ["web_search"])
            self.chat(web=False)
        self.assertEqual(self.offered[-1], [])

    def test_operator_disabled_wins(self):
        self.app.cfg.web = False
        self.chat(web=True)
        self.assertEqual(self.offered[-1], [])
        self.assertFalse(self.c.json("GET", f"/api/conversations/{self.cid}")[1].get("web"))

    def test_search_failure_reaches_model_as_error(self):
        with mock.patch.object(web, "search", side_effect=web.WebError("search is unavailable right now")):
            ev = self.chat(web=True)
        self.assertIn("unavailable", "".join(e["text"] for e in ev if e["type"] == "content_delta"))


if __name__ == "__main__":
    unittest.main()
