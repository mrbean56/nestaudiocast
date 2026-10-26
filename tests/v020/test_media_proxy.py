"""The range proxy against a stub source: no real media, no YouTube (task B, 3).

The stub is a local HTTP server that speaks ``Range``, can answer 403 on demand (a stale token)
and can drop a connection mid-body (what an expired token looks like from the proxy's side). The
"media" is synthesised, not recorded: the bytes do not matter, the byte *offsets* do — the boxes
are real MP4 box headers (``ftyp``/``moov``/``sidx``/``moof``/``mdat``) so the discovery path and
the ``sidx`` arithmetic are the production ones.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.abspath(os.path.join(HERE, "..", "..", "lib"))
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from v020 import media_proxy as M  # noqa: E402
from v020.contracts import ResolvedMedia, Track  # noqa: E402

DURATION = 600.0
MOOV = b"\x00" * 120
FTYP = b"isom\x00\x00\x02\x00isomiso2avc1mp41"


def box(kind: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + kind + payload


def sidx_box(timescale: int, earliest: int, first_offset: int,
             refs: list[tuple[int, int]]) -> bytes:
    payload = b"\x00\x00\x00\x00"                          # version 0, flags 0
    payload += (1).to_bytes(4, "big")                      # reference_ID
    payload += timescale.to_bytes(4, "big")
    payload += earliest.to_bytes(4, "big")
    payload += first_offset.to_bytes(4, "big")
    payload += b"\x00\x00" + len(refs).to_bytes(2, "big")
    for size, duration in refs:
        payload += size.to_bytes(4, "big") + duration.to_bytes(4, "big") + b"\x00" * 4
    return box(b"sidx", payload)


def sample_mp4(*, with_sidx: bool = True, fragments: int = 40, fragment: int = 4096) -> bytes:
    head = box(b"ftyp", FTYP) + box(b"moov", MOOV)
    if with_sidx:
        # 1 s timescale, 120 s segments: media second 0..480 spans the whole file.
        head += sidx_box(1, 0, 0, [(fragment + 32, 120)] * fragments)
    body = b""
    for index in range(fragments):
        payload = bytes([index % 256]) * fragment
        body += box(b"moof", b"\x00" * 24) + box(b"mdat", payload)
    return head + body


class StubSource:
    """A tiny range server standing in for googlevideo."""

    def __init__(self) -> None:
        self.bodies = {"/video.mp4": sample_mp4(), "/audio.m4a": sample_mp4(fragments=8)}
        self.expired = False
        self.drop_after: int | None = None       # truncate one body after N bytes
        self.drop_once = True                   # a dead connection is not a broken source
        self.expire_on_drop = False              # ... and stale the token at that instant
        self.requests: list[str] = []
        self.bytes_sent: dict[str, int] = {}
        self.drops = 0
        self.lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def body(self, leaf: str) -> bytes:
        return self.bodies[leaf]

    def url(self, leaf: str, token: int = 1) -> str:
        return f"http://127.0.0.1:{self.port}{leaf}?t={token}&expire=9999999999"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                leaf = self.path.split("?")[0]
                body = stub.bodies.get(leaf)
                with stub.lock:
                    stub.requests.append(self.path)
                if body is None:
                    self.send_error(404)
                    return
                if stub.expired:
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                total = len(body)
                match = re.match(r"^bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
                if match:
                    start = int(match.group(1) or 0)
                    end = int(match.group(2)) if match.group(2) else total - 1
                    end = min(end, total - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                else:
                    start, end = 0, total - 1
                    self.send_response(200)
                chunk = body[start:end + 1]
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(len(chunk)))
                self.end_headers()
                limit = stub.drop_after
                payload = chunk if limit is None else chunk[:limit]
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                    return
                with stub.lock:
                    stub.bytes_sent[leaf] = stub.bytes_sent.get(leaf, 0) + len(payload)
                if limit is not None and len(payload) < len(chunk):
                    with stub.lock:
                        stub.drops += 1
                        if stub.drop_once:
                            stub.drop_after = None
                    if stub.expire_on_drop:
                        stub.expired = True
                    self.close_connection = True
                    try:
                        self.connection.close()
                    except OSError:
                        pass

            def log_message(self, *args):
                pass

        return Handler


class Resolver:
    """``MediaProxy(resolver=…)`` — hands out a ResolvedMedia pointing at the stub."""

    def __init__(self, stub: StubSource, *, heal: bool = True) -> None:
        self.stub = stub
        self.heal = heal
        self.calls: list[dict] = []
        self.token = 1

    def __call__(self, url: str, **kwargs) -> ResolvedMedia:
        self.calls.append({"url": url, **kwargs})
        if self.heal:
            self.stub.expired = False
        self.token += 1
        now = time.time()
        return ResolvedMedia(
            video_id="ai_gI0Jbr_E", title="stub", duration_s=DURATION,
            video=Track(itag=137, codecs="avc1.640028", content_type="video/mp4",
                        url=self.stub.url("/video.mp4", self.token), width=1920, height=1080,
                        fps=25.0),
            audio=Track(itag=140, codecs="mp4a.40.2", content_type="audio/mp4",
                        url=self.stub.url("/audio.m4a", self.token)),
            fetched_at=now, expires_at=now + 3600)


class ProxyCase(unittest.TestCase):
    def setUp(self):
        self.stub = StubSource()
        self.resolver = Resolver(self.stub)
        self.proxy = M.MediaProxy("127.0.0.1", 0, log=lambda *_: None,
                                  resolver=self.resolver).start()
        self.sid = self.proxy.open_session(self.resolver("https://www.youtube.com/watch?v=x"))

    def tearDown(self):
        self.proxy.stop()
        self.stub.stop()

    # -- helpers ---------------------------------------------------------------------

    def get(self, path: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(f"http://127.0.0.1:{self.proxy.port}{path}",
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def media_path(self, sid: str | None = None, leaf: str = "0") -> str:
        return f"/media/{sid or self.sid}/{leaf}"


# --- pure functions -------------------------------------------------------------------------


class TestBoxes(unittest.TestCase):
    def test_box_walk_and_64bit_size(self):
        big = (1).to_bytes(4, "big") + b"mdat" + (48).to_bytes(8, "big") + b"\x00" * 32
        buf = box(b"ftyp", b"isom") + box(b"moov", b"\x00" * 8) + big
        boxes = M.box_list(buf)
        self.assertEqual([b[0] for b in boxes], ["ftyp", "moov", "mdat"])
        self.assertEqual(boxes[1][1], 12)
        self.assertEqual(boxes[2][2], 48)

    def test_truncated_tail_is_not_reported(self):
        buf = box(b"ftyp", b"isom") + (100).to_bytes(4, "big") + b"moov" + b"\x00" * 4
        self.assertEqual([b[0] for b in M.box_list(buf)], ["ftyp"])

    def test_segment_ranges(self):
        data = sample_mp4()
        ranges = M.segment_ranges(data)
        ftyp_size = 8 + len(FTYP)
        moov_size = 8 + len(MOOV)
        self.assertEqual(ranges["init"], (0, ftyp_size + moov_size - 1))
        self.assertIsNotNone(ranges["index"])
        boxes = {kind: (offset, size) for kind, offset, size in M.box_list(data)}
        self.assertEqual(ranges["index"], (boxes["sidx"][0], boxes["sidx"][0] +
                                           boxes["sidx"][1] - 1))

    def test_sidx_maps_seconds_to_bytes(self):
        data = sample_mp4()
        ranges = M.segment_ranges(data)
        sidx = M.parse_sidx(data, ranges["index"][0])
        self.assertEqual(sidx.timescale, 1)
        self.assertEqual(len(sidx.references), 40)
        start, end, media = sidx.range_at(0.0)
        self.assertEqual(media, 0.0)
        self.assertEqual(start, ranges["index"][1] + 1 + sidx.first_offset)
        start, end, media = sidx.range_at(250.0)          # third 120 s subsegment
        self.assertEqual(media, 240.0)
        self.assertEqual(end - start + 1, sidx.references[2][0])

    def test_parse_range(self):
        self.assertIsNone(M.parse_range(None, 1000))
        self.assertEqual(M.parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(M.parse_range("bytes=900-", 1000), (900, 999))
        self.assertEqual(M.parse_range("bytes=-100", 1000), (900, 999))
        self.assertEqual(M.parse_range("bytes=0-5000", 1000), (0, 999))
        for bad in ("bytes=1000-2000", "items=0-1", "bytes=-", "bytes=5-1"):
            with self.assertRaises(ValueError, msg=bad):
                M.parse_range(bad, 1000)


# --- manifest -------------------------------------------------------------------------------


class TestManifest(ProxyCase):
    def test_static_mpd_with_both_adaptation_sets(self):
        status, headers, body = self.get(self.media_path(leaf="manifest.mpd"))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/dash+xml")
        mpd = ET.fromstring(body)
        ns = {"d": "urn:mpeg:dash:schema:mpd:2011"}
        self.assertEqual(mpd.get("type"), "static")
        self.assertEqual(mpd.get("mediaPresentationDuration"), "PT600.000S")
        sets = mpd.findall(".//d:AdaptationSet", ns)
        self.assertEqual([s.get("contentType") for s in sets], ["video", "audio"])
        for adaptation in sets:
            representation = adaptation.find("d:Representation", ns)
            base = representation.find("d:BaseURL", ns).text
            segment = representation.find("d:SegmentBase", ns)
            self.assertTrue(base in ("0", "1"))
            self.assertRegex(segment.get("indexRange"), r"^\d+-\d+$")
            initialization = segment.find("d:Initialization", ns)
            self.assertRegex(initialization.get("range"), r"^0-\d+$")
            self.assertTrue(int(representation.get("bandwidth")) > 0)
        video = sets[0].find("d:Representation", ns)
        self.assertEqual(video.get("codecs"), "avc1.640028")
        self.assertEqual(video.get("height"), "1080")
        audio = sets[1].find("d:Representation", ns)
        self.assertEqual(audio.get("codecs"), "mp4a.40.2")

    def test_index_and_init_ranges_point_at_the_real_boxes(self):
        self.get(self.media_path(leaf="manifest.mpd"))
        state = self.proxy.sessions[self.sid].video
        source = self.stub.body("/video.mp4")
        boxes = {kind: (offset, size) for kind, offset, size in M.box_list(source)}
        self.assertEqual(state.index_range, (boxes["sidx"][0], boxes["sidx"][0] +
                                             boxes["sidx"][1] - 1))
        self.assertEqual(state.init_range, (0, boxes["moov"][0] + boxes["moov"][1] - 1))
        self.assertEqual(state.total, len(source))

    def test_no_sidx_falls_back_to_one_segment(self):
        self.stub.bodies["/video.mp4"] = sample_mp4(with_sidx=False)
        body = self.get(self.media_path(leaf="manifest.mpd"))[2]
        mpd = ET.fromstring(body)
        ns = {"d": "urn:mpeg:dash:schema:mpd:2011"}
        segment = mpd.find(".//d:AdaptationSet[@contentType='video']//d:SegmentBase", ns)
        self.assertIsNone(segment.get("indexRange"))
        self.assertIsNotNone(segment.find("d:Initialization", ns))

    def test_unknown_and_revoked_sessions(self):
        self.assertEqual(self.get("/media/nope/manifest.mpd")[0], 404)
        self.proxy.close_session(self.sid)
        self.assertEqual(self.get(self.media_path(leaf="manifest.mpd"))[0], 410)


# --- byte ranges ----------------------------------------------------------------------------


class TestRanges(ProxyCase):
    def test_range_is_byte_identical_to_the_source(self):
        source = self.stub.body("/video.mp4")
        status, headers, body = self.get(self.media_path(), {"Range": "bytes=1000-5000"})
        self.assertEqual(status, 206)
        self.assertEqual(body, source[1000:5001])
        self.assertEqual(headers["Content-Range"], f"bytes 1000-5000/{len(source)}")
        self.assertEqual(headers["Content-Length"], "4001")
        self.assertEqual(headers["Accept-Ranges"], "bytes")

    def test_open_ended_and_suffix_ranges(self):
        source = self.stub.body("/video.mp4")
        status, headers, body = self.get(self.media_path(), {"Range": "bytes=50000-"})
        self.assertEqual((status, body), (206, source[50000:]))
        self.assertEqual(headers["Content-Range"], f"bytes 50000-{len(source) - 1}/{len(source)}")
        status, _, body = self.get(self.media_path(), {"Range": "bytes=-1234"})
        self.assertEqual((status, body), (206, source[-1234:]))

    def test_whole_file_without_range(self):
        source = self.stub.body("/video.mp4")
        status, headers, body = self.get(self.media_path())
        self.assertEqual((status, body), (200, source))
        self.assertEqual(headers["Content-Length"], str(len(source)))

    def test_audio_endpoint(self):
        source = self.stub.body("/audio.m4a")
        self.assertEqual(self.proxy.audio_url_for(self.sid),
                         f"http://127.0.0.1:{self.proxy.port}/media/{self.sid}/audio.m4a")
        status, headers, body = self.get(self.media_path(leaf="audio.m4a"))
        self.assertEqual((status, body), (200, source))
        self.assertEqual(headers["Content-Type"], "audio/mp4")
        status, _, body = self.get(self.media_path(leaf="1"), {"Range": "bytes=100-199"})
        self.assertEqual((status, body), (206, source[100:200]))

    def test_416(self):
        source = self.stub.body("/video.mp4")
        status, headers, body = self.get(self.media_path(),
                                         {"Range": f"bytes={len(source) + 10}-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], f"bytes */{len(source)}")

    def test_head_is_synthesised(self):
        source = self.stub.body("/video.mp4")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy.port}{self.media_path()}",
            method="HEAD", headers={"Range": "bytes=10-19"})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Length"], "10")
            self.assertEqual(response.read(), b"")
        self.assertGreater(len(source), 10)

    def test_unknown_sid_404(self):
        self.assertEqual(self.get("/media/doesnotexist/0")[0], 404)


class FakeHandler:
    """The sender's side of the mount: ``if proxy.handle(self): return``."""

    def __init__(self, path: str, command: str = "GET", headers: dict | None = None):
        self.path, self.command = path, command
        self.headers = headers or {}
        self.status: int | None = None
        self.sent: dict[str, str] = {}
        self.body = b""

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, key: str, value: str) -> None:
        self.sent[key] = value

    def end_headers(self) -> None:
        pass

    @property
    def wfile(self) -> "FakeHandler":
        return self

    def write(self, data: bytes) -> int:
        self.body += data
        return len(data)


