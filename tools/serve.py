#!/usr/bin/env python3
"""serve.py — an optional local dev server for a static web product, on ONE fixed address.

    python tools/serve.py                 # serve [web] dir on [web] port (default: site/ on 8080)
    python tools/serve.py --port 8123 --dir public
    python tools/serve.py --check         # exit 0 when OUR server answers on that port

Why a fixed port: a web app's storage (localStorage, IndexedDB, cookies) is keyed by origin, so a
server that quietly moves to another port makes the saved data «vanish». If the port is busy this
refuses and says so; it never picks a different one. Stateful apps should keep one URL for tests.

Development only: loopback address, no-cache headers (an edited file shows on reload), UTF-8. It
announces itself with an `X-Producer-Dev-Server: <project>` header, which `--check` uses to tell
this server from whatever else might hold the port. If the product has its own dev server (Vite,
Next, …) use that instead and skip this tool. Stop with Ctrl+C, or end the process that started it.
"""
from __future__ import annotations

import argparse
import http.server
import socketserver
import sys
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path

import paths

HEADER = "X-Producer-Dev-Server"
HOST = "127.0.0.1"
DEFAULT_PORT = 8080


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static files with development headers. Quiet unless asked, so a background run stays tidy."""

    identity = "producer"

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header(HEADER, self.identity)
        super().end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - the base class signature
        if getattr(self.server, "verbose", False):
            super().log_message(format, *args)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # a busy port must fail, not be shared
    verbose = False


def make_server(directory: Path, port: int, identity: str, verbose: bool = False) -> Server:
    """Bind `port` on loopback or raise OSError - never another port."""
    handler = partial(type("DevHandler", (Handler,), {"identity": identity}), directory=str(directory))
    server = Server((HOST, port), handler)
    server.verbose = verbose
    return server


def is_ours(port: int, identity: str, timeout: float = 2.0) -> bool:
    """Whether the thing answering on `port` carries our identity header."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/", timeout=timeout) as reply:
            return reply.headers.get(HEADER) == identity
    except urllib.error.HTTPError as error:  # a 404 from our own server still carries the header
        return error.headers.get(HEADER) == identity
    except (urllib.error.URLError, OSError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=None, help="folder to serve (default: [web] dir, else site/)")
    parser.add_argument("--port", type=int, default=None, help="port (default: [web] port, else 8080)")
    parser.add_argument("--check", action="store_true", help="exit 0 if this project's server answers")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args(argv)
    paths.console_safe()
    directory = (paths.PROJECT / (args.dir or paths.web_dir())).resolve()
    port = args.port if args.port is not None else paths.web_port()
    identity = paths.project_name()
    if args.check:
        ok = is_ours(port, identity)
        print(f"{'up' if ok else 'not running'}: http://{HOST}:{port}/")
        return 0 if ok else 1
    if not directory.is_dir():
        print(f"nothing to serve: {directory} is not a folder (set [web] dir in producer.toml)", file=sys.stderr)
        return 2
    try:
        server = make_server(directory, port, identity, args.verbose)
    except OSError as error:
        mine = is_ours(port, identity)
        print(f"port {port} is busy ({error.strerror or error}); "
              + ("it is already this project's server: http://%s:%d/" % (HOST, port) if mine else
                 "something else holds it. Free it or choose a port - this tool never moves to another one."),
              file=sys.stderr)
        return 0 if mine else 1
    print(f"serving {directory} at http://{HOST}:{port}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
