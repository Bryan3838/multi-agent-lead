#!/usr/bin/env python3
"""Start the live dashboard: python run.py --project <repo> [flags]

Refreshes status.json every 5 s in a background thread (git and process counts every 30 s, the slow part) and serves
the page on 127.0.0.1. Costs no model tokens. Stop it with Ctrl+C (or by stopping its task).
"""
import argparse
import os
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402
import serve  # noqa: E402
import status  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    status.add_arguments(ap)
    ap.add_argument("--port", type=int, default=0, help="port to bind; 0 (default) uses a stable port derived from the project folder, moving up if it is busy")
    ap.add_argument("--print-port", action="store_true", help="print the default port for --project and exit (use it to fill in a launch.json entry)")
    ap.add_argument("--interval", type=float, default=5, help="seconds between status.json writes (git is refreshed every --git-interval)")
    ap.set_defaults(git_interval=30)
    ap.add_argument("--out-dir", default="", help="where status.json goes (default: a per-project folder in the temp dir)")
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    ap.add_argument("--strict-port", action="store_true", help="fail if the port is taken instead of using the next free one")
    opts = ap.parse_args()

    project = Path(opts.project).resolve()
    if opts.print_port:
        print(registry.default_port(project))
        return
    port = opts.port or registry.default_port(project)
    out_dir = Path(opts.out_dir) if opts.out_dir else Path(tempfile.gettempdir()) / "multi-agent-dashboard" / status.project_slug(project)
    holder = {"path": out_dir / "status.json"}

    entry = {}

    def refresh():
        counts = None
        try:
            snap = status.collect(opts)
            status.write_atomic(holder["path"], snap)
            counts = registry.summarize(snap)
        except Exception as e:  # keep the loop alive; the page shows the age of the last good write
            print("status refresh failed: %s" % e, file=sys.stderr, flush=True)
        if entry:
            entry["e"].beat(counts)  # heartbeat: other dashboards list this one while it keeps beating

    srv = serve.bind(lambda: holder["path"], port, 1 if opts.strict_port else 30,
                     lambda: registry.list_live(opts.claude_dir))
    real_port = srv.server_address[1]
    out = holder["path"] = out_dir / ("status-%d.json" % real_port)
    entry["e"] = registry.Entry(project, real_port, opts.claude_dir)
    entry["e"].beat()
    refresh()

    def loop():
        while True:
            time.sleep(opts.interval)
            refresh()

    threading.Thread(target=loop, daemon=True).start()
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    print("Dashboard for %s\n%s\n(port %d registered; status.json: %s, refreshed every %gs)" % (project, url, real_port, out, opts.interval), flush=True)
    if opts.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        entry["e"].remove()


if __name__ == "__main__":
    main()
