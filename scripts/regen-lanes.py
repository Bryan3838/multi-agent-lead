#!/usr/bin/env python3
"""Make relaunchable copies of stopped workflow lanes from their saved run records.

python regen-lanes.py --runs <workflows dir> --script <branch-run.js> --out <folder> name=runId [name=runId ...] [--note "..."]

A stopped run keeps its args in <runs>/<runId>.json (key "args"). For each name=runId this writes <out>/branch-run-<name>.js: a copy
of the generic script with the saved args pasted in place of `const A = args` (and its own meta name), so relaunching it is one Workflow
call with that scriptPath and no retyped arguments. `--note` is appended to each lane's `state` text (the default tells the builder
that an earlier run stopped partway and to read git log and git status before continuing).
Nothing is launched here. Use forward slashes in the paths on Windows.
"""
import argparse
import json
import sys
from pathlib import Path

DEFAULT_NOTE = (
    " NOTE: an earlier run of this lane was stopped partway (usage): its worktree may already hold commits and uncommitted work;"
    " read git log and git status first and continue from there, do not start over."
)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", required=True, help="the session's workflows folder (holds <runId>.json records)")
    ap.add_argument("--script", required=True, help="the generic workflow script, e.g. branch-run.js")
    ap.add_argument("--out", required=True, help="folder for the generated copies (must be readable by the Workflow tool)")
    ap.add_argument("--note", default=DEFAULT_NOTE)
    ap.add_argument("lanes", nargs="+", help="name=runId")
    opts = ap.parse_args()
    src = Path(opts.script).read_text(encoding="utf-8")
    marker = "const A = args"
    if marker not in src:
        sys.exit("regen-lanes: the script has no `const A = args` line")
    out = Path(opts.out)
    out.mkdir(parents=True, exist_ok=True)
    for item in opts.lanes:
        name, _, run_id = item.partition("=")
        record = Path(opts.runs) / (run_id + ".json")
        if not name or not run_id or not record.exists():
            sys.exit("regen-lanes: no record for %r (%s)" % (item, record))
        args = json.loads(record.read_text(encoding="utf-8")).get("args")
        if not isinstance(args, dict):
            sys.exit("regen-lanes: %s has no saved args" % record)
        if isinstance(args.get("state"), str) and opts.note:
            args["state"] += opts.note
        text = src.replace(marker, "const A = " + json.dumps(args), 1)
        text = text.replace("name: 'branch-run'", "name: 'branch-run-%s'" % name, 1)
        target = out / ("branch-run-%s.js" % name)
        target.write_text(text, encoding="utf-8")
        print("wrote", target)


if __name__ == "__main__":
    main()
