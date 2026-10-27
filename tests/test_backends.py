import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from jabber import backends
from jabber.backends import BackendError, Chunk


def serve(handler):
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


class FakeOllama(BaseHTTPRequestHandler):
    log_message = lambda *a: None
    seen = []

    def do_GET(self):
        if self.path == "/api/tags":
            self._send(json.dumps({"models": [{"name": "b:7b"}, {"name": "a:1b"}]}).encode())
        else:
            self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.seen.append(body)
        lines = [
            {"message": {"thinking": "hm"}, "done": False},
            {"message": {"content": "Hel"}, "done": False},
            {"message": {"content": "lo", "tool_calls": [{"function": {"name": "f", "arguments": {"x": 1}}}]}, "done": False},
            {"message": {}, "done": True},
        ]
        self._send(("\n".join(json.dumps(l) for l in lines) + "\n").encode())

    def _send(self, b):
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


class FakeOpenAI(BaseHTTPRequestHandler):
    log_message = lambda *a: None
    seen = []

    def do_GET(self):
        if self.path == "/v1/models":
            self._send(json.dumps({"data": [{"id": "m2"}, {"id": "m1"}]}).encode())
        else:
            self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOpenAI.seen.append((body, self.headers.get("Authorization")))
        ev = [
            {"choices": [{"delta": {"reasoning_content": "hm"}}]},
            {"choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "f", "arguments": '{"x"'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]}}]},
        ]
        out = "".join(f"data: {json.dumps(e)}\n\n" for e in ev) + "data: [DONE]\n\n"
        self._send(out.encode())

    def _send(self, b):
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def collect(stream):
    chunks = list(stream)
    return ("".join(c.content for c in chunks), "".join(c.thinking for c in chunks),
            [t for c in chunks for t in c.tool_calls], chunks[-1].done)


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.o, cls.ourl = serve(FakeOllama)
        cls.a, cls.aurl = serve(FakeOpenAI)

    @classmethod
    def tearDownClass(cls):
        cls.o.shutdown()
        cls.a.shutdown()

    MSGS = [{"role": "user", "content": "hi"}]
    EXPECT = ("Hello", "hm", [{"name": "f", "args": {"x": 1}}], True)

    def test_ollama_stream(self):
        b = backends.OllamaBackend(self.ourl)
        self.assertEqual(b.list_models(), ["a:1b", "b:7b"])
        self.assertEqual(collect(b.chat_stream("a:1b", self.MSGS, think=True)), self.EXPECT)
        self.assertTrue(FakeOllama.seen[-1]["think"])

    def test_openai_stream_same_shape(self):
        b = backends.OpenAIBackend(self.aurl + "/v1", api_key="k")
        self.assertEqual(b.list_models(), ["m1", "m2"])
        self.assertEqual(collect(b.chat_stream("m1", self.MSGS)), self.EXPECT)
        self.assertEqual(FakeOpenAI.seen[-1][1], "Bearer k")

    def test_autodetect(self):
        self.assertEqual(backends.make("auto", self.ourl).kind, "ollama")
        self.assertEqual(backends.make("auto", self.aurl).kind, "openai")

    def test_autodetect_nothing_listening(self):
        with self.assertRaises(BackendError):
            backends.make("auto", f"http://127.0.0.1:{closed_port()}")

    def test_message_conversion(self):
        msgs = [
            {"role": "user", "content": "see", "images": ["AAA"]},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "f", "args": {"x": 1}}]},
            {"role": "tool", "content": "42", "tool_name": "f"},
        ]
        out = backends._to_openai_messages(msgs)
        self.assertEqual(out[0]["content"][1]["image_url"]["url"], "data:image/jpeg;base64,AAA")
        self.assertEqual(out[1]["tool_calls"][0]["id"], out[2]["tool_call_id"])
        self.assertEqual(json.loads(out[1]["tool_calls"][0]["function"]["arguments"]), {"x": 1})

    def test_unreachable_is_backend_error(self):
        with self.assertRaises(BackendError):
            backends.OllamaBackend(f"http://127.0.0.1:{closed_port()}").list_models()


if __name__ == "__main__":
    unittest.main()
