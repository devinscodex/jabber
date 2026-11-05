"""Conversation and project endpoints (the contract the web UI speaks).

Chat streams newline-delimited JSON events:
  {"type": "status", "status": "loading"}
  {"type": "thinking_delta", "text": ...}   {"type": "content_delta", "text": ...}
  {"type": "tool_call", "name": ..., "args": ..., "output": ...}
  {"type": "done", "model": ..., "title": ..., "ts": ...}   {"type": "error", "error": ...}
"""
import base64
import json
import queue
import re
import sys
import threading
import time

from . import backends, context, store, tools as tools_mod, usage as usage_mod, web as web_mod

MAX_MESSAGE = 200_000
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TITLE = 120
MAX_INSTRUCTIONS = 20_000

_CONV = re.compile(r"^/api/conversations(?:/([^/]+))?(?:/([a-z-]+))?$")
_PROJ = re.compile(r"^/api/projects(?:/([^/]+))?(?:/([a-z-]+))?$")


def auto_title(message: str) -> str:
    line = next((l.strip() for l in message.splitlines() if l.strip()), "New chat")
    return line[:60] + ("…" if len(line) > 60 else "")


def _summary_conv(c):
    return {"id": c["id"], "title": c["title"], "updated": c["updated"], "project_id": c.get("project_id")}


def _summary_proj(p):
    return {"id": p["id"], "name": p["name"], "created": p["created"], "has_files": bool(p.get("has_files"))}


def handle(h, method, path):
    """Returns True if the request was handled."""
    m = _CONV.match(path)
    if m:
        return _conversations(h, method, m.group(1), m.group(2))
    m = _PROJ.match(path)
    if m:
        return _projects(h, method, m.group(1), m.group(2))
    return False


def _need_user(h):
    ident = h._identity()
    if not ident:
        h._json(401, {"error": "sign in required"})
        return None
    return ident


def _load(h, ns, kind, obj_id, label):
    try:
        return h.app.store.load(ns, kind, obj_id)
    except store.NotFound:
        h._json(404, {"error": f"no such {label}"})
        return None


def _conversations(h, method, conv_id, action):
    ident = _need_user(h)
    if ident is None:
        return True
    st, ns = h.app.store, store.ns_for(ident.id)
    now = time.time()
    if conv_id is None:
        if method == "GET":
            convs = sorted((_summary_conv(c) for c in st.list(ns, "conversations")),
                           key=lambda c: c["updated"], reverse=True)
            h._json(200, {"conversations": convs})
        elif method == "POST":
            body = h._body()
            if body is None:
                return True
            pid = body.get("project_id")
            if pid:
                try:
                    st.load(ns, "projects", pid)
                except store.NotFound:
                    pid = None
            conv = {"id": store.new_id(), "title": "New chat", "created": now, "updated": now,
                    "model": h.app.cfg.default_model, "messages": [], "project_id": pid}
            st.save(ns, "conversations", conv)
            h._json(200, conv)
        else:
            h._json(405, {"error": "method not allowed"})
        return True

    if method == "GET" and action is None:
        conv = _load(h, ns, "conversations", conv_id, "conversation")
        if conv:
            h._json(200, dict(conv, context=conv_context(h.app, st, ns, conv)))
        return True
    if method == "DELETE" and action is None:
        try:
            st.delete(ns, "conversations", conv_id)
        except store.NotFound:
            return h._json(404, {"error": "no such conversation"}) or True
        h._json(200, {"deleted": conv_id})
        return True
    if method != "POST" or action is None:
        h._json(404, {"error": "not found"})
        return True

    conv = _load(h, ns, "conversations", conv_id, "conversation")
    if conv is None:
        return True
    body = h._body()
    if body is None:
        return True
    if action == "rename":
        title = str(body.get("title") or "").strip()
        if not title:
            return h._json(400, {"error": "title cannot be blank"}) or True
        conv["title"] = title[:MAX_TITLE]
    elif action == "set-project":
        pid = body.get("project_id")
        if pid is not None:
            try:
                st.load(ns, "projects", pid)
            except store.NotFound:
                return h._json(404, {"error": "no such project"}) or True
        conv["project_id"] = pid
    elif action == "attachment-note":
        names = body.get("filenames")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names) or len(names) > 200:
            return h._json(400, {"error": 'expected {"filenames": ["a.txt", ...]}'}) or True
        names = [n[:200] for n in names]
        conv["messages"].append({"role": "user", "ts": time.time(), "attachment": True,
                                 "content": "\U0001F4CE Attached file" + ("s" if len(names) != 1 else "") + ": " + ", ".join(names)})
    elif action == "truncate":
        try:
            idx = int(body["index"])
        except (KeyError, TypeError, ValueError):
            return h._json(400, {"error": "expected {index: <int>}"}) or True
        if not 0 <= idx <= len(conv["messages"]):
            return h._json(400, {"error": "index out of range"}) or True
        conv["messages"] = conv["messages"][:idx]
    elif action == "chat":
        return _chat(h, st, ns, conv, body)
    elif action == "regenerate":
        return _regenerate(h, st, ns, conv)
    else:
        h._json(404, {"error": "not found"})
        return True
    conv["updated"] = now
    st.save(ns, "conversations", conv)
    h._json(200, conv)
    return True


