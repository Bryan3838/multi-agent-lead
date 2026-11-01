#!/usr/bin/env python3
"""Collect the state of multi-agent Claude Code runs into one JSON document.

Read-only and free: it reads Claude Code's workflow records on disk, a few
read-only git queries and an optional usage file. It never calls a model and
never executes anything it reads from the records (all of that is untrusted
data: strings are clipped and stripped of control characters here, and the page
only ever inserts them as text).

Usage as a script (one snapshot):  python status.py --project <repo> [--out status.json]
The long-running loop lives in run.py.
"""
import argparse
from datetime import datetime, timezone
import glob
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import live

CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
RUN_ID = re.compile(r"Run ID: (wf_[\w-]+)")
MAX_RUNS = 400


# ----------------------------------------------------------------- helpers
def clip(value, n=300):
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
    value = CTRL.sub("", value)
    return value if len(value) <= n else value[: n - 1] + "…"


def as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_dict(value):
    """Args arrive as a dict, or as a JSON string; anything else is empty."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def read_json(path):
    try:
        with open(path, encoding="utf8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf8", errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def mtime_ms(path):
    try:
        return int(os.stat(path).st_mtime * 1000)
    except OSError:
        return 0


def project_slug(path):
    """Claude Code names a project folder after the path with every non-alphanumeric as '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def short_model(model):
    model = clip(model, 60)
    return model[7:] if model.startswith("claude-") else model


# -------------------------------------------------------------- sessions
def claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def has_runs(session_dir):
    return (session_dir / "workflows").is_dir() or (session_dir / "subagents" / "workflows").is_dir()


def list_sessions(project_dir):
    """(latest change, session folder, has workflow runs) for every session with a folder or a transcript."""
    found = {}
    try:
        for entry in project_dir.iterdir():
            if entry.is_dir() and entry.name != "memory":
                found[entry.name] = entry
            elif entry.suffix == ".jsonl":
                found.setdefault(entry.stem, project_dir / entry.stem)
    except OSError:
        pass
    sessions = []
    for name, entry in found.items():
        latest = max(mtime_ms(entry), mtime_ms(project_dir / (name + ".jsonl")))
        sessions.append((latest, entry, has_runs(entry)))
    sessions.sort(key=lambda x: x[0], reverse=True)
    return sessions


