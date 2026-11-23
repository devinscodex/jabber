"""Real tests for the three 2026-11-23 fixes to the broad-sweep /
tool-round-cap problem (Adam, real incident -- 4-leg council + Grok
review this session, see session notes).

Covers, in the same style as test_runaway_guards.py (stdlib unittest,
scripted fake Ollama backend, no real model/network):

  1. _merge_search_ledger / _search_ledger_note in isolation -- the
     "continue has no memory" fix. Dedup, accumulation across turns,
     note formatting.
  2. A REAL two-turn replay reproducing Adam's actual shape: turn 1
     calls meg_files_search once, the reply is persisted with a
     ledger (same as the live request handler does); turn 2 ("continue")
     is driven through _chat_with_tools_stream with extra_instructions
     built the SAME way the handler builds it -- and the test asserts
     the ledger note text actually lands in the system message the
     mocked Ollama backend receives, not just that the helper functions
     return the right strings in isolation.
  3. meg_files_search's new patterns=[...] / area="all" path against
     the REAL live meg-files-mcp module and REAL file data, mirroring
     Adam's actual conversation's confirmed cases (Cogency only ever
     found in 'legal', not 'jobs'; sales correctly denied for his
     account) -- this is the one piece "tested" earlier in the session
     only via ad hoc interactive calls, formalized here so it survives.

Run directly: python3 test_search_ledger_and_multisweep.py
"""
import importlib.util
import importlib.machinery
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import server as s


def _load_meg_files_mcp():
    loader = importlib.machinery.SourceFileLoader("meg_files_mcp", "/home/ai/meg-files-mcp/meg_files_mcp.py")
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def chunk(content="", thinking="", tool_calls=None, done=False):
    return {"message": {"content": content, "thinking": thinking, "tool_calls": tool_calls or []}, "done": done}


class _CapturingStream:
    """Records the real messages list (incl. the assembled system
    message) each call receives, same pop-per-call shape _Script uses
    in test_runaway_guards.py."""

    def __init__(self, items):
        self.items = list(items)
        self.seen_messages = []

    def stream(self):
        def _f(model, messages, ns=None, think=True):
            self.seen_messages.append(messages)
            for c in self.items.pop(0):
                yield c
        return _f


class SearchLedgerUnitTests(unittest.TestCase):
    def test_no_ledger_yet_returns_none(self):
        conv = {"messages": [{"role": "user", "content": "find vendors"}]}
        self.assertIsNone(s._search_ledger_note(conv))

    def test_merge_then_note_reads_it_back(self):
        conv = {"messages": []}
        calls = [{"name": "meg_files_search", "args": {"area": "jobs", "pattern": "Cogency"}, "output": {}}]
        ledger = s._merge_search_ledger(conv, calls)
        self.assertEqual(ledger, [["jobs", "Cogency"]])
        conv["messages"].append({"role": "assistant", "content": "...", "search_ledger": ledger})
        note = s._search_ledger_note(conv)
        self.assertIn('area=jobs pattern="Cogency"', note)
        self.assertIn("do NOT repeat", note)

    def test_accumulates_and_dedupes_across_a_continue(self):
        ledger1 = [["jobs", "Cogency"]]
        conv = {"messages": [{"role": "assistant", "content": "...", "search_ledger": ledger1},
                              {"role": "user", "content": "continue"}]}
        calls_turn2 = [
            {"name": "meg_files_search", "args": {"area": "jobs", "pattern": "Cogency"}, "output": {}},  # repeat
            {"name": "meg_files_search", "args": {"area": "legal", "pattern": "alliance"}, "output": {}},  # new
        ]
        ledger2 = s._merge_search_ledger(conv, calls_turn2)
        self.assertEqual(ledger2, [["jobs", "Cogency"], ["legal", "alliance"]])  # no duplicate entry

    def test_non_search_tool_calls_ignored(self):
        conv = {"messages": []}
        calls = [{"name": "web_search", "args": {"query": "x"}, "output": {}}]
        self.assertEqual(s._merge_search_ledger(conv, calls), [])