def _projects(h, method, proj_id, action):
    ident = _need_user(h)
    if ident is None:
        return True
    st, ns = h.app.store, store.ns_for(ident.id)
    if proj_id is None:
        if method == "GET":
            projs = sorted((_summary_proj(p) for p in st.list(ns, "projects")), key=lambda p: p["created"])
            h._json(200, {"projects": projs})
        elif method == "POST":
            body = h._body()
            if body is None:
                return True
            name = str(body.get("name") or "").strip()
            if not name:
                return h._json(400, {"error": "expected {name}"}) or True
            proj = {"id": store.new_id(), "name": name[:MAX_TITLE], "created": time.time(), "instructions": ""}
            st.save(ns, "projects", proj)
            h._json(200, proj)
        else:
            h._json(405, {"error": "method not allowed"})
        return True
    if method == "GET" and action is None:
        proj = _load(h, ns, "projects", proj_id, "project")
        if proj:
            h._json(200, proj)
        return True
    if method == "DELETE" and action is None:
        try:
            st.delete(ns, "projects", proj_id)
        except store.NotFound:
            return h._json(404, {"error": "no such project"}) or True
        h.app.files.purge(ns, proj_id)
        for c in st.list(ns, "conversations"):
            if c.get("project_id") == proj_id:
                c["project_id"] = None
                st.save(ns, "conversations", c)
        h._json(200, {"deleted": proj_id})
        return True
    if method == "POST" and action == "update":
        proj = _load(h, ns, "projects", proj_id, "project")
        if proj is None:
            return True
        body = h._body()
        if body is None:
            return True
        if "name" in body:
            name = str(body["name"] or "").strip()
            if not name:
                return h._json(400, {"error": "name cannot be blank"}) or True
            proj["name"] = name[:MAX_TITLE]
        if "instructions" in body:
            proj["instructions"] = str(body["instructions"] or "").strip()[:MAX_INSTRUCTIONS]
        st.save(ns, "projects", proj)
        h._json(200, proj)
        return True
    h._json(404, {"error": "not found"})
    return True


def capability_block(app, ns, conv, model_name):
    tools = []
    pid = conv.get("project_id")
    if app.cfg.tools and pid and app.files.has_files(ns, pid) and app.groups.allowed(ns, "files"):
        tools += ["list_project_files", "read_project_file", "search_project_files", "read_project_image",
                  "crop_image", "image_metadata"]
    if conv.get("web") and app.cfg.web and app.groups.allowed(ns, "web"):
        tools += ["web_search", "fetch_page"]
    lines = [f"About this deployment: you are the model \"{model_name}\", running locally through Jabber"
             + (f" on {app.cfg.host_note}" if app.cfg.host_note else "")
             + ". You are not running in a vendor cloud, and you should not describe infrastructure you cannot know.",
             "Tools available in this conversation: " + (", ".join(tools) if tools else "none") + ".",
             "Rules: (1) If a request needs a tool you do not have (for example web search or reverse image search), "
             "say so in one line, give what you can from what you can actually see, and stop; do not guess your way "
             "around the gap. (2) Label every guess as a guess with a confidence (low/medium/high); never present an "
             "unverified identification as a finding. (3) A file's name is evidence: read it literally before "
             "brainstorming. (4) Keep reasoning short and proportionate to the question."]
    if pid and "read_project_image" in tools:
        lines.append("(5) For any question about an uploaded image, look at it with read_project_image first; use crop_image "
                     "to zoom on small details such as text, logos and signs, and image_metadata for camera, date and GPS data.")
    if "web_search" in tools:
        lines.append("(6) To identify something unknown, build specific search queries from what you see (visible text, "
                     "distinctive features, the file name), and if the first search fails, try a different query. "
                     "Open the best result with fetch_page and cite the URL you relied on.")
    lines.append("(7) Write answers in Simplified Technical English: short sentences, active voice, simple present tense, "
                 "one meaning per term, no idioms or filler. Keep every command, path, number and name exact.")
    return "\n".join(lines)


