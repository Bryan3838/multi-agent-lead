"""Cheap, read-only looks into agent transcripts for the dashboard.

Transcripts can be many megabytes, so nothing here reads one whole more than once:
the first lines give the model and start time, the tail gives the latest tool calls,
and output tokens are summed incrementally by byte offset. Everything returned is
untrusted text and is clipped by the caller.
"""
import hashlib
import json
import os
import re
from pathlib import Path

TAIL_BYTES = 700_000
HEAD_LINES = 300
HEAD_BYTES = 4_000_000
ARG_KEYS = ("command", "file_path", "path", "pattern", "description", "query", "url", "prompt", "skill", "name")

_head_cache = {}
_token_cache = {}


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


def recent_tools(path, n=10):
    """The last n tool calls of an agent, read from the tail of its transcript.

    Returns {"now": "Bash: npm test", "repeat": 5, "said": "...", "model": "...", "helpers": [...]}.
    repeat is how many of the last n calls were identical (4 or more suggests a loop). helpers are the
    subagents it started with the Agent tool in the tail, each with whether its result has come back."""
    out = {"now": "", "repeat": 0, "said": "", "model": "", "helpers": []}
    try:
        size = os.stat(path).st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read()
    except OSError:
        return out
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
    sigs = [hashlib.md5((nm + json.dumps(inp, sort_keys=True, default=str)[:400]).encode("utf8", "replace")).hexdigest() for nm, inp in calls]
    out["repeat"] = max(sigs.count(s) for s in set(sigs)) if len(sigs) >= 6 else 0
    return out


def side_agents(session_dir, now_ms, idle_ms):
    """Subagents started outside a workflow (the Agent tool): <session>/subagents/agent-*.meta.json."""
    out = []
    sub = Path(session_dir) / "subagents"
    try:
        metas = list(sub.glob("agent-*.meta.json"))
    except OSError:
        return out
    for m in metas:
        try:
            meta = json.loads(m.read_text(encoding="utf8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        aid = m.name[len("agent-"):-len(".meta.json")]
        tpath = sub / ("agent-%s.jsonl" % aid)
        try:
            act = int(os.stat(tpath).st_mtime * 1000)
        except OSError:
            act = int(os.stat(m).st_mtime * 1000)
        head = head_info(tpath)
        if meta.get("finished"):
            state = "done"
        elif now_ms - act > idle_ms:
            state = "idle"
        else:
            state = "running"
        out.append({"id": aid, "description": meta.get("description") or "", "type": meta.get("agentType") or "",
                    "state": state, "activity": act, "model": head["model"], "start": head["start"],
                    "background": meta.get("requestShape") == "background",
                    "outTokens": output_tokens(tpath) if state != "done" else 0})
    out.sort(key=lambda a: a["activity"], reverse=True)
    return out


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
