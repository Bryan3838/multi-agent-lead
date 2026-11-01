"""Lets several dashboard servers (one per project) find each other.

Each run.py writes ONE small file, <claude dir>/dashboard-projects/<port>.json, and rewrites it on every status refresh
(the heartbeat). Because every server only ever writes its own file there is nothing to lock. A page lists the entries
whose heartbeat is recent; an entry whose heartbeat stops (the server was killed) simply ages out and is deleted later.
Entries hold only a display name, the port and a few counts: no full paths.
"""
import atexit
import json
import os
import re
import time
import zlib
from pathlib import Path

LIVE_SECONDS = 120
PRUNE_SECONDS = 600
PORT_BASE, PORT_SPAN = 8700, 300


def claude_dir(override=""):
    if override:
        return Path(override)
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def registry_dir(claude=""):
    return claude_dir(claude) / "dashboard-projects"


def default_port(project):
    """A stable port for a project, so a launch.json entry and the running server agree and two projects rarely collide."""
    key = str(Path(project).resolve()).lower().replace("\\", "/")
    return PORT_BASE + zlib.crc32(key.encode("utf8")) % PORT_SPAN


def summarize(snap):
    """A few cheap counts for the dropdown: lanes running and items that need a person."""
    lanes = {}
    for r in snap.get("runs") or []:
        if not r.get("isTrain"):
            key = (r.get("session"), r.get("lane"))
            if key not in lanes:
                lanes[key] = r.get("state")
    running = sum(1 for s in lanes.values() if s in ("running", "idle"))
    needs = sum(1 for a in snap.get("attention") or [] if a.get("sev") in ("high", "med"))
    return {"lanes": len(lanes), "running": running, "needs": needs}


class Entry:
    def __init__(self, project, port, claude=""):
        project = Path(project).resolve()
        self.dir = registry_dir(claude)
        self.path = self.dir / ("%d.json" % port)
        self.data = {"name": project.name or str(project), "parent": project.parent.name, "port": int(port), "pid": os.getpid(),
                     "started": int(time.time()), "heartbeat": int(time.time()), "lanes": 0, "running": 0, "needs": 0}
        atexit.register(self.remove)

    def beat(self, counts=None):
        if counts:
            self.data.update({k: int(counts.get(k, 0)) for k in ("lanes", "running", "needs")})
        self.data["heartbeat"] = int(time.time())
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def remove(self):
        try:
            self.path.unlink()
        except OSError:
            pass


def list_live(claude="", max_age=LIVE_SECONDS):
    """Entries with a recent heartbeat, newest port first-seen order; old entries are deleted."""
    out, now = [], time.time()
    d = registry_dir(claude)
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            e = json.loads(f.read_text(encoding="utf8"))
            port = int(e["port"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        age = now - float(e.get("heartbeat") or 0)
        if age > PRUNE_SECONDS:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        if age > max_age or not (1024 <= port <= 65535):
            continue
        out.append({"name": re.sub(r"[\x00-\x1f]", "", str(e.get("name") or ""))[:60], "parent": re.sub(r"[\x00-\x1f]", "", str(e.get("parent") or ""))[:40],
                    "port": port, "started": int(e.get("started") or 0), "age": int(age),
                    "lanes": int(e.get("lanes") or 0), "running": int(e.get("running") or 0), "needs": int(e.get("needs") or 0)})
    out.sort(key=lambda e: (e["name"].lower(), e["port"]))
    return out