def system_messages(app, st, ns, conv):
    system = app.cfg.system_prompt
    model_name = conv.get("model") or app.cfg.default_model or "a local model"
    system = (system + "\n\n" if system else "") + capability_block(app, ns, conv, model_name)
    if conv.get("project_id"):
        try:
            instr = st.load(ns, "projects", conv["project_id"]).get("instructions", "")
        except store.NotFound:
            instr = ""
        if instr:
            system = (system + "\n\n" if system else "") + "Project instructions:\n" + instr
    pid = conv.get("project_id")
    if pid and app.cfg.tools and app.files.has_files(ns, pid) and app.groups.allowed(ns, "files"):
        top = ", ".join(app.files.top_listing(ns, pid)) or "(empty)"
        system = (system + "\n\n" if system else "") + (
            "This conversation's project has uploaded files. Top level: " + top + ". Use the tools list_project_files, "
            "read_project_file and search_project_files to read them instead of guessing. Documents (PDF, Word, Excel, "
            "PowerPoint) are stored as '<name>.extracted.txt'. Cite the file name when you use its content.")
    if conv.get("web") and app.cfg.web and app.groups.allowed(ns, "web"):
        system = (system + "\n\n" if system else "") + web_mod.WEB_NOTE
    return [{"role": "system", "content": system}] if system else []


def build_messages(app, st, ns, conv, history):
    """Returns (messages for the model, context info) or (None, None) if the
    newest message alone cannot fit the model's window."""
    sysm = system_messages(app, st, ns, conv)
    plain = []
    for m in history:
        mm = {"role": m["role"], "content": m["content"]}
        if m.get("images"):
            mm["images"] = [i["data"] for i in m["images"]]
        plain.append(mm)
    pid = conv.get("project_id")
    if pid and plain and plain[-1]["role"] == "user":
        seen = []
        for m in reversed(history):
            for pth in m.get("viewed") or []:
                if pth not in seen:
                    seen.append(pth)
        again = []
        for pth in seen[:2]:
            try:
                data, _ = tools_mod.image_b64(app.files, ns, pid, pth)
            except Exception:
                continue
            again.append((pth, data))
        if again:
            plain[-1] = dict(plain[-1], images=(plain[-1].get("images") or []) + [d for _, d in again],
                             content=plain[-1]["content"] + "\n\n(Images you viewed earlier are attached again: "
                             + ", ".join(p for p, _ in again) + ")")
    fitted = context.fit(sysm, plain, app.cfg.context_tokens)
    if fitted is None:
        return None, None
    kept, info = fitted
    return sysm + kept, info


def conv_context(app, st, ns, conv):
    sysm = system_messages(app, st, ns, conv)
    plain = [{"role": m["role"], "content": m["content"], "images": m.get("images")} for m in conv["messages"]]
    fitted = context.fit(sysm, plain, app.cfg.context_tokens)
    return fitted[1] if fitted else {"used": 0, "limit": app.cfg.context_tokens, "dropped": 0}


def model_options(app):
    return {"num_ctx": app.cfg.context_tokens} if app.cfg.context_tokens else None


MAX_TOOL_ROUNDS = 6

