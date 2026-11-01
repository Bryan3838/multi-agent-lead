#!/usr/bin/env python3
"""Tiny local server for the dashboard: serves index.html and the status.json written by run.py.

Binds to 127.0.0.1 only and serves exactly three paths (the page, status.json, projects.json), so nothing else on disk is exposed.
"""
import http.server
import json
import socket
from pathlib import Path

HERE = Path(__file__).resolve().parent
CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'"


class Server(http.server.ThreadingHTTPServer):
    """Binds exclusively: on Windows the default SO_REUSEADDR lets a second server bind a port that is already in use."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_handler(status_path, projects_fn=None):
    path_of = status_path if callable(status_path) else (lambda: Path(status_path))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost", "::1"):
                return self._send(403, b"forbidden", "text/plain")
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/", "/index.html"):
                    return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
                if path == "/projects.json":
                    return self._send(200, json.dumps({"projects": projects_fn() if projects_fn else []}).encode("utf8"), "application/json; charset=utf-8")
                if path == "/status.json":
                    return self._send(200, Path(path_of()).read_bytes(), "application/json; charset=utf-8")
            except OSError:
                return self._send(503, b'{"problems":["status.json is not written yet"]}', "application/json")
            self._send(404, b"not found", "text/plain")

    return Handler


def bind(status_path, port, tries=20, projects_fn=None):
    handler = make_handler(status_path, projects_fn)
    for p in range(port, port + tries):
        try:
            return Server(("127.0.0.1", p), handler)
        except OSError:
            continue
    raise SystemExit("no free port from %d to %d" % (port, port + tries - 1))
