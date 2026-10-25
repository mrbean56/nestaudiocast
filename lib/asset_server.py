"""Tiny static file server with HTTP Range support (Cast devices need 206 Partial
Content to seek inside a buffered stream; python's SimpleHTTPRequestHandler does not
implement it, which makes every SEEK restart from byte 0).

Also logs every request so we can prove what the group actually fetched.
"""
from __future__ import annotations

import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TYPES = {
    ".m4a": "audio/mp4", ".mp4": "video/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".webm": "audio/webm",
    ".ts": "video/mp2t", ".m3u8": "application/vnd.apple.mpegurl",
    ".vtt": "text/vtt",
}


class Handler(BaseHTTPRequestHandler):
    root = "."
    log_path: str | None = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        line = "%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args)
        if self.log_path:
            with open(self.log_path, "a") as fh:
                fh.write(line)

    def _resolve(self):
        path = self.path.split("?")[0]
        path = re.sub(r"^/+", "", path)
        full = os.path.realpath(os.path.join(self.root, path))
        if not full.startswith(os.path.realpath(self.root)):
            return None
        return full if os.path.isfile(full) else None

    def do_HEAD(self):  # noqa: N802
        self._serve(head=True)

    def do_GET(self):  # noqa: N802
        self._serve(head=False)

    def _serve(self, head: bool):
        full = self._resolve()
        if not full:
            self.send_error(404)
            return
        size = os.path.getsize(full)
        ctype = TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if head:
            return
        with open(full, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)


class AssetServer:
    def __init__(self, root: str, host: str = "192.168.2.18", port: int = 8099,
                 log_path: str | None = None):
        Handler.root = os.path.abspath(root)
        Handler.log_path = log_path
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    import sys

    root = sys.argv[1] if len(sys.argv) > 1 else "assets"
    srv = AssetServer(root, log_path="logs/asset-server.log").start()
    print(f"serving {os.path.abspath(root)} on http://192.168.2.18:8099")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