# Runaway-generation guards (2026-11-04, ported from Leviathan's levi-webui
# after 3 rounds of real-incident fixes there -- see that project's
# decision-ledger. Ported here now so this codebase doesn't regress on
# crash-safety when it eventually replaces levi-webui as the real deploy
# (Devin's explicit ask, "mainly for the new auth method"). Same proven
# constants/logic, not re-derived: a synonym-chain degenerate-generation
# failure mode keeps recurring on local reasoning models and needs this
# exact two-layer defense (pattern detection, not just a length cap) to
# actually catch it.
CONTENT_CHAR_CAP = 12000  # hard backstop: a reply longer than this is a runaway, not an answer
NOPUNCT_RUN_MAX = 800     # chars with no sentence/row break at all = word salad. Real data (levi-webui,
                          # 268 saved replies): longest legit run < 500; the 4 known runaways ran 1046-4791.
THINK_NOPUNCT_RUN_MAX = 2400  # wider than content's -- legit reasoning runs longer unbroken than prose.
                               # Starting assumption (3x), not measured against this model/config's own
                               # thinking traces -- replay real history before trusting a tighter number.
_BREAK_CHARS = ".!?\n:;|\u3002\uff01\uff1f"


def _last_break(text: str) -> int:
    return max(text.rfind(c) for c in _BREAK_CHARS)


def _runaway_cut(text: str, nopunct_limit: int = NOPUNCT_RUN_MAX):
    """Index to cut `text` at when it has run nopunct_limit+ chars without a single sentence or row
    break (. ! ? newline : ; | and CJK stops), else None. Tables, lists and code break constantly."""
    limit = nopunct_limit * (4 if text.count("```") % 2 == 1 else 1)
    last = _last_break(text)
    return last + 1 if len(text) - last - 1 > limit else None


def _trim_runaway(text: str) -> str:
    """Drop the garbage tail: back to the last real sentence/row break (empty if there never was one)."""
    text = text[:CONTENT_CHAR_CAP]
    cut = _runaway_cut(text)
    if cut is None:
        cut = _last_break(text) + 1
    return text[:cut].rstrip()


def _looks_repeating(text: str) -> bool:
    """True when the last 200 characters already appear 3+ times in the recent text."""
    if len(text) < 1200:
        return False
    return text[-4000:].count(text[-200:]) >= 3


def _thinking_degenerate(text: str) -> bool:
    """Live guard for the model's internal reasoning text. Raw length caps (max_thinking_chars)
    alone miss a degenerate run shorter than the cap -- this catches the actual pattern."""
    return (_runaway_cut(text, THINK_NOPUNCT_RUN_MAX) is not None
            or _looks_repeating(text))


class _ContentGate:
    """Sentence-granular holdback for streamed content. The visible answer must NEVER show
    degenerate text, not just get caught fast -- catch-after-it's-on-screen isn't good enough.

    Text is only released up to the last real sentence/row break; anything after that stays
    held. A degenerate run has no break chars by definition, so it sits in the held tail and is
    NEVER released -- on a pattern trip the held tail is simply dropped, never shown. Cost: the
    reply displays in sentence-sized chunks instead of a smooth per-token stream (accepted
    tradeoff -- trust over smoothness)."""

    def __init__(self):
        self.text = ""   # everything received this round, held + released
        self.sent = 0     # how much of self.text has been released already
        self._since = 0   # chars since the last _looks_repeating check (bounds its O(n) cost)

    def feed(self, piece: str):
        """Returns (text_to_release_now, trip_reason). trip_reason is None (keep reading),
        'pattern' (genuine degeneration -- discard the unshown tail) or 'cap' (hit
        CONTENT_CHAR_CAP with a CLEAN pattern -- just a long legitimate answer; caller should
        flush everything, not discard it). Checked in that order: a length+pattern double-hit
        is still real degeneration, not a false "just long" case."""
        self.text += piece
        self._since += len(piece)
        check_repeat = self._since >= 400
        if check_repeat:
            self._since = 0
        if _runaway_cut(self.text) is not None or (check_repeat and _looks_repeating(self.text)):
            return "", "pattern"
        if len(self.text) > CONTENT_CHAR_CAP:
            return "", "cap"
        release_to = _last_break(self.text) + 1
        if release_to > self.sent:
            out = self.text[self.sent:release_to]
            self.sent = release_to
            return out, None
        return "", None

    def flush(self) -> str:
        """Stream ended cleanly, or a 'cap' trip -- release whatever's still held, even without
        a trailing break (a legitimate reply can end mid-sentence on length alone)."""
        out = self.text[self.sent:]
        self.sent = len(self.text)
        return out

    def trip(self) -> str:
        """'pattern' trip fired -- the SAFE (already-release-worthy) content only. The held
        tail (never sent anywhere) is simply not included -- that's the whole point."""
        return _trim_runaway(self.text)


