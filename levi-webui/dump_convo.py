#!/usr/bin/env python3
import json, sys

path = sys.argv[1]
d = json.load(open(path))
print("title:", d.get("title"))
print("updated:", d.get("updated"))
for i, m in enumerate(d.get("messages", [])):
    c = m.get("content")
    if isinstance(c, list):
        c = " ".join(str(x) for x in c)
    print(f"--- msg{i} [{m.get('role')}] ---")
    print(c)
    th = m.get("thinking")
    if th:
        print(f"  [thinking]: {th[:300]}")
    print()
