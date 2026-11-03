"""Cheap, read-only looks into agent transcripts for the dashboard.

Transcripts can be many megabytes, so nothing here reads one whole more than once:
the first lines give the model and start time, the tail gives the latest tool calls,
and output tokens are summed incrementally by byte offset. Everything returned is
untrusted text and is clipped by the caller.
"""
from datetime import datetime
import hashlib
import json
import os
import re
from pathlib import Path

TAIL_BYTES = 700_000
HEAD_LINES = 300
HEAD_BYTES = 4_000_000
# a call's own description (Bash and PowerShell carry one written for people) comes before its raw command
ARG_KEYS = ("description", "command", "file_path", "path", "pattern", "query", "url", "prompt", "skill", "name")

_head_cache = {}
_token_cache = {}
_tail_cache = {}


def head_info(path):
    """Model and first timestamp of a transcript, read once from its first lines."""
    key = str(path)
    if key in _head_cache:
        return _head_cache[key]
    info = {"model": "", "start": ""}
    try:
        with open(path, "rb") as f:
            read = 0
            for _ in range(HEAD_LINES):
                line = f.readline()
                read += len(line)
                if not line or read > HEAD_BYTES:
                    break
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not info["start"] and isinstance(d, dict) and d.get("timestamp"):
                    info["start"] = d["timestamp"]
                m = (d.get("message") or {}) if isinstance(d, dict) else {}
                if isinstance(m, dict) and m.get("model"):
                    info["model"] = m["model"]
                    break
    except OSError:
        return info
    if info["model"]:
        _head_cache[key] = info
    return info


def output_tokens(path):
    """Output tokens an agent has produced so far (last usage seen per message id), summed incrementally."""
    key = str(path)
    st = _token_cache.setdefault(key, {"offset": 0, "by_msg": {}, "ctx": 0, "tools": 0})
    try:
        size = os.stat(path).st_size
    except OSError:
        return 0
    if size < st["offset"]:
        st["offset"], st["by_msg"], st["ctx"], st["tools"] = 0, {}, 0, 0
    if size > st["offset"]:
        try:
            with open(path, "rb") as f:
                f.seek(st["offset"])
                while True:
                    line = f.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    st["offset"] += len(line)
                    if b'"type":"tool_use"' in line:
                        st["tools"] += line.count(b'"type":"tool_use"')
                    if b'"output_tokens"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    m = d.get("message") if isinstance(d, dict) else None
                    if isinstance(m, dict) and isinstance(m.get("usage"), dict) and d.get("type") == "assistant":
                        u = m["usage"]
                        st["by_msg"][m.get("id") or len(st["by_msg"])] = int(u.get("output_tokens") or 0)
                        # the context the agent has built up: what the run summary later reports as its tokens
                        st["ctx"] = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("output_tokens") or 0)
        except OSError:
            pass
    return sum(st["by_msg"].values())


def usage_now(path):
    """(output tokens so far, context size now, tool calls so far) for an agent's transcript, summed incrementally."""
    out = output_tokens(path)
    st = _token_cache.get(str(path)) or {}
    return out, st.get("ctx", 0), st.get("tools", 0)


def _brief(name, inp):
    if isinstance(inp, dict):
        for k in ARG_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return name + ": " + " ".join(v.split())[:140]
    return name


def _base(p):
    p = str(p or "").rstrip("\\/")
    return re.split(r"[\\/]", p)[-1] if p else ""


def _q(v, n=60):
    v = " ".join(str(v or "").split())
    return "“" + (v if len(v) <= n else v[: n - 1] + "…") + "”" if v else ""


FILE_VERBS = {"Read": "reading", "Edit": "editing", "MultiEdit": "editing", "Write": "writing", "NotebookEdit": "editing"}
PLAIN = {"Grep": "searching the code", "Glob": "looking for files", "WebFetch": "reading a web page", "WebSearch": "searching the web",
         "ToolSearch": "loading tools", "TodoWrite": "updating its task list", "TaskCreate": "adding to its task list",
         "TaskUpdate": "updating its task list", "TaskList": "reading its task list", "StructuredOutput": "writing up its final report",
         "Workflow": "starting a workflow", "SendMessage": "sending a message to another agent", "TaskStop": "stopping a background job",
         "AskUserQuestion": "asking you a question", "ExitPlanMode": "presenting a plan", "Monitor": "watching a background job",
         "Artifact": "publishing a page", "ArtifactData": "updating a page's data", "CronCreate": "scheduling a check",
         "CronDelete": "removing a scheduled check", "PushNotification": "sending you a notification", "advisor": "asking the advisor"}
MCP_WORDS = {"ccd_turn": "the app", "ccd_session": "the app", "ccd_session_mgmt": "the app", "ccd_view": "the app", "ccd_pr": "the app",
             "Claude_Browser": "the browser", "claude-in-chrome": "the browser", "computer-use": "the desktop"}
