#!/usr/bin/env python3
"""Phone export of the tree dashboard: one self-contained HTML page plus the data doc that makes it live.

python snapshot_tree.py --out tree.html --data-out tree-data.json [--status status.json | --project <repo>] [--title "Project status"] [--max-kb 200]

--out        index.html's drawing code as ONE file (no fetch, no external files). Its data comes, in order, from the
             db doc status/current when claude.use('db') resolves (and redraws on every change), else from the
             snapshot embedded in the page.
--data-out   the body of that doc: a sanitized, trimmed copy of status.json, kept under --max-kb (a db doc is capped at
             256 KiB). Older runs are cut down, worktrees and branch details are limited.
Local paths, URL credentials, key-like strings and long session ids are removed or shortened in every string.
Publishing, and refreshing the doc, are left to the caller; this script only writes the two files.
"""
import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import status as _status  # noqa: E402
from snapshot import clean  # noqa: E402

DAY = 86400000
# progressively smaller caps until the document fits
LEVELS = [
    {"sections": 6, "agents": 10, "done_sections": 4, "done_agents": 6, "stubs": 150, "age_days": 3, "worktrees": 45, "headline": 1200, "issues": 8},
    {"sections": 4, "agents": 8, "done_sections": 3, "done_agents": 4, "stubs": 80, "age_days": 2, "worktrees": 30, "headline": 700, "issues": 5},
    {"sections": 3, "agents": 6, "done_sections": 2, "done_agents": 3, "stubs": 40, "age_days": 1, "worktrees": 20, "headline": 400, "issues": 3},
    {"sections": 2, "agents": 5, "done_sections": 1, "done_agents": 2, "stubs": 15, "age_days": 1, "worktrees": 10, "headline": 250, "issues": 2},
    {"sections": 2, "agents": 4, "done_sections": 1, "done_agents": 1, "stubs": 0, "age_days": 1, "worktrees": 8, "headline": 160, "issues": 2},
]


