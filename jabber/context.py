"""Context-window fitting.

The full conversation always stays on disk. Only what is sent to the model is
limited: system text plus as many of the most recent messages as fit in the
budget. Token counts are estimates (4 characters per token), deliberately
dependency-free and a little pessimistic.
"""
IMAGE_TOKENS = 1100
PER_MESSAGE = 4


def est_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def msg_tokens(m: dict) -> int:
    return est_tokens(m.get("content") or "") + PER_MESSAGE + IMAGE_TOKENS * len(m.get("images") or [])


def fit(system_msgs: list, history: list, limit: int, reply_reserve: float = 0.25):
    """Returns (kept_history, info) or None if the newest message alone cannot fit.

    info = {"used": est. prompt tokens, "limit": limit, "dropped": messages left out}.
    limit <= 0 disables fitting (everything is sent).
    """
    fixed = sum(msg_tokens(m) for m in system_msgs)
    if limit <= 0:
        used = fixed + sum(msg_tokens(m) for m in history)
        return list(history), {"used": used, "limit": 0, "dropped": 0}
    budget = int(limit * (1 - reply_reserve))
    kept, used = [], fixed
    for m in reversed(history):
        t = msg_tokens(m)
        if used + t > budget:
            if not kept:
                return None
            break
        kept.append(m)
        used += t
    kept.reverse()
    while kept and kept[0]["role"] == "assistant" and len(kept) > 1:
        used -= msg_tokens(kept.pop(0))
    return kept, {"used": used, "limit": limit, "dropped": len(history) - len(kept)}