APP_TOOLS = {"update_plan": "updating the task list", "wrap_up": "wrapping up the turn", "get_usage": "checking usage",
             "start_session": "starting another session", "mark_chapter": "marking a new chapter"}


CD_PREFIX = re.compile(r"""^\s*(?:cd|pushd|Set-Location)\s+(?:"[^"]*"|'[^']*'|\S+)\s*(?:&&|;)\s*""", re.I)
QUOTED_PATH = re.compile(r"""(["'])(?:[A-Za-z]:[\\/]|/)[^"']*?([^"'\\/]+)\1""")
# a heredoc start: a quoted delimiter, or an upper-case one (EOF, PYEOF), so "a << b" in a string is left alone
HEREDOC = re.compile(r"""\s*<<-?\s*(?:(["'])[A-Za-z_]\w*\1|[A-Z_][A-Z0-9_]*\b).*$""")
BARE_PATH = re.compile(r"""(?<![\w.])(?:[A-Za-z]:[\\/]|/(?:[A-Za-z]/|mnt/|home/|Users/|tmp/|var/|opt/))[^\s"'|;&<>]*?([^\s"'|;&<>\\/]+)(?=[\s"'|;&<>]|$)""")


def short_cmd(cmd):
    """A command as a person would say it: without the leading `cd <dir> &&`, and with absolute paths cut to their
    last part (C:/work/repo/tools/build.py -> build.py)."""
    c = " ".join(str(cmd or "").split())
    for _ in range(3):
        m = CD_PREFIX.match(c)
        if not m:
            break
        c = c[m.end():]
    c = HEREDOC.sub(" (inline script)", c)  # the body of `python3 - <<'EOF' ...` says nothing at a glance
    return BARE_PATH.sub(r"\1", QUOTED_PATH.sub(r"\2", c))


def describe(name, inp):
    """A tool call in plain words for people: 'Publish the build' (a command's own description), 'reading config.py',
    'searching the code for “parse”', 'using the browser (navigate)'. Untrusted text, clipped by the caller."""
    inp = inp if isinstance(inp, dict) else {}
    desc = " ".join(str(inp.get("description") or "").split())
    if name in ("Bash", "PowerShell"):
        if desc:
            return desc
        cmd = short_cmd(inp.get("command"))
        return "running a command" + (": " + (cmd if len(cmd) <= 80 else cmd[:79] + "…") if cmd else "")
    if name in FILE_VERBS:
        f = _base(inp.get("file_path") or inp.get("notebook_path") or inp.get("path"))
        return FILE_VERBS[name] + " " + (f or "a file")
    if name == "Grep":
        return "searching the code for " + _q(inp.get("pattern")) if inp.get("pattern") else PLAIN[name]
    if name == "Glob":
        return "looking for files (" + str(inp.get("pattern"))[:60] + ")" if inp.get("pattern") else PLAIN[name]
    if name == "WebSearch":
        return "searching the web for " + _q(inp.get("query")) if inp.get("query") else PLAIN[name]
    if name == "WebFetch":
        host = re.match(r"\w+://([^/]+)", str(inp.get("url") or ""))
        return "reading a web page" + (" (" + host.group(1) + ")" if host else "")
    if name in ("Agent", "Task"):
        return "asking a helper: " + desc if desc else "asking a helper agent"
    if name == "Skill":
        return "using the " + str(inp.get("skill") or inp.get("name") or "") + " skill"
    if name in PLAIN:
        return PLAIN[name]
    if name.startswith("mcp__"):
        parts = name.split("__")
        server, tool = (parts[1] if len(parts) > 1 else ""), (parts[-1] if len(parts) > 2 else "")
        if tool in APP_TOOLS:
            return APP_TOOLS[tool]
        who = MCP_WORDS.get(server) or ("the app" if server.startswith("ccd_") else server.replace("_", " ").replace("-", " "))
        return "using " + who + (" (" + tool.replace("_", " ") + ")" if tool else "")
    return "using " + name if name else ""