# ---------------------------------------------- launch info from the session
class LaunchIndex:
    """Run id -> {args, scriptPath, name, summary, ts}, read from the session transcript.

    A run that is still going has no summary file yet, so its title and args only
    exist in the transcript, in the Workflow tool call and its 'Workflow launched'
    result. The transcript is large, so it is read incrementally by byte offset
    and only the lines that mention the Workflow tool are parsed.
    """

    def __init__(self):
        self.offset = {}
        self.inputs = {}
        self.launched = {}
        self.plan = []
        self.plan_time = ""
        self.advisor_calls = 0
        self.advisor_last = ""
        self.advisor_open = {}

    def update(self, transcript):
        key = str(transcript)
        try:
            size = transcript.stat().st_size
        except OSError:
            return
        off = self.offset.get(key, 0)
        if size < off:
            off = 0
            self.plan = []
            self.advisor_calls = 0
            self.advisor_open = {}
        if size == off:
            return
        try:
            with open(transcript, "rb") as f:
                f.seek(off)
                while True:
                    line = f.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    off += len(line)
                    if (b'"name":"Workflow"' in line or b"Workflow launched" in line or b"update_plan" in line
                            or b"wrap_up" in line or b"TodoWrite" in line or b'"name":"advisor"' in line
                            or b"advisor_tool_result" in line):
                        self._take(line)
        except OSError:
            return
        self.offset[key] = off

    def _take(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if d.get("type") not in ("assistant", "user"):
            return
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("name") == "advisor" and c.get("type") in ("tool_use", "server_tool_use") and d.get("type") == "assistant":
                self.advisor_calls += 1
                self.advisor_last = d.get("timestamp") or self.advisor_last
                self.advisor_open[c.get("id")] = iso_ms(d.get("timestamp"))
            if c.get("type") == "advisor_tool_result":
                self.advisor_open.pop(c.get("tool_use_id"), None)
            if c.get("type") == "tool_use" and c.get("name") == "Workflow":
                self.inputs[c.get("id")] = c.get("input") or {}
            elif c.get("type") == "tool_use" and d.get("type") == "assistant":
                if self._plan_call(c.get("name") or "", c.get("input") or {}):
                    self.plan_time = d.get("timestamp") or ""
            elif c.get("type") == "tool_result":
                body = c.get("content")
                if isinstance(body, list):
                    body = "".join(x.get("text", "") for x in body if isinstance(x, dict))
                if isinstance(body, str) and body.startswith("Workflow launched"):
                    m = RUN_ID.search(body)
                    if m:
                        script = re.search(r"Script file: (.+)", body)
                        summ = re.search(r"Summary: (.+)", body)
                        self.launched[m.group(1)] = {
                            "tool_use_id": c.get("tool_use_id"),
                            "scriptPath": script.group(1).strip() if script else "",
                            "summary": summ.group(1).strip() if summ else "",
                            "ts": d.get("timestamp"),
                        }

    def _plan_call(self, name, inp):
        """Replay the lead's task list (the app's update_plan, or TodoWrite) in transcript order."""
        if not isinstance(inp, dict):
            return False
        if name.endswith("TodoWrite") or name.endswith("update_plan"):
            items = inp.get("todos") if name.endswith("TodoWrite") else inp.get("items")
            if isinstance(items, list):
                self.plan = [{"content": clip(i.get("content"), 160), "status": i.get("status") if i.get("status") in ("pending", "in_progress", "completed") else "pending"}
                             for i in items[:40] if isinstance(i, dict)]
                return True
            plan = self.plan
            if inp.get("advance"):
                for i in plan:
                    if i["status"] == "in_progress":
                        i["status"] = "completed"
                nxt = next((i for i in plan if i["status"] == "pending"), None)
                if nxt:
                    nxt["status"] = "in_progress"
            for key, status in (("done", "completed"), ("start", "in_progress")):
                v = inp.get(key)
                for n in v if isinstance(v, list) else [v]:
                    if isinstance(n, int) and 1 <= n <= len(plan):
                        plan[n - 1]["status"] = status
            return True
        if name.endswith("wrap_up"):
            for i in self.plan:
                if i["status"] == "in_progress":
                    i["status"] = "completed"
            return True
        return False

    def advisor_pending(self, now_ms, max_age_ms=30 * 60000):
        """Start time of a consult that has been asked and not answered yet (a call with no result), if recent."""
        since = [t for t in self.advisor_open.values() if t and now_ms - t < max_age_ms]
        return max(since) if since else 0

    def get(self, run_id):
        info = self.launched.get(run_id)
        if not info:
            return {}
        call = self.inputs.get(info["tool_use_id"]) or {}
        script = info["scriptPath"] or call.get("scriptPath") or ""
        name = call.get("name") or ""
        if not name and script:
            base = re.sub(r"\.[A-Za-z]+$", "", re.split(r"[\\/]", script)[-1])
            name = re.sub(r"-wf_[\w-]+$", "", base)
        return {
            "args": as_dict(call.get("args")),
            "scriptPath": script,
            "name": name,
            "summary": info["summary"],
            "ts": info["ts"],
        }


# ------------------------------------------------------- result extraction
SECTION_KEYS = ("plainWords", "summary", "tip", "commits", "openIssues", "issues", "gaps", "reasons", "shipOk", "ok", "tests")


def _section(name, obj):
    if not isinstance(obj, dict) or not any(k in obj for k in SECTION_KEYS):
        return None
    commits = obj.get("commits")
    issues = obj.get("openIssues") or obj.get("issues") or obj.get("gaps") or []
    sec = {
        "name": clip(name, 60),
        "plain": clip(obj.get("plainWords") or obj.get("summary"), 2400),
        "tip": clip(obj.get("tip"), 60),
        "commits": [clip(c, 220) for c in commits[:15]] if isinstance(commits, list) else [],
        "issues": [clip(i, 320) for i in issues[:15]] if isinstance(issues, list) else [],
        "tests": clip(obj.get("tests"), 600),
    }
    ok = obj.get("shipOk", obj.get("ok"))
    if isinstance(ok, bool):
        sec["ok"] = ok
    if not (sec["plain"] or sec["tip"] or sec["commits"] or sec["issues"] or sec["tests"]):
        return None
    return sec


def extract_sections(result):
    """Generic: any dict in the result carrying plainWords/summary/tip/commits/openIssues."""
    result = result if isinstance(result, (dict, list)) else as_dict(result)
    out = []

    def walk(name, obj, depth):
        if len(out) >= 12 or depth > 3:
            return
        sec = _section(name, obj)
        if sec:
            out.append(sec)
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    walk(k, v, depth + 1)
        elif isinstance(obj, list):
            for i, v in enumerate(obj[:8]):
                if isinstance(v, (dict, list)):
                    walk("%s %d" % (name, i + 1) if len(obj) > 1 else name, v, depth + 1)

    walk("result", result, 0)
    return out


def headline(sections):
    for sec in reversed(sections):
        if sec["plain"]:
            return sec["plain"]
    return ""


def pass_state(value):
    """Gate/smoke blocks differ by project: read ok / allFail / failed generically."""
    if not isinstance(value, dict):
        return "pending"
    if isinstance(value.get("ok"), bool):
        return "pass" if value["ok"] else "fail"
    for key in ("allFail", "failed", "failures"):
        if key in value:
            return "pass" if as_int(value[key]) == 0 else "fail"
    return "done"


# --------------------------------------------------------------- runs
def pick_lane(args, lane_field, name, run_id):
    keys = [lane_field] if lane_field else []
    keys += ["br", "branch", "lane", "title"]
    for k in keys:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return clip(v.strip(), 120)
    return clip(name or run_id, 120)


def arg_model(args, label):
    """A model named for an agent in the run's args (builders / critics lists), when the journal carries none."""
    for key in ("builders", "critics", "agents"):
        for item in args.get(key) or []:
            if isinstance(item, dict) and item.get("model") and (item.get("label") == label or len(args.get(key)) == 1):
                return item["model"]
    return args.get("model") if isinstance(args.get("model"), str) else ""


def load_runs(session_dir, session_id, launches, idle_ms, now_ms, lane_field):
    summaries = {}
    wf_dir = session_dir / "workflows"
    try:
        for p in wf_dir.glob("wf_*.json"):
            d = read_json(p)
            if isinstance(d, dict):
                summaries[p.stem] = (d, mtime_ms(p))
    except OSError:
        pass
    ids = set(summaries)
    sub_dir = session_dir / "subagents" / "workflows"
    try:
        for p in sub_dir.iterdir():
            if p.is_dir() and p.name.startswith("wf_"):
                ids.add(p.name)
    except OSError:
        pass

    # phases by workflow name, from the newest finished run of that name
    phase_sets = {}
    for rid, (d, _) in sorted(summaries.items(), key=lambda kv: kv[1][1]):
        if isinstance(d.get("phases"), list) and d.get("workflowName"):
            titles = [clip(p.get("title"), 40) for p in d["phases"] if isinstance(p, dict)]
            if titles:
                phase_sets[base_name(d["workflowName"])] = titles

    runs = []
    for rid in ids:
        summary, summary_mtime = summaries.get(rid, (None, 0))
        launch = launches.get(rid)
        run_dir = sub_dir / rid
        journal = read_jsonl(run_dir / "journal.jsonl") if run_dir.is_dir() else []

        started, results = {}, {}
        for e in journal:
            if not isinstance(e, dict):
                continue
            if e.get("type") == "started" and e.get("agentId"):
                started[e["agentId"]] = e
            elif e.get("type") == "result" and e.get("agentId"):
                results[e["agentId"]] = e

        agents = []
        progress = {}
        args = as_dict((summary or {}).get("args")) or (launches.get(rid) or {}).get("args") or {}
        if summary:
            for a in summary.get("workflowProgress") or []:
                if isinstance(a, dict) and a.get("type") == "workflow_agent":
                    progress[a.get("agentId") or "i%s" % a.get("index")] = a
        status = (summary or {}).get("status") or ""
        ended = bool(summary)

        seen = set()
        for aid, a in progress.items():
            seen.add(aid)
            state = a.get("state")
            act = as_int(a.get("lastProgressAt"))
            if state == "done":
                st = "done"
            elif state == "error":
                st = "failed"
            elif status == "killed":
                st = "stopped"
            else:
                st = "running"
            agents.append({
                "id": clip(aid, 40), "label": clip(a.get("label"), 80), "phase": clip(a.get("phaseTitle"), 40),
                "model": short_model(a.get("model")), "state": st, "activity": act,
                "started": as_int(a.get("startedAt")), "tokens": as_int(a.get("tokens")),
                "duration": as_int(a.get("durationMs")),
                "tools": as_int(a.get("toolCalls")),
                "error": clip(a.get("error"), 400),
                "last": clip(a.get("lastToolSummary"), 120),
            })
        for aid, s in started.items():
            if aid in seen:
                continue
            meta = read_json(run_dir / ("agent-%s.meta.json" % aid)) or {}
            t_path = run_dir / ("agent-%s.jsonl" % aid)
            act = mtime_ms(t_path)
            begun = mtime_ms(run_dir / ("agent-%s.meta.json" % aid)) or act
            if aid in results:
                st = "done"
            elif ended:
                st = "stopped"
            elif now_ms - act > idle_ms:
                st = "idle"
            else:
                st = "running"
            agents.append({
                "id": clip(aid, 40), "label": clip(s.get("label") or meta.get("description"), 80),
                "phase": clip(s.get("phase") or meta.get("workflowPhase"), 40),
                "model": short_model(meta.get("model") or arg_model(args, s.get("label") or meta.get("description"))
                                     or live.head_info(t_path)["model"]),
                "state": st, "activity": act, "started": begun,
                "tokens": 0, "tools": 0, "error": "", "last": "", "duration": max(0, act - begun) if st == "done" else 0,
            })
            if st in ("running", "idle"):
                look = live.recent_tools(t_path)
                out_now, ctx_now, tools_now = live.usage_now(t_path)
                agents[-1].update({"now": clip(look["now"], 160), "said": clip(look["said"], 500), "repeat": look["repeat"],
                                   "tokens": ctx_now, "tools": tools_now,
                                   "helpers": [{"desc": clip(x["desc"], 80), "model": short_model(x["model"]), "done": x["done"]} for x in look["helpers"]], "outTokens": live.output_tokens(t_path)})

        launch_info = launches.get(rid) or {}
        name = (summary or {}).get("workflowName") or launch_info.get("name") or ""
        started_at = as_int((summary or {}).get("startTime")) or min(
            [a["started"] for a in agents if a["started"]] or [0]
        ) or mtime_ms(run_dir)
        last_activity = max([a["activity"] for a in agents] + [summary_mtime, 0])

        if summary:
            if status == "killed":
                state = "killed"
            elif any(a["state"] == "failed" for a in agents) or status in ("failed", "error"):
                state = "failed"
            else:
                state = "done"
            finished = summary_mtime or last_activity
        else:
            running_now = [a for a in agents if a["state"] == "running"]
            if running_now:
                state = "running"
            elif any(a["state"] == "idle" for a in agents):
                state = "idle"
            elif now_ms - last_activity > idle_ms:
                state = "idle"
            else:
                state = "running"
            finished = 0

        phases = list(phase_sets.get(base_name(name), []))
        for a in agents:
            if a["phase"] and a["phase"] not in phases:
                phases.append(a["phase"])
        chips = []
        for ph in phases:
            in_phase = [a for a in agents if a["phase"] == ph]
            states = {a["state"] for a in in_phase}
            if "failed" in states:
                cs = "failed"
            elif "running" in states:
                cs = "running"
            elif "idle" in states:
                cs = "idle"
            elif "stopped" in states:
                cs = "stopped"
            elif in_phase:
                cs = "done"
            else:
                cs = "skipped" if summary and status == "completed" else "pending"
            chips.append({"title": ph, "state": cs, "count": len(in_phase)})

        result = (summary or {}).get("result")
        sections = extract_sections(result) if summary else []
        run = {
            "id": clip(rid, 40), "session": clip(session_id, 60), "name": clip(name, 80), "state": state,
            "title": clip(args.get("title") or launch_info.get("summary") or name or rid, 200),
            "lane": pick_lane(args, lane_field, name, rid),
            "branch": clip(args.get("br") or args.get("branch"), 120),
            "worktree": clip(args.get("wt") or args.get("worktree"), 160),
            "start": started_at, "end": finished, "activity": last_activity,
            "agentCount": len(agents), "tokens": as_int((summary or {}).get("totalTokens"))
            or sum(a["tokens"] for a in agents),
            "model": short_model((summary or {}).get("defaultModel")),
            "phases": chips, "agents": sorted(agents, key=lambda a: (a["started"], a["label"])),
            "headline": clip(headline(sections), 1200), "sections": sections,
            "scriptPath": clip(launch_info.get("scriptPath") or (summary or {}).get("scriptPath"), 400),
            "logs": [clip(x, 200) for x in ((summary or {}).get("logs") or [])[:10]] if summary else [],
            "isTrain": "train" in name.lower(),
            "error": clip((summary or {}).get("error"), 300).split("\n")[0],
        }
        if run["isTrain"]:
            res = result if isinstance(result, dict) else {}
            branches = args.get("branches") if isinstance(args.get("branches"), list) else []
            merge, gate, smoke = res.get("merge"), res.get("gate"), res.get("smoke")
            run["train"] = {
                "k": clip(args.get("k") or res.get("k"), 20),
                "branches": [clip(b, 80) for b in branches[:40]],
                "merged": isinstance(merge, dict),
                "tip": clip((merge or {}).get("tip") if isinstance(merge, dict) else "", 60),
                "conflicts": len((merge or {}).get("conflicts") or []) if isinstance(merge, dict) else 0,
                "gate": pass_state(gate),
                "smoke": pass_state(smoke),
                "gateNote": clip((gate or {}).get("gateLog"), 200) if isinstance(gate, dict) else "",
            }
        runs.append(run)

    runs.sort(key=lambda r: r["start"], reverse=True)
    return runs[:MAX_RUNS]


# ------------------------------------------------------------------- eta
MIN_SAMPLES = 2


def base_name(name):
    """build2 and build are the same kind of run."""
    return re.sub(r"\d+$", "", name or "")


def median(values):
    v = sorted(values)
    return v[len(v) // 2] if v else 0


def add_eta(runs, now_ms):
    """Estimate when live runs finish from how long finished agents of the same workflow and phase took.

    Only estimates with at least MIN_SAMPLES (2) finished agents behind them are shown; a phase that
    has run longer than its typical time is reported as overdue instead of guessing.
    """
    by_phase, by_phase_any, runs_seen, runs_with = {}, {}, {}, {}
    for r in runs:
        if r["state"] in ("running", "idle"):
            continue
        runs_seen[base_name(r["name"])] = runs_seen.get(base_name(r["name"]), 0) + 1
        for ph in {a["phase"] for a in r["agents"]}:
            runs_with[(base_name(r["name"]), ph)] = runs_with.get((base_name(r["name"]), ph), 0) + 1
        for a in r["agents"]:
            if a["state"] == "done" and a.get("duration"):
                by_phase.setdefault((base_name(r["name"]), a["phase"]), []).append(a["duration"])
                by_phase_any.setdefault(a["phase"], []).append(a["duration"])

    def typical(name, phase):
        v = by_phase.get((base_name(name), phase)) or []
        if len(v) < MIN_SAMPLES:
            v = by_phase_any.get(phase) or []
        return (median(v), len(v)) if len(v) >= MIN_SAMPLES else (0, len(v))

    tools_by, tokens_by = {}, {}
    for r in runs:
        if r["state"] in ("running", "idle"):
            continue
        for a in r["agents"]:
            if a["state"] == "done":
                for store, val in ((tools_by, a.get("tools")), (tokens_by, a.get("tokens"))):
                    if val:
                        store.setdefault((base_name(r["name"]), a["phase"]), []).append(val)

    def typ_of(store, name, phase):
        v = store.get((base_name(name), phase)) or []
        return median(v) if len(v) >= MIN_SAMPLES else 0

    for r in runs:
        for a in r["agents"]:
            a["typicalTools"] = typ_of(tools_by, r["name"], a["phase"])
            a["typicalTokens"] = typ_of(tokens_by, r["name"], a["phase"])
            if not a.get("typical"):
                a["typical"] = typical(r["name"], a["phase"])[0]

    for r in runs:
        if r["state"] not in ("running", "idle"):
            continue
        remaining, overdue, samples, known = 0, False, 0, False
        for chip in r["phases"]:
            typ, n = typical(r["name"], chip["title"])
            if chip["state"] in ("running", "idle"):
                running_now = [a for a in r["agents"] if a["phase"] == chip["title"] and a["state"] in ("running", "idle")]
                for a in running_now:
                    a["typical"] = typ
                if typ and running_now:
                    known, samples = True, max(samples, n)
                    left = max(a["started"] + typ - now_ms for a in running_now)
                    # past the median is normal (half of all runs are); call it late only at 1.5x
                    if left <= 0 and now_ms - min(a["started"] for a in running_now) > 1.5 * typ:
                        overdue = True
                        remaining = max(remaining, 0)
                    else:
                        remaining = max(remaining, max(left, 0))
            elif chip["state"] == "pending" and typ and runs_with.get((base_name(r["name"]), chip["title"]), 0) * 2 >= runs_seen.get(base_name(r["name"]), 1):
                known, samples = True, max(samples, n)
                remaining += typ
        r["eta"] = {"known": known, "overdue": overdue and remaining == 0, "remainingMs": int(remaining) if known else None, "samples": samples}


# ------------------------------------------------------------- advisor notes
def iso_ms(ts):
    try:
        return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def read_advisor_log(path, limit=20, tail=131072):
    """The lead's own notes about each advisor consult (advisor_log.py appends them), newest first.
    A missing file or a half-written last line is fine; every string is clipped like all other untrusted text."""
    try:
        size = os.stat(path).st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - tail))
            text = f.read().decode("utf8", "replace")
    except OSError:
        return []
    lines = text.splitlines()
    if size > tail:
        lines = lines[1:]
    out = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and as_int(e.get("at")):
            out.append({"at": as_int(e.get("at")), "asked": clip(e.get("asked"), 400), "advice": clip(e.get("advice"), 400),
                        "action": clip(e.get("action"), 400), "model": clip(e.get("model"), 40)})
    return out[-limit:][::-1]