def run_web(app, ns, name, args):
    """(result_text, None, label). Logs counts and provider only, never the query or URL."""
    args = args if isinstance(args, dict) else {}
    if not app.cfg.web:
        return "error: web access is turned off", None, ""
    if not app.groups.allowed(ns, "web"):
        return "error: web search is not enabled for your account", None, ""
    try:
        if name == "web_search":
            q = str(args.get("query") or "")
            res, provider = web_mod.search(app.cfg, q, args.get("max_results") or 5)
            app.telemetry.event("web", user=ns, tool="search", provider=provider, results=len(res))
            if not res:
                return "no results", None, q[:60]
            return "\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in res), None, q[:60]
        url = str(args.get("url") or "")
        text = web_mod.fetch_page(url, args.get("offset") or 0)
        app.telemetry.event("web", user=ns, tool="fetch", chars=len(text))
        return text, None, urlparse_host(url)
    except web_mod.WebError as e:
        app.telemetry.event("web", user=ns, tool=name, error=str(e)[:80])
        return f"error: {e}", None, str(args.get("query") or args.get("url") or "")[:60]
    except (ValueError, TypeError):
        return "error: invalid arguments", None, ""


def urlparse_host(url):
    from urllib.parse import urlparse
    return (urlparse(url).hostname or url)[:60]