def num(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def scrub(obj):
    """Clean every string anywhere in the document, after the specific trimming."""
    if isinstance(obj, str):
        return clean(obj, 1500)
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    return obj


def trim_section(sec, cap):
    return {
        "name": clean(sec.get("name"), 40), "plain": clean(sec.get("plain"), cap["headline"]), "tip": clean(sec.get("tip"), 10),
        "commits": [clean(c, 110) for c in (sec.get("commits") or [])[:6]],
        "issues": [clean(i, 240) for i in (sec.get("issues") or [])[:cap["issues"]]],
        "tests": clean(sec.get("tests"), 240),
        **({"ok": sec["ok"]} if isinstance(sec.get("ok"), bool) else {}),
    }


def trim_agent(a):
    return {
        "id": clean(a.get("id"), 24), "label": clean(a.get("label"), 70), "phase": clean(a.get("phase"), 40), "model": clean(a.get("model"), 24),
        "state": clean(a.get("state"), 12), "activity": num(a.get("activity")), "started": num(a.get("started")),
        "tokens": num(a.get("tokens")), "tools": num(a.get("tools")), "duration": num(a.get("duration")),
        "typicalTools": num(a.get("typicalTools")), "typicalTokens": num(a.get("typicalTokens")), "error": clean(a.get("error"), 300), "now": clean(a.get("now"), 140), "said": clean(a.get("said"), 280),
        "repeat": num(a.get("repeat")), "outTokens": num(a.get("outTokens")), "typical": num(a.get("typical")),
        "helpers": [{"desc": clean(h.get("desc"), 60), "model": clean(h.get("model"), 20), "done": bool(h.get("done"))} for h in (a.get("helpers") or [])[:4]],
    }


def trim_run(r, short, cap, full):
    base = {
        "id": clean(r.get("id"), 24), "session": short.get(r.get("session"), clean(r.get("session"), 8)), "name": clean(r.get("name"), 60),
        "state": clean(r.get("state"), 12), "title": clean(r.get("title"), 160), "lane": clean(r.get("lane"), 80), "branch": clean(r.get("branch"), 80),
        "worktree": "", "start": num(r.get("start")), "end": num(r.get("end")), "activity": num(r.get("activity")),
        "isTrain": bool(r.get("isTrain")), "model": clean(r.get("model"), 24), "tokens": num(r.get("tokens")), "agentCount": num(r.get("agentCount")),
        "error": clean(r.get("error"), 160), "scriptPath": "", "logs": [], "headline": "", "sections": [], "phases": [], "agents": [],
    }
    if not full:
        return base
    eta = r.get("eta") if isinstance(r.get("eta"), dict) else {}
    live_run = r.get("state") in ("running", "idle")
    base.update({
        "phases": [{"title": clean(p.get("title"), 30), "state": clean(p.get("state"), 12), "count": num(p.get("count"))} for p in (r.get("phases") or [])[:8]],
        "agents": [trim_agent(a) for a in (r.get("agents") or [])[:cap["agents"] if live_run else cap["done_agents"]]],
        "headline": clean(r.get("headline"), cap["headline"]),
        "sections": [trim_section(x, cap) for x in (r.get("sections") or [])[:cap["sections"] if live_run else cap["done_sections"]]],
        "logs": [clean(x, 140) for x in (r.get("logs") or [])[-3:]],
        "eta": {"known": bool(eta.get("known")), "overdue": bool(eta.get("overdue")), "remainingMs": num(eta.get("remainingMs")), "samples": num(eta.get("samples"))},
    })
    if isinstance(r.get("train"), dict):
        t = r["train"]
        base["train"] = {"k": clean(t.get("k"), 12), "branches": [clean(b, 50) for b in (t.get("branches") or [])[:20]], "merged": bool(t.get("merged")),
                         "tip": clean(t.get("tip"), 10), "conflicts": num(t.get("conflicts")), "gate": clean(t.get("gate"), 10), "smoke": clean(t.get("smoke"), 10),
                         "gateNote": clean(t.get("gateNote"), 120)}
    return base


def build_data(snap, title, cap):
    now = int(time.time() * 1000)
    sessions = {r.get("session") for r in (snap.get("runs") or []) if r.get("session")}
    short = {s: s[:8] for s in sessions}

    def rk(key):
        key = str(key or "")
        for full, sh in short.items():
            if key.startswith(full + "|"):
                return sh + key[len(full):]
        return key

    runs_in = [r for r in (snap.get("runs") or []) if isinstance(r, dict)]
    latest = {}
    for r in sorted(runs_in, key=lambda r: num(r.get("start")), reverse=True):
        if not r.get("isTrain"):
            latest.setdefault((r.get("session"), r.get("lane")), r["id"])
    trains = [r["id"] for r in sorted((r for r in runs_in if r.get("isTrain")), key=lambda r: num(r.get("start")), reverse=True)[:6]]
    full_ids = set(latest.values()) | set(trains) | {r["id"] for r in runs_in if r.get("state") in ("running", "idle")}
    keep, stubs = [], 0
    for r in sorted(runs_in, key=lambda r: num(r.get("start")), reverse=True):
        is_full = r["id"] in full_ids
        recent = now - max(num(r.get("end")), num(r.get("activity")), num(r.get("start"))) < cap["age_days"] * DAY
        if not is_full:
            if not recent or stubs >= cap["stubs"]:
                continue
            stubs += 1
        keep.append(trim_run(r, short, cap, is_full))

    git = snap.get("git") or {}
    lane_branches = {r["branch"] for r in keep if r.get("branch")} | {r["lane"] for r in keep}
    wts = [{"name": clean(w.get("name"), 60), "branch": clean(w.get("branch"), 80), "tip": clean(w.get("tip"), 10), "ahead": w.get("ahead"), "behind": w.get("behind"),
            "dirty": w.get("dirty"), "detached": bool(w.get("detached")), "path": ""} for w in (git.get("worktrees") or []) if w.get("branch")][:cap["worktrees"]]
    branches = {}
    for name, b in (git.get("branches") or {}).items():
        if name in lane_branches or name in {w["branch"] for w in wts}:
            branches[clean(name, 80)] = {"name": clean(b.get("name") or name, 80), "tip": clean(b.get("tip"), 10), "ahead": b.get("ahead"), "behind": b.get("behind"),
                                         "unpushed": num(b.get("unpushed")), "upstream": clean(b.get("upstream"), 40), "gone": bool(b.get("gone"))}
    usage = snap.get("usage") if isinstance(snap.get("usage"), dict) and not snap["usage"].get("error") else None
    if usage:
        usage = {"updated": num(usage.get("updated")), "appSilent": num(usage.get("appSilent")), "windows": [{"label": clean(w.get("label"), 40), "percent": w.get("percent"), "resets": clean(w.get("resets"), 40), "from": clean(w.get("from"), 8),
                                                  "at": num(w.get("at")), "resetSince": bool(w.get("resetSince"))} for w in (usage.get("windows") or [])[:4]],
                 **{k: usage[k] for k in ("fiveHour", "weekly") if isinstance(usage.get(k), dict)}}
    lead = snap.get("lead") if isinstance(snap.get("lead"), dict) else None
    if lead:
        adv = lead.get("advisor") or {}
        lead = {"session": short.get(lead.get("session"), clean(lead.get("session"), 8)), "model": clean(lead.get("model"), 24), "activity": num(lead.get("activity")),
                "state": clean(lead.get("state"), 10), "now": clean(lead.get("now"), 120), "said": clean(lead.get("said"), 240), "step": clean(lead.get("step"), 140),
                "advisor": {"calls": num(adv.get("calls")), "last": clean(adv.get("last"), 30),
                            "pending": {"since": num((adv.get("pending") or {}).get("since"))} if isinstance(adv.get("pending"), dict) else None}}
    burn = snap.get("burn") or {}
    data = {
        "title": clean(title, 60), "project": clean(Path(str(snap.get("project") or "")).name, 40), "session": ",".join(sorted(short.values()))[:40],
        "generated": num(snap.get("generated")) or now, "idleMinutes": snap.get("idleMinutes", 10), "problems": [clean(p, 140) for p in (snap.get("problems") or [])[:2]],
        "runs": keep,
        "git": {"available": bool(git.get("available")), "integration": clean(git.get("integration"), 40), "worktrees": wts, "branches": branches},
        "usage": usage, "usageFile": "",
        "plan": {"items": [{"content": clean(i.get("content"), 160), "status": clean(i.get("status"), 14)} for i in ((snap.get("plan") or {}).get("items") or [])[:14]],
                 "updated": clean((snap.get("plan") or {}).get("updated"), 30)},
        "side": [{"id": clean(a.get("id"), 20), "description": clean(a.get("description"), 100), "type": clean(a.get("type"), 30), "state": clean(a.get("state"), 10),
                  "activity": num(a.get("activity")), "model": clean(a.get("model"), 24), "outTokens": num(a.get("outTokens")), "background": bool(a.get("background"))}
                 for a in (snap.get("side") or [])[:10]],
        "handoff": {"name": clean((snap.get("handoff") or {}).get("name"), 40), "updated": num((snap.get("handoff") or {}).get("updated"))} if snap.get("handoff") else None,
        "lead": lead,
        "advisorLog": [{"at": num(e.get("at")), "asked": clean(e.get("asked"), 300), "advice": clean(e.get("advice"), 300), "action": clean(e.get("action"), 240),
                        "model": clean(e.get("model"), 24)} for e in (snap.get("advisorLog") or [])[:10]],
        "burn": {"finished1h": num(burn.get("finished1h")), "finished5h": num(burn.get("finished5h")), "byFamily5h": burn.get("byFamily5h") or {},
                 "liveAgents": num(burn.get("liveAgents")), "liveOutTokens": num(burn.get("liveOutTokens")), "forecast": burn.get("forecast"),
                 "topLanes": [{"key": rk(x.get("key")), "lane": clean(x.get("lane"), 40), "title": clean(x.get("title"), 80), "tokens": num(x.get("tokens"))} for x in (burn.get("topLanes") or [])[:5]]},
        "ready": [{"key": rk(x.get("key")), "lane": clean(x.get("lane"), 40), "title": clean(x.get("title"), 100), "branch": clean(x.get("branch"), 60), "ahead": x.get("ahead"),
                   "behind": x.get("behind"), "tip": clean(x.get("tip"), 10), "shipOk": x.get("shipOk"), "train": clean(x.get("train"), 12), "pushed": x.get("pushed"),
                   "end": num(x.get("end"))} for x in (snap.get("ready") or [])[:12]],
        "attention": [{"sev": clean(x.get("sev"), 6), "kind": clean(x.get("kind"), 16), "key": rk(x.get("key")), "lane": clean(x.get("lane"), 50), "text": clean(x.get("text"), 220)}
                      for x in (snap.get("attention") or [])[:25]],
        "studio": ({"count": (snap["studio"] or {}).get("count"), "limit": snap["studio"].get("limit"), "label": clean(snap["studio"].get("label"), 20)} if snap.get("studio") else None),
    }
    return scrub(data)


def make_data(snap, title, max_kb):
    for cap in LEVELS:
        data = build_data(snap, title, cap)
        size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        if size <= max_kb * 1024:
            return data, size, cap
    return data, size, cap


def render_page(data):
    src = (HERE / "index.html").read_text(encoding="utf-8")
    a, b = src.find("/*SOURCE_BEGIN*/"), src.find("/*SOURCE_END*/")
    if a < 0 or b < 0:
        raise SystemExit("index.html has no SOURCE markers")
    source = '''
var seed = JSON.parse(document.getElementById("seed").textContent);
ingest(seed, "seed:" + seed.generated);
setInterval(tickMeta, 15000);
// subscribe to the doc; subscribing again (when the page is shown) delivers the current doc at once
var unsub = null;
function subscribe() {
  if (!(window.claude && window.claude.use)) return;
  window.claude.use("db").then(function (db) {
    if (!db) return;
    if (typeof unsub === "function") { try { unsub(); } catch (e) {} }
    unsub = db.doc("status/current").onSnapshot(function (snap) {
      var d = snap.exists && snap.data();
      if (d && d.generated && (!data || d.generated >= data.generated)) ingest(d, "doc:" + d.generated);
    }, function () {});
  }).catch(function () {});
}
refreshNow = subscribe;
subscribe();
'''
    out = src[:a] + source + src[b + len("/*SOURCE_END*/"):]
    # the local fetch loop is dead code in the phone page: remove it so the page never requests status.json
    p0, p1 = out.find("function poll() {"), out.find("function setTheme(t) {")
    if p0 < 0 or p1 < p0:
        raise SystemExit("index.html has no poll() to strip")
    out = out[:p0] + out[p1:]
    out = out.replace("// Everything from status.json is untrusted text", "// Everything in the data is untrusted text", 1)
    flag = "var STALE_MS = 120000, PHONE = false"
    if flag not in out:
        raise SystemExit("snapshot_tree: the PHONE flag line was not found in index.html; update the pattern")
    out = out.replace(flag, "var STALE_MS = 900000, PHONE = true", 1)
    out = out.replace("<title>Agent control room</title>", "<title>%s</title>" % data["title"].replace("<", "&lt;"), 1)
    seed = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    i = out.index("<script>")
    return out[:i] + '<script id="seed" type="application/json">' + seed + "</script>\n" + out[i:]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--data-out", default="")
    ap.add_argument("--status", default="")
    ap.add_argument("--project", default="")
    ap.add_argument("--title", default="Project status")
    ap.add_argument("--max-kb", type=int, default=200)
    opts = ap.parse_args()
    if opts.status:
        path = Path(opts.status)
    else:
        project = Path(opts.project or ".").resolve()
        base = Path(tempfile.gettempdir()) / "multi-agent-dashboard" / _status.project_slug(project)
        cands = sorted(base.glob("status*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not cands:
            raise SystemExit("no status json under %s; start run.py first or pass --status" % base)
        path = cands[0]
    snap = json.loads(path.read_text(encoding="utf-8"))
    data, size, cap = make_data(snap, opts.title, opts.max_kb)
    page = render_page(data)
    Path(opts.out).write_text(page, encoding="utf-8")
    if opts.data_out:
        Path(opts.data_out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print("page %s: %.0f KB; data %s: %.0f KB (limit %d KB); runs %d" % (opts.out, len(page.encode("utf-8")) / 1024, opts.data_out or "-", size / 1024, opts.max_kb, len(data["runs"])))
    if size > opts.max_kb * 1024:
        print("WARNING: data is over the limit", file=sys.stderr)


if __name__ == "__main__":
    main()