# ------------------------------------------------------------------- git
def git(args, cwd, timeout=20):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(["git", "--no-optional-locks"] + args, cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout, env=env, encoding="utf8", errors="replace")
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def collect_git(project, integration, wanted_branches):
    """Worktrees, branch tips, ahead/behind vs the integration branch and dirty counts. Read-only."""
    out = {"available": False, "integration": integration or "", "worktrees": [], "branches": {}}
    raw = git(["worktree", "list", "--porcelain"], project)
    if raw is None:
        return out
    out["available"] = True
    trees, cur = [], {}
    for line in raw.splitlines() + [""]:
        if not line.strip():
            if cur.get("path"):
                trees.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur["path"] = val
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.replace("refs/heads/", "", 1)
        elif key == "detached":
            cur["detached"] = True
    refs = {}
    raw = git(["for-each-ref", "refs/heads", "--format=%(refname:short)\t%(objectname:short)\t%(committerdate:unix)\t%(upstream:short)\t%(upstream:track)\t%(subject)"], project)
    for line in (raw or "").splitlines():
        parts = line.split("\t", 5)
        if len(parts) == 6:
            track = re.search(r"ahead (\d+)", parts[4])
            refs[parts[0]] = {"tip": parts[1], "time": as_int(parts[2]) * 1000, "subject": clip(parts[5], 120),
                              "upstream": clip(parts[3], 120), "gone": "gone" in parts[4],
                              "unpushed": int(track.group(1)) if track else 0}
    remote_refs = set((git(["for-each-ref", "refs/remotes", "--format=%(refname:short)"], project) or "").split())

    def own_remote(branch):
        """The branch's own copy on a remote (origin/<branch> first), not whatever upstream it was set to track."""
        if "origin/" + branch in remote_refs:
            return "origin/" + branch
        others = sorted(r for r in remote_refs if "/" in r and r.split("/", 1)[1] == branch)
        return others[0] if others else ""

    if not integration:
        integration = next((b for b in ("main", "master") if b in refs), "")
        if not integration and trees and trees[0].get("branch"):
            integration = trees[0]["branch"]
    if integration not in refs:
        integration = ""
    out["integration"] = integration

    names = {t.get("branch") for t in trees if t.get("branch")} | {b for b in wanted_branches if b in refs}
    names = sorted(n for n in names if n in refs)[:80]

    def ahead_behind(branch):
        if not integration:
            return branch, None
        raw = git(["rev-list", "--left-right", "--count", "refs/heads/%s...refs/heads/%s" % (integration, branch)], project)
        try:
            behind, ahead = raw.split()
            return branch, {"ahead": int(ahead), "behind": int(behind)}
        except (AttributeError, ValueError):
            return branch, None

    def push_state(branch):
        rr = own_remote(branch)
        if not rr:
            return branch, {"upstream": "", "unpushed": 0, "gone": False}
        raw = git(["rev-list", "--count", "refs/remotes/%s..refs/heads/%s" % (rr, branch)], project)
        try:
            return branch, {"upstream": clip(rr, 120), "unpushed": int(raw), "gone": False}
        except (TypeError, ValueError):
            return branch, {"upstream": clip(rr, 120), "unpushed": 0, "gone": False}

    def dirty(tree):
        raw = git(["status", "--porcelain"], tree["path"])
        return tree["path"], (len(raw.splitlines()) if raw is not None else None)

    with ThreadPoolExecutor(max_workers=4) as pool:
        ab = dict(pool.map(ahead_behind, names))
        ps = dict(pool.map(push_state, names))
        dirt = dict(pool.map(dirty, trees[:40]))
    for n in names:
        r = dict(refs[n])
        r["name"] = clip(n, 120)
        if ab.get(n):
            r.update(ab[n])
        r.update(ps.get(n) or {})
        out["branches"][n] = r
    for t in trees[:40]:
        b = t.get("branch")
        out["worktrees"].append({
            "path": clip(t["path"], 300), "name": clip(re.split(r"[\\/]", t["path"].rstrip("\\/"))[-1], 100),
            "branch": clip(b, 120), "detached": bool(t.get("detached")),
            "tip": (refs.get(b, {}).get("tip") if b else clip(t.get("head", "")[:7], 8)) or "",
            "ahead": (ab.get(b) or {}).get("ahead") if b else None,
            "behind": (ab.get(b) or {}).get("behind") if b else None,
            "dirty": dirt.get(t["path"]),
        })
    return out