def generate(app, ns, conv, model, msgs, think, stop, viewed=None):
    """Yields ("chunk", Chunk) and ("tool", {"name", "label"}). Runs the model; if it asks to use
    project-file tools, runs them (read-only, scoped to this project) and continues, up to MAX_TOOL_ROUNDS."""
    pid = conv.get("project_id")
    use_files = bool(app.cfg.tools and pid and app.files.has_files(ns, pid) and app.groups.allowed(ns, "files"))
    use_web = bool(app.cfg.web and conv.get("web") and app.groups.allowed(ns, "web"))
    use_tools = use_files or use_web
    work = list(msgs)
    budget = max(4000, int((app.cfg.context_tokens or 32768) * 4 * 0.25))
    for rnd in range(MAX_TOOL_ROUNDS + 1):
        offer = ((tools_mod.SCHEMAS if use_files else []) + (web_mod.SCHEMAS if use_web else [])) if use_tools and rnd < MAX_TOOL_ROUNDS else None
        calls, text, gen = [], "", None
        gate = _ContentGate()
        try:
            try:
                gen = app.backend().chat_stream(model, work, tools=offer, options=model_options(app), think=think)
                think_chars, think_since, thinking_acc, capped, cap_tag = 0, 0, "", False, None
                content_trip = None
                for chunk in gen:
                    if stop.is_set():
                        return
                    calls += chunk.tool_calls
                    text += chunk.content
                    thinking_acc += chunk.thinking
                    think_chars += len(chunk.thinking)
                    think_since += len(chunk.thinking)
                    if chunk.content:
                        out, content_trip = gate.feed(chunk.content)
                        if content_trip:
                            break
                        chunk.content = out
                    yield ("chunk", chunk)
                    cap = app.cfg.max_thinking_chars
                    think_degenerate = False
                    if think_since >= 400:
                        think_since = 0
                        think_degenerate = not gate.text and not calls and _thinking_degenerate(thinking_acc)
                    if think and not gate.text and not calls and ((cap and think_chars > cap) or think_degenerate):
                        capped, cap_tag = True, ("thinking_pattern" if think_degenerate else "thinking_time")
                        break
                if content_trip:
                    # 2026-11-04, same real-incident fix as levi-webui (see
                    # that project's decision-ledger) -- content must NEVER
                    # visibly show degenerate text, and a merely-long clean
                    # reply must NOT be discarded or blamed as degenerate.
                    gen.close()
                    if content_trip == "pattern":
                        safe = gate.trip()
                        notice = "\n\nThat answer degenerated into repeated/incoherent text, so it was stopped here. Send a follow-up and I'll try again."
                        yield ("chunk", backends.Chunk(content=notice))
                        text = safe + notice
                        yield ("note", "content_pattern")
                    else:  # "cap" -- clean pattern, just long; keep ALL of it
                        rest = gate.flush()
                        notice = "\n\nThis answer ran long, so I'm pausing here -- say \"continue\" and I'll pick up where I left off."
                        yield ("chunk", backends.Chunk(content=rest + notice))
                        text = gate.text + notice
                        yield ("note", "content_cap")
                    return
                if capped:
                    gen.close()
                    yield ("note", cap_tag)
                    nudge = ("Your last reasoning degenerated into repeated/incoherent text -- discarded, not "
                             "shown. Answer the question now, cleanly and concisely, using only what you "
                             "already know and can see.") if cap_tag == "thinking_pattern" else (
                             "(Reasoning is running long. Answer now using only what you already know and can "
                             "see. State clearly what is a guess and what you could not check.)")
                    work.append({"role": "user", "content": nudge})
                    gen = app.backend().chat_stream(model, work, tools=None, options=model_options(app), think=False)
                    gate2 = _ContentGate()
                    for chunk in gen:
                        if stop.is_set():
                            return
                        if chunk.content:
                            out, trip2 = gate2.feed(chunk.content)
                            if trip2:
                                break
                            chunk.content = out
                        yield ("chunk", chunk)
                    tail2 = gate2.flush()
                    if tail2:
                        yield ("chunk", backends.Chunk(content=tail2))
                    return
            except backends.BackendError as e:
                if offer and rnd == 0 and "tool" in str(e).lower():
                    use_tools, offer = False, None   # this model cannot use tools; carry on without
                    gen = app.backend().chat_stream(model, work, options=model_options(app), think=think)
                    gate3 = _ContentGate()
                    for chunk in gen:
                        if stop.is_set():
                            return
                        if chunk.content:
                            out, trip3 = gate3.feed(chunk.content)
                            if trip3:
                                break
                            chunk.content = out
                        yield ("chunk", chunk)
                    tail3 = gate3.flush()
                    if tail3:
                        yield ("chunk", backends.Chunk(content=tail3))
                    return
                raise
        finally:
            if gen is not None:
                gen.close()
        # Loop ended cleanly (no content_trip, no capped break) -- flush
        # whatever the gate is still holding (most replies end mid-
        # sentence with no trailing break char at all; without this the
        # held tail -- sometimes the WHOLE reply -- would never reach
        # the browser). Real regression caught by test_chat_streams_and_persists.
        tail = gate.flush()
        if tail:
            yield ("chunk", backends.Chunk(content=tail))
        if not calls:
            return
        work.append({"role": "assistant", "content": text, "tool_calls": calls})
        images = []
        for c in calls:
            if c.get("name") in ("web_search", "fetch_page"):
                result, img, label = run_web(app, ns, c.get("name"), c.get("args"))
            else:
                result, img, label = tools_mod.run(app.files, ns, pid, c.get("name", ""), c.get("args"), budget)
            result = result[:max(500, budget)]
            budget -= len(result)
            work.append({"role": "tool", "content": result, "tool_name": c.get("name", "")})
            yield ("tool", {"name": c.get("name", ""), "label": label})
            if img:
                images.append(img)
                if viewed is not None and label not in viewed:
                    viewed.append(label)
        if images:
            work.append({"role": "user", "content": "(image requested from the project files)", "images": images})
        if budget <= 0:
            work.append({"role": "user", "content": "(file-reading budget used up; answer with what you have)"})
            use_tools = False