class TestMounting(ProxyCase):
    def test_other_paths_are_left_to_the_sender(self):
        for path in ("/api/status", "/receiver.html", "/", "/healthz", "/media"):
            self.assertFalse(self.proxy.handle(FakeHandler(path)), path)

    def test_media_is_served_into_the_senders_own_handler(self):
        source = self.stub.body("/video.mp4")
        handler = FakeHandler(self.media_path(), headers={"Range": "bytes=0-9"})
        self.assertTrue(self.proxy.handle(handler))
        self.assertEqual(handler.status, 206)
        self.assertEqual(handler.body, source[:10])
        self.assertEqual(handler.sent["Content-Range"], f"bytes 0-9/{len(source)}")

    def test_options_preflight(self):
        handler = FakeHandler(self.media_path(), command="OPTIONS")
        self.assertTrue(self.proxy.handle(handler))
        self.assertEqual(handler.status, 204)
        self.assertEqual(handler.sent["Access-Control-Allow-Headers"], "Range")


# --- revocation -----------------------------------------------------------------------------


class TestRevocation(ProxyCase):
    def test_revoked_sid_is_410_and_never_a_truncated_200(self):
        self.assertEqual(self.get(self.media_path())[0], 200)
        self.proxy.close_session(self.sid)
        status, headers, body = self.get(self.media_path())
        self.assertEqual(status, 410)
        self.assertEqual(headers["Content-Length"], str(len(body)))
        self.assertEqual(self.proxy.stats()["sessions"], 0)

    def test_inflight_reader_is_not_cut(self):
        source = self.stub.body("/video.mp4")
        collected = bytearray()
        started = threading.Event()
        done = threading.Event()

        def reader():
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.proxy.port}{self.media_path()}",
                headers={"Range": f"bytes=0-{len(source) - 1}"})
            with urllib.request.urlopen(request, timeout=20) as response:
                while True:
                    chunk = response.read(2048)
                    if not chunk:
                        break
                    collected.extend(chunk)
                    started.set()
                    time.sleep(0.001)
            done.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        self.assertTrue(started.wait(5), "the reader never started")
        time.sleep(0.02)
        self.proxy.close_session(self.sid)          # revoke while bytes are on the wire
        self.assertTrue(done.wait(20), "the reader was cut")
        thread.join(1)
        self.assertEqual(len(collected), len(source))
        self.assertEqual(hashlib.sha256(collected).hexdigest(),
                         hashlib.sha256(source).hexdigest())

    def test_stats_shape(self):
        stats = self.proxy.stats()
        self.assertEqual(set(stats), {"sessions", "rss_mb", "refreshes"})
        self.assertEqual(stats["sessions"], 1)
        self.assertLess(stats["rss_mb"], M.RSS_BUDGET_MB)


