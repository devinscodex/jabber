"""Real integration tests for the runaway-generation guards (content
holdback + thinking pattern check) added 2026-11-04 after a live
synonym-chain degeneration recurrence (see core/decision-ledger.csv,
2026-11-04 COUNCIL+BUILD rows) despite the 2026-10-22 min_p fix.

Drives the REAL _chat_with_tools/_chat_with_tools_stream functions
(not the gate class in isolation) with a scripted fake Ollama backend,
so every scenario is deterministic and needs no real model. Covers:

  1. Clean short reply -- untouched, no notices.
  2. Long but CLEAN reply (over CONTENT_CHAR_CAP, no pattern issue) --
     kept whole, neutral "ran long" notice, never blamed as degenerate,
     no "limit" wording (Devin's explicit ask).
  3. Genuinely degenerate content -- a clean opener is released and
     kept; the synonym-chain tail is NEVER shown via any content_delta
     event and never reaches the saved reply, fed in realistic small
     incremental chunks (real backends never deliver one giant
     pre-built blob -- an earlier draft of this test fed one mega-
     chunk and got a false failure on the clean opener for exactly
     that reason; keep it incremental).
  4. Degenerate THINKING with clean content after -- reasoning
     discarded (thinking_reset), retried once with think=False, final
     answer is clean. Independent of THINK_CHAR_CAP (a raw length cap
     alone misses a shorter degenerate run, which is the actual gap
     this fix closed).
  5. /regenerate path (_chat_with_tools, non-streaming) -- this
     function had ZERO protection before 2026-11-04 (a real gap, not a
     threshold being too loose): one bad attempt gets retried once;
     bad on both attempts gets trimmed + notice. Never raw garbage
     either way.

Run directly: python3 test_runaway_guards.py
(stdlib unittest only, zero new dependencies, matches this project's
own doctrine -- see test_ns_isolation.py's docstring)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server as s

SYNONYMS = ("quick fast rapid speedy swift brisk nimble agile spry zippy hasty brief prompt "
            "snappy sudden instant immediate abrupt fleet winged darting ")


def chunk(content="", thinking="", tool_calls=None, done=False):
    return {"message": {"content": content, "thinking": thinking, "tool_calls": tool_calls or []}, "done": done}


class _Script:
    """Each call to the mocked backend pops the next scripted response
    (stream: a list of chunks; single: one dict), same pop-per-call
    shape as a real multi-round conversation."""

    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def stream(self, **_defaults):
        def _f(model, messages, ns=None, think=True):
            self.calls.append(("stream", think))
            for c in self.items.pop(0):
                yield c
        return _f

    def single(self):
        def _f(model, messages, ns=None):
            self.calls.append(("final", None))
            return self.items.pop(0)
        return _f


class RunawayGuardTests(unittest.TestCase):
    def setUp(self):
        # Save/restore the real network-calling functions -- these tests
        # never touch the real Ollama server.
        self._orig_stream = s._ollama_call_stream
        self._orig_final_stream = s._ollama_call_final_stream
        self._orig_call = s._ollama_call
        self._orig_final = s._ollama_call_final
        self.addCleanup(self._restore)

    def _restore(self):
        s._ollama_call_stream = self._orig_stream
        s._ollama_call_final_stream = self._orig_final_stream
        s._ollama_call = self._orig_call
        s._ollama_call_final = self._orig_final

    def _stream_events(self, script_items):
        sc = _Script(script_items)
        s._ollama_call_stream = sc.stream()
        events = list(s._chat_with_tools_stream("m1", [{"role": "user", "content": "x"}], ns="t"))
        return events, sc

    def test_clean_short_reply_untouched(self):
        events, sc = self._stream_events([[chunk(content="Hello! "), chunk(content="This is fine."), chunk(done=True)]])
        shown = "".join(e["text"] for e in events if e["type"] == "content_delta")
        self.assertEqual(shown, "Hello! This is fine.")
        self.assertNotIn("thinking_reset", [e["type"] for e in events])

    def test_long_clean_reply_kept_whole_not_blamed(self):
        long_clean = "".join("Item %d is fine. " % i for i in range(900))  # ~16200 chars, real sentences
        self.assertGreater(len(long_clean), s.CONTENT_CHAR_CAP)
        events, sc = self._stream_events([[chunk(content=long_clean), chunk(done=True)]])
        shown = "".join(e["text"] for e in events if e["type"] == "content_delta")
        self.assertIn(long_clean, shown)  # nothing silently dropped just for being long
        self.assertIn("ran long", shown)
        self.assertNotIn("degenerated", shown)
        self.assertNotIn("limit", shown.lower())  # Devin's explicit ask: no "hit your limit" framing

    def test_degenerate_content_never_shown(self):
        words = (SYNONYMS * 8).split(" ")
        chunks = [chunk(content="This part is clean. ")]
        for i in range(0, len(words), 4):
            chunks.append(chunk(content=" ".join(words[i:i + 4]) + " "))
        chunks.append(chunk(done=True))
        events, sc = self._stream_events([chunks])
        shown = "".join(e["text"] for e in events if e["type"] == "content_delta")
        self.assertIn("This part is clean.", shown)
        self.assertNotIn("zippy hasty brief", shown)  # the degenerate tail must never reach the browser
        self.assertIn("degenerated", shown)
        self.assertNotIn("limit", shown.lower())

    def test_degenerate_thinking_discarded_and_retried_clean(self):
        bad_thinking = SYNONYMS * 15  # well over THINK_NOPUNCT_RUN_MAX
        events, sc = self._stream_events([
            [chunk(thinking=bad_thinking)] + [chunk(thinking="more " + SYNONYMS) for _ in range(3)],
            [chunk(content="Clean final answer."), chunk(done=True)],
        ])
        self.assertIn("thinking_reset", [e["type"] for e in events])
        shown = "".join(e["text"] for e in events if e["type"] == "content_delta")
        self.assertEqual(shown, "Clean final answer.")
        self.assertEqual(sc.calls[-1], ("stream", False))  # retried with reasoning off

    def test_regenerate_retries_once_then_clean(self):
        sc = _Script([
            {"message": {"content": "This looks fine at first " + (SYNONYMS * 6), "thinking": ""}},
            {"message": {"content": "Clean regenerated answer.", "thinking": ""}},
        ])
        s._ollama_call = sc.single()
        reply, tool_calls, thinking = s._chat_with_tools("m1", [{"role": "user", "content": "redo it"}], ns="t")
        self.assertEqual(reply, "Clean regenerated answer.")
        self.assertNotIn("zippy hasty brief", reply)

    def test_regenerate_degenerate_both_attempts_trimmed_not_raw(self):
        sc = _Script([
            {"message": {"content": "Still clean here. " + (SYNONYMS * 6), "thinking": ""}},
            {"message": {"content": "Degenerated again. " + (SYNONYMS * 6), "thinking": ""}},
        ])
        s._ollama_call = sc.single()
        reply, tool_calls, thinking = s._chat_with_tools("m1", [{"role": "user", "content": "redo it"}], ns="t")
        self.assertNotIn("zippy hasty brief", reply)  # raw synonym chain never in the final returned reply
        self.assertIn("degenerated", reply)
        self.assertNotIn("limit", reply.lower())


if __name__ == "__main__":
    unittest.main()