def _regenerate(h, st, ns, conv):
    if not conv["messages"] or conv["messages"][-1]["role"] != "assistant":
        return h._json(400, {"error": "nothing to regenerate -- last turn isn't an assistant reply"}) or True
    cfg = h.app.cfg
    model = conv.get("model") or cfg.default_model
    if not model or (cfg.model_allowlist and model not in cfg.model_allowlist):
        return h._json(400, {"error": "invalid model"}) or True
    msgs, info = build_messages(h.app, st, ns, conv, conv["messages"][:-1])
    if msgs is None:
        return h._json(413, {"error": "the last message is too large for the model's context window"}) or True
    content, thinking = "", ""
    t0 = time.time()
    try:
        for kind, val in generate(h.app, ns, conv, model, msgs, h.app.cfg.think, threading.Event()):
            if kind == "chunk":
                content += val.content
                thinking += val.thinking
    except backends.BackendError as e:
        h.app.telemetry.event("chat", action="regenerate", user=store.ns_for(h._identity().id), model=model,
                              status="error", error=str(e)[:200], total_ms=round((time.time() - t0) * 1000))
        return h._json(502, {"error": str(e)}) or True
    h.app.telemetry.event("chat", action="regenerate", user=store.ns_for(h._identity().id), model=model,
                          status="ok", total_ms=round((time.time() - t0) * 1000), out_chars=len(content),
                          ctx_used=info["used"], ctx_limit=info["limit"], dropped=info["dropped"])
    ts = time.time()
    msg = {"role": "assistant", "content": content, "ts": ts}
    if thinking:
        msg["thinking"] = thinking
    conv["messages"][-1] = msg
    conv["updated"] = ts
    st.save(ns, "conversations", conv)
    h._json(200, {"reply": content, "model": model, "thinking": thinking or None, "ts": ts})
    return True


