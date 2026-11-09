"""Loopback-only dashboard. POSTs configure/schedule local read-only monitoring."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

from .client import Config, repository_selection
from .demo import DemoClient
from .monitor import Monitor

ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def handler_for(monitor: Monitor):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format, *args):
            pass

        def _allowed(self) -> bool:
            assert isinstance(self.server, ThreadingHTTPServer)
            port = self.server.server_port
            host = self.headers.get("Host", "")
            valid_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            if self.command == "GET" and self.path in ASSETS:
                # Public static assets contain no assessment data. Permit browser
                # navigation from another app/site, but still reject foreign Hosts.
                return host in valid_hosts
            return (
                host in valid_hosts
                and (origin is None or origin == f"http://{host}")
                and self.headers.get("Sec-Fetch-Site", "none") in {"none", "same-origin"}
            )

        def _reply(self, status: int, data: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # Browser disconnected; this does not change assessment state.
                return

        def _json(self, status: int, data):
            self._reply(status, json.dumps(data).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not self._allowed():
                return self._json(403, {"error": "Only same-origin loopback access is allowed"})
            if self.path == "/api/state":
                return self._json(200, monitor.snapshot())
            if self.path in ASSETS:
                name, content_type = ASSETS[self.path]
                return self._reply(200, files("impact").joinpath("static", name).read_bytes(), content_type)
            return self._json(404, {"error": "Not found"})

        def do_POST(self):
            if not self._allowed():
                return self._json(403, {"error": "Only same-origin loopback access is allowed"})
            if self.path == "/api/repositories":
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type") != "application/json":
                    return self._json(400, {"error": "Repository selection requires bounded JSON"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= length <= 49152:
                        raise ValueError("Repository selection body must be 1..49152 bytes")
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError("Incomplete repository selection")
                    data = json.loads(raw)
                    if not isinstance(data, dict) or set(data) != {"mode", "repositories"} or not isinstance(data["repositories"], list):
                        raise ValueError("Expected mode and repository list only")
                    status = monitor.select_repositories(data["mode"], data["repositories"])
                except (ValueError, UnicodeDecodeError, TypeError, RecursionError):
                    return self._json(400, {"error": "Invalid selection: provide selected/all/paused mode and valid owner/repo names within the displayed limit"})
                except (OSError, TimeoutError):
                    return self._json(408, {"error": "Repository selection body timed out"})
                return self._json(202 if status == "accepted" else 200 if status == "unchanged" else 409, {"status": status})
            if self.path != "/api/refresh":
                return self._json(404, {"error": "Not found"})
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                return self._json(400, {"error": "Refresh accepts no request body"})
            status = monitor.refresh()
            return self._json(202 if status == "accepted" else 409, {"status": status})

    return Handler


def make_server(monitor: Monitor) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", monitor.config.port), handler_for(monitor))
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="Local read-only GitHub incident impact monitor")
    parser.add_argument("--demo", action="store_true", help="Use incident fixtures only: no network or authentication")
    parser.add_argument("--repos", nargs="+", metavar="OWNER/REPO", help="Start with exact repositories; skips account-wide discovery")
    parser.add_argument("--all-repositories", action="store_true", help="Opt in to credential-visible account-wide discovery")
    args = parser.parse_args()
    try:
        config = Config.from_env()
        if args.repos and args.all_repositories:
            parser.error("--repos and --all-repositories are mutually exclusive")
        selected = repository_selection(args.repos, config.max_repositories) if args.repos else (
            None if args.all_repositories or args.demo else ()
        )
    except ValueError as error:
        parser.error(str(error))
    monitor = Monitor(config, client_factory=DemoClient if args.demo else None, demo=args.demo, selected=selected)
    try:
        server = make_server(monitor)
    except OSError:
        print(f"Cannot bind 127.0.0.1:{config.port}; check port availability and permissions", file=sys.stderr)
        return 1
    shutdown = threading.Event()

    def stop_signal(signum, frame):
        shutdown.set()

    signal.signal(signal.SIGTERM, stop_signal)
    signal.signal(signal.SIGINT, stop_signal)
    server.timeout = 0.5
    monitor.start()
    print(f"{'FIXTURE DEMO' if args.demo else 'Live monitor'}: http://127.0.0.1:{config.port} (Ctrl-C to stop)", flush=True)
    try:
        while not shutdown.is_set():
            server.handle_request()
    finally:
        monitor.stop()
        server.server_close()
    return 0
