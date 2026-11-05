#!/usr/bin/env python3
"""Read-only scan of levi-webui/Jabber conversation JSON for signs of
trouble, on either side of the chat: broken/empty assistant replies,
error-shaped replies, degenerate/repetitive assistant replies, word-salad
replies with no sentence breaks, degenerate THINKING (hidden from users
but still stored -- a failure can live ONLY here), and frustration
language in user turns. Never writes anything. Scans FULL history, no
time cutoff -- this is the deliberate difference from the admin
dashboard's own health_snapshot.py cron job, which only looks at the
last 24h for a "how's today going" glance and will under-report a real
"review our users" ask. Detector logic mirrors server.py's own
NOPUNCT_RUN_MAX runaway guard and health_snapshot.py's thinking scan --
keep these three in sync if one changes.

Usage: scp to the target host (~/levi-webui/), then run with no args:
    python3 conversation_health_scan.py
Assumes the standard layout: ~/levi-webui/conversations/<user>_<domain>/*.json

IMPORTANT when reading output: every hit is a lead, not a verdict. Most
error-shaped and even most thinking_degenerate hits will be normal
technical content -- a user and the model debugging a formula has lots
of repeated domain terms (column names, error codes), which legitimately
lowers the unique-word ratio without being real degeneration. The
frustration regex's own "broken" trigger is a common false-positive
source specifically: ordinary prose describing something physically
broken into parts ("a fixture broken up into three pieces") matches it
with zero relation to user frustration. Read each hit's surrounding
context before reporting it as a real incident -- report raw counts
separately from what you've actually verified by reading the full
conversation. Absence of a flagged word-salad/runaway cutoff is NOT
proof the conversation is clean: a synonym-chain run interspersed with
ordinary punctuation at a normal-enough frequency can evade the
no-punctuation-run detector entirely and ship in full, uncaught.
"""
import json, os, re

BASE = os.path.expanduser("~/levi-webui/conversations")

# Mirrors server.py's/health_snapshot.py's own thresholds -- keep these
# three copies in sync if one changes.
REPETITION_MIN_CHARS = 800
REPETITION_MAX_UNIQUE_RATIO = 0.4
NOPUNCT_RUN_MAX = 800
_BREAK_CHARS = ".!?\n:;|\u3002\uff01\uff1f"

FRUSTRATION = re.compile(
    r"\b(that'?s wrong|not working|doesn'?t work|still not|not right|"
    r"no that'?s not|broken|frustrat|annoying|ugh\b|useless|"
    r"i already (said|told)|try again|that'?s not what|incorrect|"
    r"stop (doing|saying)|wtf|this is (wrong|bad)|forget it|nevermind)\b",
    re.IGNORECASE,
)
ERROR_SHAPED = re.compile(
    r"\b(error|traceback|exception|i (cannot|can't|am unable)|"
    r"something went wrong|failed to|502|500 internal|connection reset)\b",
    re.IGNORECASE,
)

def flat(content):
    if isinstance(content, list):
        return " ".join(str(x) for x in content)
    return str(content or "")

def longest_nobreak_run(text):
    longest = run = 0
    for ch in text:
        if ch in _BREAK_CHARS:
            run = 0
        else:
            run += 1
            longest = max(longest, run)
    return longest

def scan(base=BASE):
    findings = {"broken_replies": [], "error_shaped_replies": [],
                "degenerate_repetition": [], "word_salad_no_breaks": [],
                "thinking_degenerate": [], "frustration": []}
    totals = {"users": 0, "conversations": 0, "messages": 0}
    for user in sorted(os.listdir(base)):
        udir = os.path.join(base, user)
        if not os.path.isdir(udir):
            continue
        totals["users"] += 1
        for fname in os.listdir(udir):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(udir, fname)
            try:
                d = json.load(open(path))
            except Exception as e:
                findings["broken_replies"].append((user, fname, f"UNPARSEABLE: {e}"))
                continue
            totals["conversations"] += 1
            msgs = d.get("messages", [])
            totals["messages"] += len(msgs)
            title = d.get("title", "")[:60]
            for i, m in enumerate(msgs):
                role = m.get("role")
                text = flat(m.get("content"))
                if role == "assistant":
                    if not text.strip():
                        findings["broken_replies"].append((user, title, f"msg#{i} empty assistant reply"))
                    elif ERROR_SHAPED.search(text[:300]):
                        findings["error_shaped_replies"].append((user, title, f"msg#{i}: {text[:120]!r}"))
                    if len(text) > REPETITION_MIN_CHARS:
                        words = text.split()
                        if words:
                            ratio = len(set(w.lower() for w in words)) / len(words)
                            if ratio < REPETITION_MAX_UNIQUE_RATIO:
                                findings["degenerate_repetition"].append(
                                    (user, title, f"msg#{i}: {len(text)} chars, "
                                     f"unique-word-ratio={ratio:.2f} -- {text[:150]!r}"))
                        run_len = longest_nobreak_run(text)
                        if run_len > NOPUNCT_RUN_MAX:
                            findings["word_salad_no_breaks"].append(
                                (user, title, f"msg#{i}: {run_len}-char no-break run -- {text[:150]!r}"))
                    # Thinking is hidden from users but still generated and
                    # stored -- a degenerate pattern can live ONLY here if
                    # it never reaches the visible reply. Scan it with the
                    # SAME two checks (ratio + no-break-run), separately.
                    think_text = flat(m.get("thinking"))
                    if len(think_text) > REPETITION_MIN_CHARS:
                        think_run = longest_nobreak_run(think_text)
                        think_words = think_text.split()
                        think_ratio = (len(set(w.lower() for w in think_words)) / len(think_words)) if think_words else 1.0
                        if think_run > NOPUNCT_RUN_MAX or think_ratio < REPETITION_MAX_UNIQUE_RATIO:
                            findings["thinking_degenerate"].append(
                                (user, title, f"msg#{i} run={think_run} unique_ratio={think_ratio:.2f}: {think_text[:150]!r}"))
                elif role == "user":
                    if FRUSTRATION.search(text):
                        findings["frustration"].append((user, title, f"msg#{i}: {text[:150]!r}"))
    return totals, findings

if __name__ == "__main__":
    totals, findings = scan()
    print("TOTALS:", totals)
    print()
    for key, rows in findings.items():
        print(f"--- {key} ({len(rows)}) ---")
        for r in rows:
            print(" ", r)
        print()