# ------------------------------------------------------- usage and studio
def pct(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, v * 100 if 0 < v <= 1 and not float(value).is_integer() else v))


def default_usage_file():
    return claude_dir() / "dashboard-usage.json"


def read_usage(path):
    """Usage windows from a JSON file: {"windows": [{"label", "percent" (or percentUsed), "resets_at" (or resetsAt)}]},
    or the older {"five_hour": {...}, "weekly": {...}} form. Account-wide, so one shared file serves every project."""
    if not path:
        return None
    if not Path(path).exists():
        return None
    d = read_json(path)
    if not isinstance(d, dict):
        return {"error": "usage file is not valid JSON", "path": clip(path, 200)}
    out = {"updated": mtime_ms(path), "windows": []}
    for w in d.get("windows") or []:
        if isinstance(w, dict):
            p = w.get("percent", w.get("percentUsed", w.get("pct")))
            out["windows"].append({"label": clip(w.get("label"), 60), "percent": pct(p),
                                   "resets": clip(w.get("resets_at", w.get("resetsAt", w.get("resets"))), 60)})
    for label, names in (("fiveHour", ("five_hour", "fiveHour", "5h", "session")),
                         ("weekly", ("weekly", "seven_day", "sevenDay", "week", "7d"))):
        block = next((d[n] for n in names if n in d), None)
        if isinstance(block, (int, float)):
            block = {"percent": block}
        if isinstance(block, dict):
            p = block.get("percent", block.get("pct", block.get("utilization", block.get("used"))))
            out[label] = {"percent": pct(p), "resets": clip(block.get("resets_at", block.get("resetsAt", block.get("resets"))), 60)}
            out["windows"].append({"label": "5-hour limit" if label == "fiveHour" else "Weekly", **out[label]})
    for w in out["windows"]:
        name = w["label"].lower()
        if "fiveHour" not in out and ("5-hour" in name or "5 hour" in name or "session" in name):
            out["fiveHour"] = {"percent": w["percent"], "resets": w["resets"]}
        elif "weekly" not in out and "weekly" in name and "all" in name:
            out["weekly"] = {"percent": w["percent"], "resets": w["resets"]}
    return out


