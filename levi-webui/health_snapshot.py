#!/usr/bin/env python3
"""Writes a JSON health snapshot for the /admin/panel dashboard. Runs
every 30 min from the ai account's crontab (added 2026-10-26, Devin:
'keep an eye on the chat throughout the day' -> no alerting, just a
good dashboard). Read-only against everything it inspects; the only
write is the snapshot file itself.

Conversation-health findings are scoped to conversations touched in the
last SCAN_WINDOW_H hours, not full history -- a dashboard meant to show
"how's today going" should not keep re-flagging the same stale incident
from three weeks ago every single run. For full-history forensics, use
conversation_health_scan.py (the existing skill script) directly.
"""
import csv
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(os.path.expanduser("~/levi-webui"))
CONV_DIR = BASE / "conversations"
SNAPSHOT_PATH = BASE / "admin_health_snapshot.json"
SCAN_WINDOW_H = 24

REPETITION_MIN_CHARS = 800
REPETITION_MAX_UNIQUE_RATIO = 0.4
MAX_FLAGGED = 25

# Mirrors server.py's own NOPUNCT_RUN_MAX/_BREAK_CHARS runaway detector
# (2026-10-29, real incident: a degenerate reply's internal reasoning and,
# separately, its visible content both collapsed into a synonym-chain/
# tonal-ramble shape that the unique-word-ratio check above CANNOT catch --
# a synonym chain or a run-on sentence uses almost entirely distinct words,
# so its unique ratio stays high even while it's clearly degenerate. This
# longest-no-break-run check is the one that actually caught that incident
# live in server.py; porting the identical logic here closes the matching
# blind spot in this dashboard's own scan, which showed degenerate_repetition
# = 0 for a window that contained a real, confirmed incident.
NOPUNCT_RUN_MAX = 800
_BREAK_CHARS = ".!?\n:;|\u3002\uff01\uff1f"


def longest_nobreak_run(text):
    longest = run = 0
    for ch in text:
        if ch in _BREAK_CHARS:
            run = 0
        else:
            run += 1
            longest = max(longest, run)
    return longest

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


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def service_status():
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "levi-webui.service",
             "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        fields = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
        uptime_s = None
        ts = fields.get("ActiveEnterTimestamp", "")
        if ts:
            try:
                epoch = subprocess.run(["date", "-d", ts, "+%s"], capture_output=True, text=True, timeout=5).stdout.strip()
                if epoch:
                    uptime_s = int(time.time() - int(epoch))
            except Exception:
                pass
        return {
            "active_state": fields.get("ActiveState", "unknown"),
            "sub_state": fields.get("SubState", "unknown"),
            "main_pid": fields.get("MainPID", "unknown"),
            "uptime_s": uptime_s,
        }
    except Exception as e:
        return {"error": str(e)}