# --- expiry and refresh ---------------------------------------------------------------------


class TestRefresh(ProxyCase):
    def test_403_triggers_one_refresh_and_the_same_session_continues(self):
        source = self.stub.body("/video.mp4")
        self.assertEqual(self.get(self.media_path(), {"Range": "bytes=0-999"})[2],
                         source[:1000])
        old_url = self.proxy.sessions[self.sid].media.video.url
        self.stub.expired = True
        status, _, body = self.get(self.media_path(), {"Range": "bytes=1000-1999"})
        self.assertEqual(status, 206)
        self.assertEqual(body, source[1000:2000])
        self.assertEqual(self.proxy.stats()["refreshes"], 1)
        self.assertNotEqual(self.proxy.sessions[self.sid].media.video.url, old_url)
        self.assertEqual(self.resolver.calls[-1]["max_height"], 1080)
        self.assertIsNotNone(self.proxy.sessions[self.sid].media.expires_at)

    def test_midbody_drop_with_a_stale_token_is_recovered_transparently(self):
        source = self.stub.body("/video.mp4")
        self.proxy.mpd_for(self.sid)               # discovery first, on a healthy source
        old_url = self.proxy.sessions[self.sid].media.video.url
        self.stub.drop_after = 20000               # the source dies 20 kB into the body
        self.stub.expire_on_drop = True            # and its token is stale at that instant
        status, headers, body = self.get(self.media_path(),
                                         {"Range": f"bytes=0-{len(source) - 1}"})
        self.assertEqual(status, 206)
        self.assertEqual(len(body), len(source), "a truncated body would be shorter")
        self.assertEqual(hashlib.sha256(body).hexdigest(), hashlib.sha256(source).hexdigest())
        self.assertEqual(headers["Content-Length"], str(len(source)))
        self.assertGreaterEqual(self.proxy.stats()["refreshes"], 1)
        self.assertNotEqual(self.proxy.sessions[self.sid].media.video.url, old_url)
        self.assertGreaterEqual(self.stub.drops, 1)

    def test_refresh_of_a_revoked_session_is_refused(self):
        self.proxy.close_session(self.sid)
        with self.assertRaises(KeyError):
            self.proxy.refresh(self.sid)


