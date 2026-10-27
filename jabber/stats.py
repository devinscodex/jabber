"""jabber-stats: summarize the operator event log.

    python -m jabber.stats [--config FILE] [--data-dir DIR] [--days 7] [--raw N]

Reads <data_dir>/logs/events.jsonl. Shows usage, latency, model load stalls,
context pressure, errors and sign-in activity. Never shows message text,
because none is logged. Users are shown by username when the local account
database is present, otherwise by their opaque id.
"""
import argparse
import collections
import os
import sys
import time

from . import config as cfgmod, store, telemetry
from .auth_local import LocalAuth


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(round((len(v) - 1) * p)))]


def fmt_ms(v):
    if v is None:
        return "-"
    return f"{v / 1000:.1f}s" if v >= 1000 else f"{v:.0f}ms"


def summarize(events, names=None, out=print):
    names = names or {}
    chats = [e for e in events if e["kind"] == "chat"]
    auth = [e for e in events if e["kind"] == "auth"]
    if not events:
        out("No events in this period.")
        return
    ok = [e for e in chats if e.get("status") == "ok"]
    out(f"Chat requests: {len(chats)}  (ok {len(ok)}, "
        f"error {sum(e.get('status') == 'error' for e in chats)}, aborted {sum(e.get('status') == 'aborted' for e in chats)})")

    by_day = collections.Counter(time.strftime("%Y-%m-%d", time.localtime(e["ts"])) for e in chats)
    out("\nPer day:  " + "   ".join(f"{d} {n}" for d, n in sorted(by_day.items())))
    by_hour = collections.Counter(time.strftime("%H", time.localtime(e["ts"])) for e in chats)
    if by_hour:
        top = by_hour.most_common(3)
        out("Busiest hours: " + ", ".join(f"{h}:00 ({n})" for h, n in top))

    out("\nPer user:")
    by_user = collections.Counter(e.get("user", "?") for e in chats)
    for u, n in by_user.most_common():
        label = names.get(u, u)
        mine = [e for e in chats if e.get("user") == u]
        convs = len({e.get("conv") for e in mine if e.get("conv")})
        out(f"  {label:<24} {n:>4} requests  {convs:>3} conversations")

    out("\nPer model:")
    for m, n in collections.Counter(e.get("model", "?") for e in chats).most_common():
        mine = [e for e in ok if e.get("model") == m]
        tps = [e["tok_per_s"] for e in mine if e.get("tok_per_s")]
        out(f"  {m[:46]:<46} {n:>4}   speed p50 {pct(tps, .5) or '-'} tok/s")

    ttft = [e["ttft_ms"] for e in ok if e.get("ttft_ms") is not None]
    cont = [e["first_content_ms"] for e in ok if e.get("first_content_ms") is not None]
    total = [e["total_ms"] for e in ok if e.get("total_ms") is not None]
    out("\nLatency (ok requests):               p50        p95        max")
    for label, vals in (("first token (any)", ttft), ("first answer text", cont), ("whole reply", total)):
        out(f"  {label:<28} {fmt_ms(pct(vals, .5)):>10} {fmt_ms(pct(vals, .95)):>10} {fmt_ms(max(vals) if vals else None):>10}")
    cold = [e for e in ok if (e.get("load_ms") or 0) > 5000]
    out(f"  cold model loads (>5s): {len(cold)} of {len(ok)}"
        + (f", slowest {fmt_ms(max(e['load_ms'] for e in cold))}" if cold else ""))

    think = [e for e in ok if e.get("think")]
    nothink = [e for e in ok if e.get("think") is False]
    if think and nothink:
        out(f"  thinking on: p50 reply {fmt_ms(pct([e['total_ms'] for e in think], .5))}  "
            f"off: {fmt_ms(pct([e['total_ms'] for e in nothink], .5))}")
    elif think:
        tc = sum(e.get("thinking_chars", 0) for e in think)
        oc = sum(e.get("out_chars", 0) for e in think)
        out(f"  thinking on for all; reasoning text is {tc / max(tc + oc, 1):.0%} of everything generated")

    fills = [e["ctx_used"] / e["ctx_limit"] for e in chats if e.get("ctx_limit") and e.get("ctx_used") is not None]
    dropped = [e for e in chats if (e.get("dropped") or 0) > 0]
    if fills:
        out(f"\nContext: median fill {pct(fills, .5):.0%}, p95 {pct(fills, .95):.0%}; "
            f"{len(dropped)} request(s) had older messages left out of the model's window "
            f"({len({(e.get('user'), e.get('conv')) for e in dropped})} conversation(s))")

    errs = [e for e in chats if e.get("status") == "error"]
    if errs:
        out("\nErrors:")
        for msg, n in collections.Counter((e.get("error") or "?")[:80] for e in errs).most_common(5):
            out(f"  {n:>3}x {msg}")

    if auth:
        c = collections.Counter(e.get("action") for e in auth)
        out("\nSign-ins: " + ", ".join(f"{a} {n}" for a, n in c.most_common()))
        fails = [e for e in auth if e.get("action") in ("login_fail", "locked")]
        by_ip = collections.Counter(e.get("xff") or e.get("peer") for e in fails)
        if by_ip:
            out("  failed sign-ins by address: " + ", ".join(f"{ip} ({n})" for ip, n in by_ip.most_common(5)))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jabber-stats")
    ap.add_argument("--config")
    ap.add_argument("--data-dir")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--raw", type=int, default=0, help="also print the last N raw events")
    args = ap.parse_args(argv)
    try:
        cfg = cfgmod.load(args.config)
    except cfgmod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.data_dir:
        cfg.data_dir = args.data_dir
    tel = telemetry.Telemetry(cfg.log_path())
    events = list(tel.read(time.time() - args.days * 86400))
    names = {}
    db = cfg.data_path() / "auth.db"
    if db.exists():
        names = {store.ns_for(i): u for i, u in LocalAuth(str(cfg.data_path())).id_map().items()}
    print(f"Jabber stats, last {args.days:g} day(s), {cfg.log_path()}\n")
    summarize(events, names)
    if args.raw:
        print("\nLast events:")
        import json
        for e in events[-args.raw:]:
            print(json.dumps(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
