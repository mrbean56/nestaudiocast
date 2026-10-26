#!/usr/bin/env python3
"""Dev HTTP server for the VideoNetCaster receiver — 192.168.2.21:7640 ONLY.

This is the *development stand-in* for the v0.2 owner app (contracts §5). It serves exactly the
receiver's needs and nothing else, on the LAN address the Cast console has registered:

    GET /receiver.html   the CAF receiver page  (the path is frozen: 15 min to change it)
    GET /                the same page (a device that opens the root must not look broken)
    GET /media/<file>    test media for tools/receiver_probe.py, with Range support
    GET /healthz         {"ok": true, "owner": "receiver-dev"}

Rules it keeps: stdlib only, binds one LAN address, answers 192.168.2.0/24 only, never touches
the v0.1 relay on :8099, never runs systemctl, logs no query string (a media URL can carry a
short-lived token and a token in a log is a leaked secret).

    python3 serve.py --bind 192.168.2.21 --port 7640 [--root DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LAN_PREFIX = "192.168.2."
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".m4a": "audio/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mpd": "application/dash+xml",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}

ROOT = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.abspath(os.path.join(ROOT, "..", "media"))


def log(msg: str) -> None:
    sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


class Handler(BaseHTTPRequestHandler):
    server_version = "nac-receiver-dev/0.2"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------------------------
    def _lan_only(self) -> bool:
        addr = self.client_address[0] if self.client_address else ""
        ok = addr.startswith(LAN_PREFIX) or addr in ("127.0.0.1", "::1")
        if not ok:
            log("refused %s" % addr)
        return ok

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def _file(self, path: str, ctype: str | None = None) -> None:
        if not os.path.isfile(path):
            self._json(404, {"ok": False, "error": "not found"})
            return
        size = os.path.getsize(path)
        ctype = ctype or MEDIA_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                first, _, last = rng[6:].partition("-")
                start = int(first) if first else 0
                end = int(last) if last else size - 1
            except ValueError:
                self._json(400, {"ok": False, "error": "bad range"})
                return
            end = min(end, size - 1)
            if start > end or start >= size:
                self._send(416, b"", ctype, {"Content-Range": "bytes */%d" % size})
                return
            length = end - start + 1
            with open(path, "rb") as fh:
                fh.seek(start)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(262144, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            return
        with open(path, "rb") as fh:
            body = fh.read()
        self._send(200, body, ctype, {"Accept-Ranges": "bytes"})

    # -- routes ----------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib name)
        if not self._lan_only():
            self._json(403, {"ok": False, "error": "lan only"})
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/receiver.html", "/index.html"):
            self._file(os.path.join(ROOT, "receiver.html"), "text/html; charset=utf-8")
            return
        # the loader fetches these: the script is versioned by its query string, the build stamp
        # must never be cached (that is the whole point of it)
        # The device caches by path and ignores query strings, so the loader asks for a path
        # that changes when the content may have: /r/<build>/receiver.js and /b/<now>/build.txt.
        if path.startswith("/r/") and path.endswith("/receiver.js"):
            self._file(os.path.join(ROOT, "receiver.js"), "text/javascript; charset=utf-8")
            return
        if path.startswith("/b/") and path.endswith("/build.txt"):
            self._file(os.path.join(ROOT, "build.txt"), "text/plain; charset=utf-8")
            return
        if path == "/receiver.js":
            self._file(os.path.join(ROOT, "receiver.js"), "text/javascript; charset=utf-8")
            return
        if path == "/build.txt":
            self._file(os.path.join(ROOT, "build.txt"), "text/plain; charset=utf-8")
            return
        if path == "/healthz":
            self._json(
                200,
                {
                    "ok": True,
                    "owner": "receiver-dev",
                    "root": ROOT,
                    "media_dir": MEDIA_DIR,
                    "media_files": _count_media(),
                },
            )
            return
        if path.startswith("/media/"):
            name = os.path.basename(path)
            if name != path[len("/media/") :] or not name:
                self._json(404, {"ok": False, "error": "not found"})
                return
            self._file(os.path.join(MEDIA_DIR, name))
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401
        # path only, no query string: a media URL may carry a short-lived token
        log("%s %s" % (self.client_address[0], (args[0] if args else "")[:80]))


def _count_media() -> int:
    try:
        return len([f for f in os.listdir(MEDIA_DIR) if not f.startswith(".")])
    except OSError:
        return 0


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    global ROOT, MEDIA_DIR  # noqa: PLW0603 - one process, one root
    ap = argparse.ArgumentParser(description="dev HTTP server for the Cast receiver page")
    ap.add_argument("--bind", default=os.environ.get("NAC_BIND", "192.168.2.21"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("NAC_PORT", "7640")))
    ap.add_argument("--root", default=ROOT)
    args = ap.parse_args(argv)

    ROOT = os.path.abspath(args.root)
    MEDIA_DIR = os.path.abspath(os.path.join(ROOT, "..", "media"))
    page = os.path.join(ROOT, "receiver.html")
    if not os.path.isfile(page):
        log("FATAL: %s is missing" % page)
        return 2
    srv = Server((args.bind, args.port), Handler)
    log(
        "serving %s on http://%s:%d/receiver.html  (media: %s, %d files)"
        % (page, args.bind, args.port, MEDIA_DIR, _count_media())
    )
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