class TwoTurnReplayTest(unittest.TestCase):
    """The real end-to-end shape: turn 1 produces a ledger, turn 2
    ("continue") must actually SEE it in the system prompt text sent
    to the model -- not just that the helper functions work alone."""

    def test_continue_turn_system_prompt_contains_ledger_note(self):
        # Turn 1's tool_calls_made (what the real round loop inside
        # _chat_with_tools_stream would have produced) -- hardcoded
        # here since this test's job is the ledger/extra_instructions
        # plumbing around that loop, not the loop itself (already
        # covered by test_runaway_guards.py).
        tool_calls_made_turn1 = [{"name": "meg_files_search",
                                   "args": {"area": "jobs", "pattern": "Cogency"},
                                   "output": {"result": "0 match(es) in jobs"}}]

        conv = {"messages": [{"role": "user", "content": "find Cogency"}]}
        ledger = s._merge_search_ledger(conv, tool_calls_made_turn1)
        self.assertTrue(ledger)
        conv["messages"].append({"role": "assistant", "content": "0 matches in jobs. Say continue to keep going.",
                                  "search_ledger": ledger, "tool_calls": tool_calls_made_turn1})
        conv["messages"].append({"role": "user", "content": "continue"})

        # --- turn 2: exact same extra_instructions assembly the real
        # handler does (server.py _chat_with_tools_stream call sites) ---
        extra = s._project_instructions(conv, "abalinski_martinenergygroup.com")
        ledger_note = s._search_ledger_note(conv)
        if ledger_note:
            extra = (extra + "\n\n" + ledger_note) if extra else ledger_note

        cap2 = _CapturingStream([[chunk(content="Checked legal area too, nothing new.", done=True)]])
        orig_stream = s._ollama_call_stream
        try:
            s._ollama_call_stream = cap2.stream()
            list(s._chat_with_tools_stream("m1", conv["messages"], ns="t", extra_instructions=extra))
        finally:
            s._ollama_call_stream = orig_stream

        self.assertEqual(len(cap2.seen_messages), 1)
        system_text = cap2.seen_messages[0][0]["content"]
        self.assertEqual(cap2.seen_messages[0][0]["role"], "system")
        self.assertIn('area=jobs pattern="Cogency"', system_text)
        self.assertIn("do NOT repeat", system_text)


class RealMegFilesMultiSweepTest(unittest.TestCase):
    """Against the REAL live module + REAL file data (not mocked) --
    mirrors Adam's actual confirmed conversation facts: Cogency only
    ever found in 'legal', sales denied for his account."""

    NS = "abalinski_martinenergygroup.com"

    @classmethod
    def setUpClass(cls):
        cls.m = _load_meg_files_mcp()

    def test_backward_compat_single_call_matches_original_wording(self):
        r = self.m.meg_files_search(ns=self.NS, area="legal", pattern="*Cogency*")
        self.assertIn("match(es) in legal", r["result"])
        self.assertIn("Cogency", r["result"])

    def test_area_all_only_searches_areas_this_account_has(self):
        r = self.m.meg_files_search(ns=self.NS, area="all", patterns=["*Cogency*"])
        self.assertIn('jobs:"*Cogency*"', r["result"])
        self.assertIn('legal:"*Cogency*"', r["result"])
        self.assertNotIn("sales:", r["result"])  # Adam has no sales access

    def test_bad_area_rejected(self):
        r = self.m.meg_files_search(ns=self.NS, area="bogus", pattern="x")
        self.assertIn("unknown area", r["error"])

    def test_patterns_capped_at_ten(self):
        r = self.m.meg_files_search(ns=self.NS, area="jobs", patterns=[f"zzz_nohit_{i}" for i in range(15)])
        sections = r["result"].split("\n\n")
        self.assertEqual(len(sections), 10)


if __name__ == "__main__":
    unittest.main()
