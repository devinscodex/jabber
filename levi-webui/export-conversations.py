#!/usr/bin/env python3
"""
export-conversations.py -- admin-only audit/export for Leviathan Beacon
(levi-webui) conversation history. Built 2026-10-08, Devin's ask ("we need
to be able to audit and export these conversations").

Deliberately NOT a web endpoint. conversations/ is per-identity namespaced
(CONV_DIR / <email-as-dirname> / <conv_id>.json, see server.py's
_identity_ns) specifically so one logged-in pilot user's browser session
can never reach another user's data -- adding an HTTP route here, even an
"admin-only" one, would mean bolting a second auth check onto an app whose
entire security model is "the identity on the request IS the identity
that's allowed to see this." Simpler and safer: a script run directly on
Leviathan's disk (SSH access only, same access level this was built from),
stdlib-only, no new deps, matches this project's own doctrine.

Usage (run as the `ai` user on Leviathan, from anywhere):
  python3 export-conversations.py --list
      Lists every user namespace + conversation count, no content read.
  python3 export-conversations.py --user alice_example.com
      Exports that one user's conversations to export/<ns>/*.md
  python3 export-conversations.py --all
      Exports every user's conversations to export/<ns>/*.md
  python3 export-conversations.py --all --json
      Same, but raw JSON copies instead of rendered markdown (full
      fidelity, including the "thinking" field, for real audit/compliance
      use rather than human reading).

Output lands in <script's own dir>/export/, never inside conversations/
itself (keeps raw source data untouched, exports are a separate, deletable
by-product).
"""
import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent.resolve()
CONV_DIR = HERE / "conversations"
EXPORT_DIR = HERE / "export"


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def list_namespaces() -> list[Path]:
    if not CONV_DIR.exists():
        return []
    return sorted(p for p in CONV_DIR.iterdir() if p.is_dir())


def do_list():
    namespaces = list_namespaces()
    if not namespaces:
        print("No conversations/ namespaces found.")
        return
    total = 0
    for ns_dir in namespaces:
        convs = sorted(ns_dir.glob("*.json"))
        total += len(convs)
        print(f"{ns_dir.name}: {len(convs)} conversation(s)")
    print(f"\n{len(namespaces)} user(s), {total} conversation(s) total.")


def render_markdown(conv: dict) -> str:
    lines = [
        f"# {conv.get('title', '(untitled)')}",
        "",
        f"- Conversation ID: `{conv.get('id', '?')}`",
        f"- Model: {conv.get('model', '?')}",
        f"- Created: {_ts(conv['created']) if conv.get('created') else '?'}",
        f"- Updated: {_ts(conv['updated']) if conv.get('updated') else '?'}",
        "",
        "---",
        "",
    ]
    for msg in conv.get("messages", []):
        role = msg.get("role", "?").upper()
        lines.append(f"### {role}")
        lines.append("")
        if msg.get("thinking"):
            lines.append("<details><summary>Thinking</summary>\n")
            lines.append(msg["thinking"])
            lines.append("\n</details>\n")
        lines.append(msg.get("content", ""))
        lines.append("")
    return "\n".join(lines)


def export_namespace(ns_dir: Path, as_json: bool) -> int:
    out_dir = EXPORT_DIR / ns_dir.name
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for conv_path in sorted(ns_dir.glob("*.json")):
        conv = json.loads(conv_path.read_text())
        if as_json:
            shutil.copy2(conv_path, out_dir / conv_path.name)
        else:
            md = render_markdown(conv)
            (out_dir / f"{conv_path.stem}.md").write_text(md)
        count += 1
    return count


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="List users + conversation counts, no export.")
    ap.add_argument("--user", metavar="NAMESPACE", help="Export one user's conversations (namespace dir name, e.g. alice_example.com).")
    ap.add_argument("--all", action="store_true", help="Export every user's conversations.")
    ap.add_argument("--json", action="store_true", help="Export raw JSON (full fidelity) instead of rendered markdown.")
    args = ap.parse_args()

    if args.list:
        do_list()
        return

    if args.user:
        ns_dir = CONV_DIR / args.user
        if not ns_dir.is_dir():
            print(f"No such namespace: {args.user}", file=sys.stderr)
            sys.exit(1)
        n = export_namespace(ns_dir, args.json)
        print(f"Exported {n} conversation(s) for {args.user} -> {EXPORT_DIR / args.user}/")
        return

    if args.all:
        namespaces = list_namespaces()
        total = 0
        for ns_dir in namespaces:
            n = export_namespace(ns_dir, args.json)
            total += n
            print(f"{ns_dir.name}: exported {n}")
        print(f"\nTotal: {total} conversation(s) across {len(namespaces)} user(s) -> {EXPORT_DIR}/")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