def _images(raw):
    """Validated [{data, mime}] or an error string."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_IMAGES:
        return f"at most {MAX_IMAGES} images per message"
    out = []
    for img in raw:
        if not isinstance(img, dict) or not isinstance(img.get("data"), str):
            return 'images must be [{"data": "<base64>", "mime": "image/png"}]'
        mime = img.get("mime") if isinstance(img.get("mime"), str) and img["mime"].startswith("image/") else "image/png"
        try:
            n = len(base64.b64decode(img["data"], validate=True))
        except (ValueError, TypeError):
            return "an image is not valid base64"
        if n > MAX_IMAGE_BYTES:
            return f"an image exceeds {MAX_IMAGE_BYTES} bytes"
        out.append({"data": img["data"], "mime": mime})
    return out


def _chat(h, st, ns, conv, body):
    cfg = h.app.cfg
    text = body.get("message")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE:
        return h._json(400, {"error": 'expected {"message": "..."}'}) or True
    images = _images(body.get("images"))
    if isinstance(images, str):
        return h._json(400, {"error": images}) or True
    model = body.get("model") if cfg.model_choice else cfg.default_model
    model = model or conv.get("model") or cfg.default_model
    if not isinstance(model, str) or not model or (cfg.model_allowlist and model not in cfg.model_allowlist):
        return h._json(400, {"error": "invalid model"}) or True

    if "web" in body:
        conv["web"] = bool(body["web"]) and cfg.web and h.app.groups.allowed(ns, "web")
    conv["model"] = model
    if conv["title"] == "New chat":
        conv["title"] = auto_title(text)
    hits = usage_mod.pii_counts(text)
    if hits:
        h.app.telemetry.event("pii", user=ns, counts=hits)
    turn = {"role": "user", "content": text, "ts": time.time()}
    if images:
        turn["images"] = images
    conv["messages"].append(turn)

    msgs, info = build_messages(h.app, st, ns, conv, conv["messages"])
    if msgs is None:
        conv["messages"].pop()
        return h._json(413, {"error": "that message is too large for the model's context window; shorten it"}) or True
    t_start = time.time()
    marks = {"first": None, "content": None}
    stats = {}
    think = h.app.cfg.think
    viewed, capped = [], False

    q = queue.Queue()
    stop = threading.Event()

    def pump():
        try:
            for item in generate(h.app, ns, conv, model, msgs, think, stop, viewed):
                q.put(item)
            q.put(("end", None))
        except backends.BackendError as e:
            q.put(("error", str(e)))
        except Exception as e:
            sys.stderr.write("chat error: %r\n" % (e,))
            q.put(("error", "internal error"))

    threading.Thread(target=pump, daemon=True).start()
    h.send_response(200)
    h.send_header("Content-Type", "application/x-ndjson")
    h.send_header("X-Accel-Buffering", "no")
    h._headers_common()
    h.end_headers()

    gone = False
    content, thinking, error = "", "", None
    tools_used = []

    def emit(obj):
        nonlocal gone
        if gone:
            return
        try:
            h.wfile.write((json.dumps(obj) + "\n").encode())
            h.wfile.flush()
        except OSError:
            gone = True
            stop.set()

    emit({"type": "status", "status": "loading"})
    while True:
        try:
            kind, val = q.get(timeout=10)
        except queue.Empty:
            if not gone:
                try:
                    h.wfile.write(b"\n")
                    h.wfile.flush()
                except OSError:
                    gone = True
                    stop.set()
            continue
        if kind == "chunk":
            if (val.thinking or val.content) and marks["first"] is None:
                marks["first"] = time.time()
            if val.stats:
                stats = {"prompt_tokens": val.stats.get("prompt_tokens") or stats.get("prompt_tokens"),
                         "completion_tokens": (stats.get("completion_tokens") or 0) + (val.stats.get("completion_tokens") or 0),
                         "eval_ms": (stats.get("eval_ms") or 0) + (val.stats.get("eval_ms") or 0),
                         "load_ms": max(stats.get("load_ms") or 0, val.stats.get("load_ms") or 0)}
            if val.thinking:
                thinking += val.thinking
                emit({"type": "thinking_delta", "text": val.thinking})
            if val.content:
                if marks["content"] is None:
                    marks["content"] = time.time()
                content += val.content
                emit({"type": "content_delta", "text": val.content})
            if gone:
                break
        elif kind == "note":
            capped = True
            # 2026-11-04 -- distinct wording per real cause, ported from
            # levi-webui's same fix: avoid "limit" framing (Devin's
            # explicit ask) and never call a merely-long clean reply
            # the same thing as genuine degeneration.
            note_text = {
                "thinking_time": "Reasoning ran long, so the model was told to answer with what it has.",
                "thinking_pattern": "Reasoning started repeating itself, so it was stopped and the model was told to answer with what it has.",
                "content_pattern": "That answer degenerated into repeated/incoherent text, so it was stopped.",
                "content_cap": "That answer ran long, so it's continuing in a follow-up.",
            }.get(val, "Reasoning ran long, so the model was told to answer with what it has.")
            emit({"type": "note", "text": note_text})
        elif kind == "tool":
            tools_used.append(val)
            emit({"type": "tool", "name": val["name"], "label": val["label"]})
        elif kind == "error":
            error = val
            break
        else:
            break
    stop.set()

    ts = time.time()
    if content or thinking:
        msg = {"role": "assistant", "content": content, "ts": ts}
        if thinking:
            msg["thinking"] = thinking
        if tools_used:
            msg["tools"] = tools_used
        if viewed:
            msg["viewed"] = viewed
        conv["messages"].append(msg)
    conv["updated"] = ts
    st.save(ns, "conversations", conv)
    ms = lambda t: round((t - t_start) * 1000) if t else None
    eval_ms = stats.get("eval_ms")
    done_tokens = stats.get("completion_tokens")
    h.app.telemetry.event(
        "chat", action="chat", user=ns, conv=conv["id"][:8], model=model, think=think,
        status="error" if error else ("aborted" if gone else "ok"), error=(error or "")[:200] or None,
        queued_ms=None, ttft_ms=ms(marks["first"]), first_content_ms=ms(marks["content"]), total_ms=ms(ts),
        ctx_used=info["used"], ctx_limit=info["limit"], dropped=info["dropped"],
        prompt_tokens=stats.get("prompt_tokens"), completion_tokens=done_tokens, load_ms=stats.get("load_ms"),
        tok_per_s=round(done_tokens / (eval_ms / 1000), 1) if done_tokens and eval_ms else None,
        out_chars=len(content), thinking_chars=len(thinking), images=len(images) or None,
        tool_calls=len(tools_used) or None, thinking_capped=capped or None)
    if error:
        emit({"type": "error", "error": error})
    else:
        emit({"type": "done", "model": model, "title": conv["title"], "ts": ts, "context": info})
    return True