def gpu_status():
    out = run([
        "nvidia-smi",
        "--query-gpu=name,persistence_mode,power.draw,power.limit,utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ])
    if out.startswith("ERROR") or not out:
        return {"error": out or "no output"}
    parts = [p.strip() for p in out.split(",")]
    if len(parts) != 7:
        return {"error": f"unexpected nvidia-smi output: {out!r}"}
    name, persistence, draw, limit, util, mem_used, mem_total = parts
    return {
        "name": name, "persistence_mode": persistence,
        "power_draw_w": float(draw), "power_limit_w": float(limit),
        "utilization_pct": float(util),
        "memory_used_mib": float(mem_used), "memory_total_mib": float(mem_total),
    }


def ollama_status():
    """Reports EVERY model Ollama currently has loaded, not just the first.
    Devin, 2026-11-03: Jabber is about to add a second concurrently-loaded
    model (Qwen-Image, alongside the existing qwen3.6:27b chat model) --
    the old code silently discarded every model past models[0], which
    would have made the dashboard's Model card quietly wrong the moment
    a second model landed. `models` is now the real list; `loaded`/`name`/
    `cpu_fallback` are kept at the top level too, mirroring the first
    entry, so nothing reading the old singular shape breaks."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=5) as r:
            data = json.loads(r.read())
        raw = data.get("models", [])
        if not raw:
            return {"loaded": False, "models": []}
        models = [{
            "name": m.get("name"),
            "size_vram": m.get("size_vram", 0),
            "cpu_fallback": m.get("size_vram", 0) == 0,
            "context_length": m.get("details", {}).get("parameter_size"),
        } for m in raw]
        return {
            "loaded": True, "models": models,
            # back-compat singular fields, mirror the first model
            "name": models[0]["name"], "size_vram": models[0]["size_vram"],
            "cpu_fallback": models[0]["cpu_fallback"], "context_length": models[0]["context_length"],
        }
    except Exception as e:
        return {"error": str(e)}


def _when(m, d):
    """Human-readable timestamp for a flagged message -- the message's own
    ts (real per-turn epoch, populated on every message since the 2026-10
    streaming rewrite) when present, falling back to the conversation's
    own updated time for older messages saved before per-message ts
    existed. Devin's ask, 2026-11-03: flagged rows need a real "when did
    this happen", not just which conversation."""
    ts = m.get("ts") or d.get("updated")
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except Exception:
        return None


def conversation_health(window_h=SCAN_WINDOW_H):
    cutoff = time.time() - window_h * 3600
    totals = {"users_scanned": 0, "conversations_in_window": 0}
    counts = {"broken_replies": 0, "error_shaped_replies": 0, "degenerate_repetition": 0,
              "word_salad_no_breaks": 0, "thinking_degenerate": 0, "frustration": 0}
    flagged = []
    if not CONV_DIR.exists():
        return {"error": "conversations dir not found"}
    for user in sorted(os.listdir(CONV_DIR)):
        udir = CONV_DIR / user
        if not udir.is_dir():
            continue
        totals["users_scanned"] += 1
        for fname in os.listdir(udir):
            if not fname.endswith(".json"):
                continue
            path = udir / fname
            try:
                d = json.loads(path.read_text())
            except Exception:
                continue
            if d.get("updated", 0) < cutoff:
                continue
            totals["conversations_in_window"] += 1
            title = (d.get("title") or "")[:60]
            conv_id = fname.removesuffix(".json")
            for i, m in enumerate(d.get("messages", [])):
                role = m.get("role")
                text = flat(m.get("content"))
                # Every flagged row below carries the same base identity
                # fields (user/conv_id/title/msg_index/ts) so the admin
                # panel can point at the exact message, not just the
                # conversation -- "more info about where it went wrong".
                base = {"user": user, "conv_id": conv_id, "title": title, "msg_index": i, "ts": _when(m, d)}
                if role == "assistant":
                    if not text.strip():
                        counts["broken_replies"] += 1
                        if len(flagged) < MAX_FLAGGED:
                            flagged.append({**base, "kind": "broken_reply", "detail": f"msg#{i} empty"})
                    elif ERROR_SHAPED.search(text[:300]):
                        counts["error_shaped_replies"] += 1
                        if len(flagged) < MAX_FLAGGED:
                            flagged.append({**base, "kind": "error_shaped", "detail": text[:140]})
                    if len(text) > REPETITION_MIN_CHARS:
                        words = text.split()
                        if words and len(set(w.lower() for w in words)) / len(words) < REPETITION_MAX_UNIQUE_RATIO:
                            counts["degenerate_repetition"] += 1
                            if len(flagged) < MAX_FLAGGED:
                                flagged.append({**base, "kind": "degenerate_repetition", "detail": text[:140]})
                        run = longest_nobreak_run(text)
                        if run > NOPUNCT_RUN_MAX:
                            counts["word_salad_no_breaks"] += 1
                            if len(flagged) < MAX_FLAGGED:
                                flagged.append({**base, "kind": "word_salad_no_breaks", "detail": f"{run}-char run with no sentence break: " + text[:120]})
                    # Thinking is hidden from users (admin_settings.json,
                    # 2026-10-29) but still generated and stored -- scan it
                    # too, separately, since the real incident that prompted
                    # this check showed the degenerate pattern starting HERE
                    # (internal reasoning) before it ever reached the visible
                    # reply. This is now the only place this would surface.
                    think_text = flat(m.get("thinking"))
                    if len(think_text) > REPETITION_MIN_CHARS:
                        think_run = longest_nobreak_run(think_text)
                        think_words = think_text.split()
                        think_ratio = (len(set(w.lower() for w in think_words)) / len(think_words)) if think_words else 1.0
                        if think_run > NOPUNCT_RUN_MAX or think_ratio < REPETITION_MAX_UNIQUE_RATIO:
                            counts["thinking_degenerate"] += 1
                            if len(flagged) < MAX_FLAGGED:
                                flagged.append({**base, "kind": "thinking_degenerate",
                                                 "detail": f"run={think_run} unique_ratio={think_ratio:.2f}: " + think_text[:120]})
                elif role == "user":
                    if FRUSTRATION.search(text):
                        counts["frustration"] += 1
                        if len(flagged) < MAX_FLAGGED:
                            flagged.append({**base, "kind": "frustration", "detail": text[:140]})
    return {"window_hours": window_h, "totals": totals, "counts": counts, "flagged": flagged}


def activity_trend(window_h=SCAN_WINDOW_H, buckets=24):
    """Real hourly message/token counts from usage_log.jsonl's own "chat"
    records (each one a real per-turn event, not derived/estimated).
    Devin's ask, 2026-11-03: a real trend widget, not a fabricated chart --
    every number here traces back to an actual logged chat turn. Also
    buckets TOKEN volume alongside message count (not just one series) --
    Devin flagged the original message-only sparkline as "not very
    informative"; a heavy-but-infrequent conversation (e.g. a long Sage
    query session) is invisible on a pure message-count line but shows up
    plainly once token volume is its own series."""
    log_path = BASE / "usage_log.jsonl"
    now = time.time()
    cutoff = now - window_h * 3600
    bucket_w = (window_h * 3600) / buckets
    hourly = [0] * buckets
    token_hourly = [0] * buckets
    total_messages = 0
    tokens_by_ns = {}
    total_tokens = 0
    if not log_path.exists():
        return {"window_hours": window_h, "buckets": hourly, "token_buckets": token_hourly,
                "total_messages": 0, "total_tokens": 0, "top_user": None}
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("kind") != "chat":
                    continue
                ts = rec.get("ts")
                if not ts or ts < cutoff:
                    continue
                total_messages += 1
                idx = min(buckets - 1, int((ts - cutoff) / bucket_w))
                hourly[idx] += 1
                tok = int(rec.get("prompt") or 0) + int(rec.get("completion") or 0)
                token_hourly[idx] += tok
                total_tokens += tok
                ns = rec.get("ns")
                if ns:
                    tokens_by_ns[ns] = tokens_by_ns.get(ns, 0) + tok
    except Exception as e:
        return {"error": str(e)}
    top_user = max(tokens_by_ns.items(), key=lambda kv: kv[1])[0] if tokens_by_ns else None
    return {"window_hours": window_h, "buckets": hourly, "token_buckets": token_hourly,
            "total_messages": total_messages, "total_tokens": total_tokens, "top_user": top_user}


def recent_admin_actions(limit=10):
    """Last N rows of the real admin audit log (admin_allowlist/mcp-toggle/
    skill-group edits -- every _log_admin_action call in server.py), for
    the Attention feed. Tail-reads a bounded number of lines rather than
    loading the whole CSV -- this file only grows."""
    path = BASE / "admin_audit_log.csv"
    if not path.exists():
        return []
    try:
        lines = path.read_text().strip().splitlines()[-limit:]
        out = []
        for line in reversed(lines):
            parts = next(csv.reader([line]))
            if len(parts) != 4:
                continue
            ts, admin_ns, action, detail = parts
            out.append({"ts": ts, "admin": admin_ns, "action": action, "detail": detail})
        return out
    except Exception:
        return []


def main():
    snapshot = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "service": service_status(),
        "gpu": gpu_status(),
        "ollama": ollama_status(),
        "conversation_health": conversation_health(),
        "activity_trend": activity_trend(),
        # 7 daily buckets over the last 7*24h, Devin's call 2026-11-03 to
        # replace the 24h sparkline (not informative enough) with a wider
        # week view. Same function, same real usage_log.jsonl source --
        # just a different window/bucket count. Bucket boundaries are
        # rolling 24h windows ending now, not calendar-midnight-aligned;
        # the frontend labels each bucket with its own real weekday+date
        # computed from "now", so this lines up with what's shown.
        "activity_trend_7d": activity_trend(window_h=24 * 7, buckets=7),
        "recent_admin_actions": recent_admin_actions(),
    }
    tmp = SNAPSHOT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, indent=2))
    tmp.replace(SNAPSHOT_PATH)  # atomic swap -- never leaves a half-written file for the admin panel to read


if __name__ == "__main__":
    main()
