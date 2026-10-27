"""Model backends: Ollama native API and OpenAI-compatible servers.

Both yield the same Chunk stream, so the rest of Jabber never sees the wire
format. Internal message format follows Ollama:
  {"role": "user"|"assistant"|"system"|"tool", "content": str,
   "images": [base64, ...] (optional), "tool_calls": [{"name", "args"}] (assistant),
   "tool_name": str (tool results)}
"""
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator


class BackendError(Exception):
    pass


@dataclass
class Chunk:
    content: str = ""
    thinking: str = ""
    tool_calls: list = field(default_factory=list)  # [{"name": str, "args": dict}]
    done: bool = False
    stats: dict = field(default_factory=dict)  # prompt_tokens, completion_tokens, load_ms, eval_ms (when known)


def _norm_url(url: str) -> str:
    url = url.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def _open(req, timeout):
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(300).decode("utf-8", "replace")
        finally:
            e.close()
        raise BackendError(f"HTTP {e.code} from {req.full_url}: {detail}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BackendError(f"cannot reach {req.full_url}: {e}")


class Backend:
    kind = ""

    def __init__(self, url, api_key="", timeout=600):
        self.url = _norm_url(url)
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _get(self, path, timeout=15):
        req = urllib.request.Request(self.url + path, headers=self._headers())
        with _open(req, timeout) as r:
            return json.load(r)

    def _post_stream(self, path, payload):
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode(),
                                     headers=self._headers(), method="POST")
        return _open(req, self.timeout)

    def list_models(self, timeout=15) -> list:
        raise NotImplementedError

    def chat_stream(self, model, messages, tools=None, options=None, think=False) -> Iterator[Chunk]:
        raise NotImplementedError


class OllamaBackend(Backend):
    kind = "ollama"

    def list_models(self, timeout=15):
        return sorted(m["name"] for m in self._get("/api/tags", timeout).get("models", []))

    def chat_stream(self, model, messages, tools=None, options=None, think=False):
        payload = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        if options:
            payload["options"] = options
        if think:
            payload["think"] = True
        with self._post_stream("/api/chat", payload) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("error"):
                    raise BackendError(obj["error"])
                m = obj.get("message") or {}
                calls = [{"name": c["function"]["name"], "args": c["function"].get("arguments") or {}}
                         for c in (m.get("tool_calls") or [])]
                stats = {}
                if obj.get("done"):
                    stats = {"prompt_tokens": obj.get("prompt_eval_count"), "completion_tokens": obj.get("eval_count"),
                             "load_ms": round((obj.get("load_duration") or 0) / 1e6),
                             "eval_ms": round((obj.get("eval_duration") or 0) / 1e6)}
                yield Chunk(m.get("content") or "", m.get("thinking") or "", calls, bool(obj.get("done")), stats)


def _to_openai_messages(messages):
    out, pending = [], []
    n = 0
    for m in messages:
        role = m["role"]
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                n += 1
                cid = f"call_{n}"
                pending.append(cid)
                calls.append({"id": cid, "type": "function",
                              "function": {"name": c["name"], "arguments": json.dumps(c.get("args") or {})}})
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": calls})
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": pending.pop(0) if pending else "call_0",
                        "content": m.get("content", "")})
        elif m.get("images"):
            parts = [{"type": "text", "text": m.get("content", "")}]
            parts += [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}}
                      for b in m["images"]]
            out.append({"role": role, "content": parts})
        else:
            out.append({"role": role, "content": m.get("content", "")})
    return out


class OpenAIBackend(Backend):
    kind = "openai"

    def list_models(self, timeout=15):
        return sorted(m["id"] for m in self._get("/v1/models", timeout).get("data", []))

    def chat_stream(self, model, messages, tools=None, options=None, think=False):
        payload = {"model": model, "messages": _to_openai_messages(messages), "stream": True}
        if tools:
            payload["tools"] = tools
        if options and "num_predict" in options:
            payload["max_tokens"] = options["num_predict"]
        if options and "temperature" in options:
            payload["temperature"] = options["temperature"]
        acc = {}  # tool call index -> {"name", "args"}
        with self._post_stream("/v1/chat/completions", payload) as r:
            for raw in r:
                raw = raw.strip()
                if not raw.startswith(b"data:"):
                    continue
                data = raw[5:].strip()
                if data == b"[DONE]":
                    break
                obj = json.loads(data)
                if obj.get("error"):
                    raise BackendError(str(obj["error"]))
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    for tc in d.get("tool_calls") or []:
                        slot = acc.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                        f = tc.get("function") or {}
                        slot["name"] += f.get("name") or ""
                        slot["args"] += f.get("arguments") or ""
                    thinking = d.get("reasoning_content") or d.get("reasoning") or ""
                    if d.get("content") or thinking:
                        yield Chunk(d.get("content") or "", thinking)
        calls = []
        for i in sorted(acc):
            try:
                args = json.loads(acc[i]["args"]) if acc[i]["args"] else {}
            except ValueError:
                raise BackendError(f"model produced invalid tool arguments for {acc[i]['name']}")
            calls.append({"name": acc[i]["name"], "args": args})
        yield Chunk(tool_calls=calls, done=True)


def make(kind, url, api_key_env="") -> Backend:
    key = os.environ.get(api_key_env, "") if api_key_env else ""
    if kind == "ollama":
        return OllamaBackend(url, key)
    if kind == "openai":
        return OpenAIBackend(url, key)
    if kind != "auto":
        raise BackendError(f"unknown backend kind {kind!r}")
    for cls in (OllamaBackend, OpenAIBackend):
        b = cls(url, key)
        try:
            b.list_models(timeout=5)
            return b
        except (BackendError, ValueError, KeyError):
            continue
    raise BackendError(f"no Ollama or OpenAI-compatible server answered at {url}")