APP_USAGE_NAME = "plan-usage-history.json"
FIVE_H, SEVEN_D = 5 * 3600000, 7 * 86400000


def app_usage_files(override=""):
    """Where the Claude desktop app keeps its plan-usage log, newest first. Searched by pattern, so no fixed install path."""
    if override:
        return [Path(override)]
    pats, home = [], Path.home()
    if os.name == "nt":
        for root in (os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")):
            if root:
                pats += [os.path.join(root, "Claude-Data", "*", APP_USAGE_NAME), os.path.join(root, "Claude*", APP_USAGE_NAME),
                         os.path.join(root, "Claude*", "*", APP_USAGE_NAME)]
    else:
        base = home / "Library" / "Application Support" if sys.platform == "darwin" else home / ".config"
        pats += [str(base / "Claude*" / APP_USAGE_NAME), str(base / "Claude*" / "*" / APP_USAGE_NAME)]
    found = set()
    for pat in pats:
        found.update(glob.glob(pat))
    return sorted((Path(f) for f in found), key=mtime_ms, reverse=True)


def _pct(v):
    try:
        return max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return None


def read_app_usage(override=""):
    """Newest sample of the app's usage log: {"t", "fh" (5-hour %), "sd" (7-day %), "points": recent samples of the same org}.
    The log has several accounts' samples; the account of the newest sample is the one in use. Missing or odd file: None."""
    for f in app_usage_files(override)[:3]:
        try:
            if f.stat().st_size > 5_000_000:
                continue
            d = json.loads(f.read_text(encoding="utf8"))
        except (OSError, ValueError):
            continue
        samples = [x for x in (d.get("samples") or []) if isinstance(x, dict) and isinstance(x.get("u"), dict) and as_int(x.get("t"))] if isinstance(d, dict) else []
        if not samples:
            continue
        newest = max(samples, key=lambda x: as_int(x["t"]))
        mine = sorted((x for x in samples if x.get("org") == newest.get("org")), key=lambda x: as_int(x["t"]))[-60:]
        return {"t": as_int(newest["t"]), "fh": _pct(newest["u"].get("fh")), "sd": _pct(newest["u"].get("sd")),
                "points": [(as_int(x["t"]), _pct(x["u"].get("fh")), _pct(x["u"].get("sd"))) for x in mine]}
    return None


def _is_five(label):
    l = (label or "").lower()
    return "5-hour" in l or "5 hour" in l or "five" in l or "session" in l


def _is_weekly_all(label):
    l = (label or "").lower()
    return "weekly" in l and "all" in l


def _roll(iso, period_ms, now_ms):
    t = iso_ms(iso)
    if not t or not period_ms:
        return iso, False
    rolled = False
    while t <= now_ms:
        t += period_ms
        rolled = True
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"), rolled


def merge_usage(lead, app, now_ms):
    """The lead's file knows the reset times; the app's log knows the current percentages. Use the app's numbers when its newest
    sample is newer than the lead's file, else the file's. Reset times that have passed are rolled forward by 5 h or 7 days."""
    if lead and lead.get("error") and not app:
        return lead
    lead_ok = bool(lead) and not lead.get("error")
    lead_at = lead.get("updated", 0) if lead_ok else 0
    windows = [dict(w, **{"from": "lead", "at": lead_at}) for w in (lead.get("windows") or [])] if lead_ok else []
    if not windows and not app:
        return lead
    if app and app["t"] > lead_at:
        for val, label, test in ((app.get("fh"), "5-hour limit", _is_five), (app.get("sd"), "Weekly \u00b7 all models", _is_weekly_all)):
            if val is None:
                continue
            w = next((x for x in windows if test(x["label"])), None)
            if w is None:
                w = {"label": label, "resets": ""}
                windows.insert(0 if test is _is_five else min(1, len(windows)), w)
            w.update({"percent": val, "from": "app", "at": app["t"]})
    for w in windows:
        if w.get("resets"):
            period = FIVE_H if _is_five(w["label"]) else SEVEN_D if "weekly" in w["label"].lower() else 0
            w["resets"], rolled = _roll(w["resets"], period, now_ms)
            last_reset = iso_ms(w["resets"]) - period if period else 0
            if period and last_reset and w.get("at", 0) < last_reset and w.get("percent") is not None:
                # the reading was taken before the window last reset, so it belongs to the previous window: show a fresh one
                w["percent"] = 0.0
                w["resetSince"] = True
    out = {"updated": max(lead_at, app["t"] if app else 0), "windows": windows, "points": app["points"] if app else [],
           "sources": {"app": app["t"] if app else 0, "lead": lead_at}}
    for w in windows:
        if _is_five(w["label"]) and "fiveHour" not in out:
            out["fiveHour"] = {"percent": w.get("percent"), "resets": w.get("resets", ""), "resetSince": bool(w.get("resetSince"))}
        elif _is_weekly_all(w["label"]) and "weekly" not in out:
            out["weekly"] = {"percent": w.get("percent"), "resets": w.get("resets", ""), "resetSince": bool(w.get("resetSince"))}
    return out


def count_processes(name):
    try:
        if os.name == "nt":
            p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s.exe" % name, "/NH", "/FO", "CSV"],
                               capture_output=True, text=True, timeout=15)
            return sum(1 for line in p.stdout.splitlines() if line.lower().startswith('"%s.exe"' % name.lower()))
        p = subprocess.run(["pgrep", "-fc", name], capture_output=True, text=True, timeout=15)
        return as_int(p.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None


# ------------------------------------------------------------- the snapshot
_launches = LaunchIndex()


# ------------------------------------------------- derived: burn, merge queue, attention
_usage_hist = []
LIMIT_WORDS = ("session limit", "usage limit", "rate limit", "limit reached", "hit your")


def usage_forecast(usage, now_ms):
    """Rate of change of the usage percentages, from the samples this collector has seen."""
    if not usage or usage.get("error"):
        return None
    sample = (now_ms, (usage.get("fiveHour") or {}).get("percent"), (usage.get("weekly") or {}).get("percent"))
    if not _usage_hist or _usage_hist[-1][1:] != sample[1:]:
        _usage_hist.append(sample)
    del _usage_hist[:-200]
    hist = sorted(usage.get("points") or []) if len(usage.get("points") or []) >= 2 else _usage_hist
    out = {}
    for idx, key, window, min_span in ((1, "fiveHour", 90 * 60000, 10 * 60000), (2, "weekly", 6 * 3600000, 30 * 60000)):
        if (usage.get(key) or {}).get("resetSince"):
            continue
        pts = []
        for h in reversed(hist):  # newest first, stop where the value was higher before: that is a reset, not a rate
            if h[idx] is None or now_ms - h[0] > window or (pts and h[idx] > pts[-1][idx] + 0.5):
                break
            pts.append(h)
        pts.reverse()
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < min_span:
            continue
        rate = (pts[-1][idx] - pts[0][idx]) / ((pts[-1][0] - pts[0][0]) / 3600000.0)
        cur = (usage.get(key) or {}).get("percent")
        if cur is None:
            cur = pts[-1][idx]
        elif abs(pts[-1][idx] - cur) > 3:
            out[key] = {"mismatch": True}  # the log's samples belong to another account: its rate says nothing about this one
            continue
        if rate <= 0.05:
            continue
        reset = iso_ms((usage.get(key) or {}).get("resets"))
        resets_in = int(max(0, (reset - now_ms) / 60000)) if reset else None
        to100 = int(max(0, (100 - cur) / rate * 60))
        out[key] = {"ratePerHour": round(rate, 1), "percent": cur, "to100Min": to100, "resetsInMin": resets_in,
                    "hitsBeforeReset": bool(resets_in is not None and to100 < resets_in)}
    return out or None


def derive(snap, now):
    runs = snap["runs"]
    lanes = {}
    for r in runs:
        if not r["isTrain"]:
            lanes.setdefault(r["session"] + "|" + r["lane"], []).append(r)
    for rs in lanes.values():
        rs.sort(key=lambda r: r["start"], reverse=True)
    git_branches = (snap.get("git") or {}).get("branches") or {}
    trees = (snap.get("git") or {}).get("worktrees") or []

    # burn: tokens finished recently (run summaries) and what live agents have written so far
    agents = [(r, a) for r in runs for a in r["agents"]]

    def finished(window):
        return sum(a["tokens"] for _, a in agents if a["state"] in ("done", "failed", "stopped") and a["tokens"] and now - a["activity"] <= window)

    fam = {}
    for _, a in agents:
        if a["tokens"] and a["state"] in ("done", "failed", "stopped") and now - a["activity"] <= 5 * 3600000:
            f = live.family(a["model"])
            fam[f] = fam.get(f, 0) + a["tokens"]
    lane_tokens = {}
    for key, rs in lanes.items():
        t = sum(a["tokens"] for r in rs for a in r["agents"] if now - a["activity"] <= 24 * 3600000)
        if t:
            lane_tokens[key] = {"key": key, "lane": rs[0]["lane"], "title": rs[0]["title"], "tokens": t}
    live_agents = [a for _, a in agents if a["state"] in ("running", "idle")]
    snap["burn"] = {
        "finished1h": finished(3600000), "finished5h": finished(5 * 3600000), "byFamily5h": fam,
        "liveAgents": len(live_agents), "liveOutTokens": sum(a.get("outTokens", 0) for a in live_agents),
        "topLanes": sorted(lane_tokens.values(), key=lambda x: -x["tokens"])[:5],
        "forecast": usage_forecast(snap.get("usage"), now),
    }

    # ready to merge: finished lanes with commits that are not in the integration branch yet
    in_train = {}
    for r in runs:
        if r["isTrain"] and r.get("train") and not r["train"]["merged"] and r["state"] in ("running", "idle", "done", "failed"):
            for b in r["train"]["branches"]:
                in_train.setdefault(b, r["train"]["k"])
    merged_branches = set()
    for r in runs:
        if r["isTrain"] and r.get("train") and r["train"]["merged"]:
            merged_branches.update(r["train"]["branches"])
    ready = []
    for key, rs in lanes.items():
        r = rs[0]
        b = git_branches.get(r["branch"] or r["lane"])
        if r["state"] != "done" or not b or not b.get("ahead") or b["name"] in merged_branches:
            continue
        ship = None
        for sec in r["sections"]:
            if "ok" in sec:
                ship = sec["ok"]
        ready.append({"key": key, "lane": r["lane"], "title": r["title"], "branch": b["name"], "ahead": b["ahead"],
                      "behind": b.get("behind", 0), "tip": b["tip"], "shipOk": ship, "train": in_train.get(b["name"], ""),
                      "pushed": not b.get("unpushed") and bool(b.get("upstream")), "end": r["end"]})
    ready.sort(key=lambda x: (x["train"] != "", -x["end"]))
    snap["ready"] = ready[:30]

    # needs you
    att = []

    def add(sev, kind, key, lane, text):
        att.append({"sev": sev, "kind": kind, "key": key, "lane": clip(lane, 100), "text": clip(text, 260)})

    idle_min = snap.get("idleMinutes", 10)
    for key, rs in lanes.items():
        r = rs[0]
        lane = r["lane"]
        for a in r["agents"]:
            if a["state"] == "failed":
                limit = any(w in (a["error"] or "").lower() for w in LIMIT_WORDS)
                add("high", "limit" if limit else "failed", key, lane, "%s: %s" % (a["label"] or a["id"], a["error"] or "failed"))
            if a["state"] in ("running", "idle") and a.get("repeat", 0) >= 4:
                add("high", "loop", key, lane, "%s repeats the same tool call (%d of its last 10): %s" % (a["label"] or a["id"], a["repeat"], a.get("now", "")))
        if r["state"] == "idle":
            add("med", "idle", key, lane, "no activity for over %g minutes (the session that started it may have ended)" % idle_min)
        elif r["state"] == "running" and r.get("eta", {}).get("overdue"):
            add("low", "slow", key, lane, "running well past its usual time (over 1.5x)")
        if r["state"] == "killed" and len(rs) == 1 and now - (r["end"] or r["activity"]) < 12 * 3600000:
            add("low", "killed", key, lane, "the run was stopped and not restarted")
        if r["state"] == "failed" and not any(a["state"] == "failed" for a in r["agents"]):
            add("high", "failed", key, lane, r["error"] or "the run failed")
        branch_info = git_branches.get(r["branch"] or r["lane"])
        shipped = bool(branch_info and (branch_info.get("ahead") == 0 or branch_info["name"] in merged_branches)) or r["branch"] in merged_branches
        if r["state"] == "done" and not shipped:
            for sec in r["sections"]:
                if sec.get("ok") is False:
                    add("high", "verdict", key, lane, "the final check says not ready to ship: " + (sec["plain"] or sec["name"]))
            issues = sum(len(sec["issues"]) for sec in r["sections"])
            if issues:
                add("low", "issues", key, lane, "%d open issues in the last result" % issues)
            b = git_branches.get(r["branch"] or r["lane"])
            tree = next((t for t in trees if b and t["branch"] == b["name"]), None)
            if tree and tree.get("dirty"):
                add("med", "dirty", key, lane, "%d uncommitted files in the worktree of a finished lane" % tree["dirty"])
            if b and b.get("ahead"):
                if b.get("unpushed"):
                    add("med", "unpushed", key, lane, "%d commits are not pushed" % b["unpushed"])
                elif not b.get("upstream"):
                    add("med", "unpushed", key, lane, "the branch has never been pushed")
    h = snap.get("handoff")
    if h and live_agents and now - h["updated"] > 3 * 3600000:
        add("low", "notes", "", "", "%s was last updated %dh ago while agents are working" % (h["name"], (now - h["updated"]) // 3600000))
    fc = (snap["burn"]["forecast"] or {}).get("fiveHour")
    if fc and fc.get("hitsBeforeReset") and fc["to100Min"] < 90:
        add("high", "usage", "", "", "5-hour usage is rising %.1f%% per hour and reaches the limit in about %d min, before the window resets" % (fc["ratePerHour"], fc["to100Min"]))
    order = {"high": 0, "med": 1, "low": 2}
    att.sort(key=lambda x: order[x["sev"]])
    snap["attention"] = att[:40]



def collect(opts):
    now = int(time.time() * 1000)
    project = Path(opts.project).resolve()
    base = Path(opts.claude_dir) if opts.claude_dir else claude_dir()
    slug = opts.project_slug or project_slug(project)
    project_dir = base / "projects" / slug
    snap = {"generated": now, "project": clip(str(project), 300), "slug": clip(slug, 200), "idleMinutes": opts.idle_minutes,
            "session": "", "sessions": [], "runs": [], "problems": []}

    sessions = list_sessions(project_dir)
    if not sessions:
        snap["problems"].append("No Claude Code sessions found under %s" % clip(str(project_dir), 300))
    chosen = []
    if opts.session == "all":
        chosen = sessions
    elif opts.session:
        chosen = [x for x in sessions if x[1].name == opts.session]
        if not chosen:
            snap["problems"].append("Session %s was not found" % clip(opts.session, 80))
    elif sessions:
        chosen = [next((x for x in sessions if x[2]), sessions[0])]
    snap["sessions"] = [{"id": clip(s[1].name, 60), "updated": s[0]} for s in sessions[:20]]
    snap["session"] = ",".join(clip(s[1].name, 60) for s in chosen)

    idle_ms = int(opts.idle_minutes * 60000)
    for _, sdir, _wf in chosen:
        _launches.update(project_dir / (sdir.name + ".jsonl"))
        snap["runs"] += load_runs(sdir, sdir.name, _LaunchView(_launches), idle_ms, now, opts.lane_field)
    snap["runs"].sort(key=lambda r: r["start"], reverse=True)
    add_eta(snap["runs"], now)
    snap["plan"] = {"items": [dict(i) for i in _launches.plan], "updated": _launches.plan_time}

    wanted = {r["branch"] for r in snap["runs"] if r["branch"]} | {r["lane"] for r in snap["runs"]}
    snap["git"] = collect_git(project, opts.integration_branch, wanted)
    usage_path = opts.usage_file or str((Path(opts.claude_dir) if opts.claude_dir else claude_dir()) / "dashboard-usage.json")
    snap["usageFile"] = clip(usage_path, 300)
    snap["usage"] = merge_usage(read_usage(usage_path), read_app_usage(opts.plan_usage_history), now)
    snap["side"] = []
    for _, sdir, _wf in chosen:
        for a in live.side_agents(sdir, now, idle_ms):
            snap["side"].append({"id": clip(a["id"], 40), "description": clip(a["description"], 160), "type": clip(a["type"], 40),
                                 "state": a["state"], "activity": a["activity"], "model": short_model(a["model"]),
                                 "outTokens": a["outTokens"], "background": a["background"]})
    snap["side"] = snap["side"][:40]
    h = live.find_handoff(project, opts.handoff_file)
    snap["handoff"] = {"name": clip(h["name"], 80), "updated": h["updated"]} if h else None
    snap["advisorLog"] = read_advisor_log((Path(opts.claude_dir) if opts.claude_dir else claude_dir()) / "dashboard-advisor" / (slug + ".jsonl"))
    derive(snap, now)
    if isinstance(snap.get("usage"), dict):
        snap["usage"].pop("points", None)
    snap["lead"] = None
    if chosen:
        tpath = project_dir / (chosen[0][1].name + ".jsonl")
        look = live.recent_tools(tpath)
        act = mtime_ms(tpath)
        step = next((i["content"] for i in _launches.plan if i["status"] == "in_progress"), "")
        snap["lead"] = {"session": clip(chosen[0][1].name, 60), "model": short_model(look["model"]), "activity": act,
                        "state": "working" if now - act < 90000 else "waiting", "now": clip(look["now"], 140),
                        "said": clip(look["said"], 300), "step": clip(step, 160),
                        "advisor": {"calls": _launches.advisor_calls, "last": clip(_launches.advisor_last, 40),
                                    "pending": {"since": _launches.advisor_pending(now)} if _launches.advisor_pending(now) else None}}
    u = snap.get("usage")
    if isinstance(u, dict) and (u.get("sources") or {}).get("app") and snap.get("lead"):
        gap = now - u["sources"]["app"]
        if gap > 20 * 60000 and now - snap["lead"]["activity"] < 5 * 60000:
            u["appSilent"] = int(gap / 60000)
    studio = None
    if opts.studio_limit:
        studio = {"count": count_processes(opts.studio_process) if opts.studio_process else None, "limit": opts.studio_limit, "label": clip(opts.slot_label, 30)}
    snap["studio"] = studio
    return snap


class _LaunchView:
    def __init__(self, idx):
        self.idx = idx

    def get(self, run_id):
        return self.idx.get(run_id)


def add_arguments(ap):
    ap.add_argument("--project", default=".", help="path of the repository the Claude Code session ran in (default: the current directory)")
    ap.add_argument("--session", default="", help="session id to show, or 'all'; default the newest session with workflow runs")
    ap.add_argument("--integration-branch", default="", help="branch lanes are compared against (default main or master)")
    ap.add_argument("--lane-field", default="", help="workflow arg that names a lane (default br, branch, lane, title)")
    ap.add_argument("--plan-usage-history", default="", help="the Claude app's plan-usage-history.json (default: found by searching the app data folders)")
    ap.add_argument("--usage-file", default="", help="usage JSON (default: <claude dir>/dashboard-usage.json, shared by all projects)")
    ap.add_argument("--studio-limit", type=int, default=0, help="show running Studio processes against this limit")
    ap.add_argument("--slot-label", default="slots", help="name shown for the --studio-limit counter (for example Studio or browsers)")
    ap.add_argument("--studio-process", default="", help="process name counted for --studio-limit (required with it; empty shows the count as unknown)")
    ap.add_argument("--idle-minutes", type=float, default=10, help="no transcript activity for this long marks an agent idle")
    ap.add_argument("--claude-dir", default="", help="Claude config dir (default ~/.claude or CLAUDE_CONFIG_DIR)")
    ap.add_argument("--handoff-file", default="", help="notes or handoff file whose age is shown (default: HANDOFF.md, NOTES.md, docs/HANDOFF.md ...)")
    ap.add_argument("--project-slug", default="", help="override the project folder name under ~/.claude/projects")


def write_atomic(path, snap):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf8")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_arguments(ap)
    ap.add_argument("--out", default="", help="write the snapshot here instead of printing it")
    opts = ap.parse_args()
    snap = collect(opts)
    if opts.out:
        write_atomic(opts.out, snap)
    else:
        json.dump(snap, sys.stdout, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
