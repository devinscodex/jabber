"""Real integration test for the capability-handoff retraction fix
(2026-11-23, real incident -- Devin, 5-leg council round). Same pattern
as test_runaway_guards.py: scripted fake Ollama backend driving the
real _chat_with_tools_stream function, no real model needed.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server as s


def chunk(content="", thinking="", tool_calls=None, done=False):
    return {"message": {"content": content, "thinking": thinking, "tool_calls": tool_calls or []}, "done": done}


class _Script:
    def __init__(self, items):
        self.items = list(items)

    def stream(self):
        def _f(model, messages, ns=None, think=True):
            for c in self.items.pop(0):
                yield c
        return _f


class CapabilityHandoffTests(unittest.TestCase):
    def setUp(self):
        self._orig_stream = s._ollama_call_stream
        self.addCleanup(lambda: setattr(s, "_ollama_call_stream", self._orig_stream))

    def test_hedge_plus_tool_calls_gets_retraction_and_tools_still_run(self):
        # Real incident shape: content is a capability-handoff hedge,
        # AND tool_calls fire in the same round. Ollama delivers content
        # first, then tool_calls, in the SAME streamed response. Uses a
        # deliberately unknown tool name so run_tool fails fast/locally
        # instead of making a real network call -- this test is only
        # about the retraction+dispatch plumbing, not web_search itself.
        sc = _Script([
            [
                chunk(content="Can't follow live URLs -- I don't have a browser. "
                               "Tell me the artist name instead."),
                chunk(tool_calls=[{"function": {"name": "_test_unknown_tool", "arguments": {"query": "test"}}}],
                      done=True),
            ],
            [chunk(content="Found nothing new, here's what I have.", done=True)],
        ])
        s._ollama_call_stream = sc.stream()
        events = list(s._chat_with_tools_stream("m1", [{"role": "user", "content": "x"}], ns="t"))
        types = [e["type"] for e in events]
        shown = "".join(e["text"] for e in events if e["type"] == "content_delta")
        self.assertIn("Can't follow live URLs", shown)  # original hedge still shown (can't unsend)
        self.assertIn("Actually, let me check that for real", shown)  # retraction added
        self.assertIn("tool_call", "".join(types))  # tools still actually ran, not blocked

    def test_normal_narrated_fallback_gets_no_retraction(self):
        # "Can't open the URL, but I'll search the name instead" -- a
        # LEGITIMATE narrated fallback, not a contradiction. Must not
        # trigger the retraction (false-positive guard).
        sc = _Script([
            [
                chunk(content="Can't open the URL directly, but I'll search for the name instead."),
                chunk(tool_calls=[{"function": {"name": "_test_unknown_tool", "arguments": {"query": "test"}}}],
                      done=True),
            ],
            [chunk(content="Here's what I found.", done=True)],
        ])
        s._ollama_call_stream = sc.stream()
        events = list(s._chat_with_tools_stream("m1", [{"role": "user", "content": "x"}], ns="t"))
        shown = "".join(e["text"] for e in events if e["type"] == "content_delta")
        self.assertNotIn("Actually, let me check that for real", shown)

    def test_hedge_with_no_tool_calls_gets_no_retraction(self):
        # Stated inability with NO tool calls at all is a normal,
        # honest final answer -- nothing to retract.
        sc = _Script([[
            chunk(content="Can't follow live URLs. Tell me the artist name instead.", done=True),
        ]])
        s._ollama_call_stream = sc.stream()
        events = list(s._chat_with_tools_stream("m1", [{"role": "user", "content": "x"}], ns="t"))
        shown = "".join(e["text"] for e in events if e["type"] == "content_delta")
        self.assertNotIn("Actually, let me check that for real", shown)
        self.assertIn("Can't follow live URLs", shown)


if __name__ == "__main__":
    unittest.main()