# --- memory ---------------------------------------------------------------------------------


class TestMemory(ProxyCase):
    def test_two_concurrent_readers_stay_under_the_budget(self):
        self.stub.bodies["/video.mp4"] = sample_mp4(fragments=1000, fragment=4096)  # ~4 MB
        source = self.stub.body("/video.mp4")
        results = []
        lock = threading.Lock()

        def reader(offset: int):
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.proxy.port}{self.media_path()}",
                headers={"Range": f"bytes={offset}-"})
            with urllib.request.urlopen(request, timeout=20) as response:
                digest = hashlib.sha256()
                size = 0
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            with lock:
                results.append((size, digest.hexdigest()))

        threads = [threading.Thread(target=reader, args=(offset,))
                   for offset in (0, 1024 * 1024)]
        for thread in threads:
            thread.start()
        peak = 0.0
        for _ in range(40):
            peak = max(peak, self.proxy.stats()["rss_mb"])
            if not any(t.is_alive() for t in threads):
                break
            time.sleep(0.05)
        for thread in threads:
            thread.join(20)
        expected_zero = hashlib.sha256(source).hexdigest()
        expected_one = hashlib.sha256(source[1024 * 1024:]).hexdigest()
        self.assertIn((len(source), expected_zero), results)
        self.assertIn((len(source) - 1024 * 1024, expected_one), results)
        self.assertLess(peak, M.RSS_BUDGET_MB, f"rss peaked at {peak} MB")

    def test_client_hangup_stops_pulling_from_the_source(self):
        self.stub.bodies["/video.mp4"] = sample_mp4(fragments=2000, fragment=4096)  # ~8 MB
        source = self.stub.body("/video.mp4")
        sock = socket.create_connection(("127.0.0.1", self.proxy.port), timeout=10)
        sock.sendall((f"GET {self.media_path()} HTTP/1.1\r\nHost: x\r\n"
                      f"Range: bytes=0-{len(source) - 1}\r\n\r\n").encode())
        sock.recv(4096)
        time.sleep(0.05)
        sock.close()
        time.sleep(0.4)
        sent = self.stub.bytes_sent.get("/video.mp4", 0)
        self.assertLess(sent, len(source) // 2,
                        f"the proxy kept pulling {sent} B after the client left")


if __name__ == "__main__":
    unittest.main()