def recent_tools(path, n=10):
    """The last n tool calls of an agent, read from the tail of its transcript (again only when it has grown).

    Returns {"now": "Bash: npm test", "nowText": "Run the tests", "repeat": 5, "said": "...", "model": "...",
    "helpers": [...]}. now is the raw call, nowText the same in plain words. repeat is how many of the last n calls were
    identical (4 or more suggests a loop). helpers are the subagents it started with the Agent tool in the tail, each
    with whether its result has come back."""
    out = {"now": "", "nowText": "", "repeat": 0, "said": "", "model": "", "helpers": []}
    try:
        size = os.stat(path).st_size
        hit = _tail_cache.get(str(path))
        if hit and hit[0] == size:
            return hit[1]
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read()
    except OSError:
        return out
    _tail_cache[str(path)] = (size, out)
    lines = chunk.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]
    calls, helpers = [], []
    for line in lines:
        if b'"tool_use"' not in line and b'"type":"text"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        content = (d.get("message") or {}).get("content") if isinstance(d, dict) else None
        if d.get("type") != "assistant" or not isinstance(content, list):
            continue
        m = (d.get("message") or {}).get("model")
        if m and m != "<synthetic>":
            out["model"] = m
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                calls.append((c.get("name") or "", c.get("input")))
                if c.get("name") in ("Agent", "Task") and isinstance(c.get("input"), dict):
                    inp = c["input"]
                    helpers.append({"id": c.get("id") or "", "desc": " ".join(str(inp.get("description") or inp.get("subagent_type") or "helper").split())[:80],
                                    "model": str(inp.get("model") or "")})
            elif c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].strip():
                out["said"] = " ".join(c["text"].split())[:600]
    for hlp in helpers[-6:]:
        hlp["done"] = bool(hlp["id"]) and (b'"tool_use_id":"' + hlp["id"].encode() + b'"') in chunk
        del hlp["id"]
        out["helpers"].append(hlp)
    calls = calls[-n:]
    if not calls:
        return out
    out["now"] = _brief(*calls[-1])
    out["nowText"] = describe(*calls[-1])[:160]
    sigs = [hashlib.md5((nm + json.dumps(inp, sort_keys=True, default=str)[:400]).encode("utf8", "replace")).hexdigest() for nm, inp in calls]
    out["repeat"] = max(sigs.count(s) for s in set(sigs)) if len(sigs) >= 6 else 0
    return out


_continued = {}


def continued_label(sub, aid):
    """An agent that began inside a workflow run and was continued later (SendMessage) is written next to the side
    agents, often with no description: name it after its label in that run."""
    if aid in _continued:
        return _continued[aid]
    label = ""
    try:
        for meta in (sub / "workflows").glob("*/agent-%s.meta.json" % aid):
            for line in (meta.parent / "journal.jsonl").read_text(encoding="utf8", errors="replace").splitlines():
                if aid in line:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and e.get("agentId") == aid and e.get("label"):
                        label = str(e["label"])
                        break
            label = "continued: " + (label or "a workflow agent")
            break
    except OSError:
        pass
    _continued[aid] = label
    return label


def side_agents(session_dir, now_ms, idle_ms):
    """Subagents started outside a workflow (the Agent tool, or a workflow agent continued later):
    <session>/subagents/agent-*.meta.json, and agent transcripts there that have no meta file."""
    out = []
    sub = Path(session_dir) / "subagents"
    try:
        metas = {m.name[len("agent-"):-len(".meta.json")]: m for m in sub.glob("agent-*.meta.json")}
        found = {t.name[len("agent-"):-len(".jsonl")]: t for t in sub.glob("agent-*.jsonl")}
    except OSError:
        return out
    for aid in set(metas) | set(found):
        meta = {}
        if aid in metas:
            try:
                meta = json.loads(metas[aid].read_text(encoding="utf8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
        tpath = sub / ("agent-%s.jsonl" % aid)
        try:
            act = int(os.stat(tpath).st_mtime * 1000)
        except OSError:
            act = int(os.stat(metas[aid]).st_mtime * 1000)
        head = head_info(tpath)
        if meta.get("finished"):
            state = "done"
        elif now_ms - act > idle_ms:
            state = "idle"
        else:
            state = "running"
        if aid not in metas and state == "idle":
            continue  # a bare transcript that has gone quiet: nothing to show
        desc = meta.get("description") or (continued_label(sub, aid) if not meta.get("toolUseId") else "")
        row = {"id": aid, "description": desc, "type": meta.get("agentType") or "",
               "state": state, "activity": act, "model": head["model"], "start": _ms(head["start"]),
               "useId": meta.get("toolUseId") or "", "background": meta.get("requestShape") == "background",
               "outTokens": output_tokens(tpath) if state != "done" else 0}
        if state != "done":
            look = recent_tools(tpath)
            row.update({"now": look["now"], "nowText": look["nowText"], "said": look["said"], "tools": usage_now(tpath)[2],
                        "model": head["model"] or look["model"]})
        out.append(row)
    out.sort(key=lambda a: a["activity"], reverse=True)
    return out


def _ms(iso):
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


HANDOFF_NAMES = ("HANDOFF.md", "NOTES.md", "handoff.md", "notes.md", "docs/HANDOFF.md", "docs/NOTES.md", "docs/handoff.md", "TODO.md")


def find_handoff(project, explicit=""):
    cands = [Path(explicit)] if explicit else [Path(project) / n for n in HANDOFF_NAMES]
    for c in cands:
        try:
            if c.is_file():
                return {"path": str(c), "name": c.name, "updated": int(os.stat(c).st_mtime * 1000)}
        except OSError:
            pass
    return None


def family(model):
    m = (model or "").lower()
    for f in ("opus", "sonnet", "haiku", "fable"):
        if f in m:
            return f
    return "other" if m else "default"
