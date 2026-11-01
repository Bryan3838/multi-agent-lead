#!/usr/bin/env python3
"""Append one note about an advisor consult, so the dashboard can show what the advisor said and what the lead did.

python advisor_log.py --project <repo> --asked "what you consulted about" --advice "what it said, 1-3 sentences" --action "what you did about it" [--model opus]

The advisor's own replies are encrypted in the session file, so the lead writes this note itself after every consult.
It appends one JSON line to <claude dir>/dashboard-advisor/<project slug>.jsonl (the slug the status files use):
{"at": <ms>, "asked": ..., "advice": ..., "action": ..., "model": ...}. All fields are plain text, control
characters removed, each clipped to 400 characters (model 40). The dashboard reads the newest 20 lines.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import status as _status  # noqa: E402

CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def tidy(value, n):
    s = " ".join(CTRL.sub("", str(value or "")).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def log_path(project, claude_dir="", slug=""):
    base = Path(claude_dir) if claude_dir else _status.claude_dir()
    return base / "dashboard-advisor" / ((slug or _status.project_slug(Path(project).resolve())) + ".jsonl")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=".", help="the repository (default: the current directory)")
    ap.add_argument("--asked", required=True, help="what decision it was consulted about, one line")
    ap.add_argument("--advice", required=True, help="what the advisor said, 1-3 short sentences")
    ap.add_argument("--action", default="", help="what the lead did about it, one line")
    ap.add_argument("--model", default="opus", help="the advisor's model, for example opus")
    ap.add_argument("--claude-dir", default="", help="Claude config dir (default ~/.claude or CLAUDE_CONFIG_DIR)")
    ap.add_argument("--project-slug", default="", help="override the project slug")
    opts = ap.parse_args()
    entry = {"at": int(time.time() * 1000), "asked": tidy(opts.asked, 400), "advice": tidy(opts.advice, 400),
             "action": tidy(opts.action, 400), "model": tidy(opts.model, 40).lower()}
    if not entry["asked"] or not entry["advice"]:
        raise SystemExit("--asked and --advice must not be empty")
    path = log_path(opts.project, opts.claude_dir, opts.project_slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    lead_in = ""
    try:
        with open(path, "rb") as f:
            f.seek(-1, 2)
            lead_in = "" if f.read(1) == b"\n" else "\n"  # an earlier half-written line must not swallow this one
    except OSError:
        pass
    with open(path, "a", encoding="utf8") as f:
        f.write(lead_in + json.dumps(entry, ensure_ascii=False) + "\n")  # one write, so a reader never sees half a line for long
    print("logged to %s" % path)


if __name__ == "__main__":
    main()
