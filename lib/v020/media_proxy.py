"""Stream the resolved media to the receiver and to the group, without keeping it (contracts §4).

Why a stdlib handler and not WSGI
--------------------------------
CT 121 runs one HTTP server on ``192.168.2.21:7640`` for ``/receiver.html``, ``/api/*`` and
``/media/*`` (contracts §5). The sender owns the socket; this module therefore exposes both
shapes over one code path:

- :meth:`MediaProxy.handle` — serve a request from a ``BaseHTTPRequestHandler`` the sender already
  owns (``if proxy.handle(self): return``). **This is the shape the sender uses**: one port, one
  server, no extra process and no message passing. WSGI was rejected because a byte-range body has
  to be written and flushed incrementally while the source keeps being read; a WSGI ``iterable``
  gives no way to say "this reader was revoked, answer 410 instead of a truncated 200", and the
  read loop's refresh/resume state would have to live in the application object anyway.
- :meth:`MediaProxy.start` — bind our own ``ThreadingHTTPServer`` (the probe servo on ``:7641``,
  and the fallback if the sender prefers a separate server).

Streaming, not storing
----------------------
A session keeps tokens and byte offsets, never media. Every response is a byte range forwarded
from the source; ``init_range``/``index_range`` are read from the file's own ``ftyp/moov/sidx``
boxes when the extractor does not supply them, so the synthesised MPD can point at real byte
offsets. Memory is one 64 KiB chunk per reader plus one 256 KiB head probe per track.

Revocation
----------
``close_session`` removes the sid from the directory, so any **new** request gets ``410`` with no
body; a reader that already started keeps its bytes and is never cut mid-body (its session is
only dropped when its last reader leaves). A sid that was never opened gets ``404``.
"""

from __future__ import annotations

import http.client
import os
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .contracts import PATH_MEDIA_PREFIX, ResolvedMedia, Track

CHUNK = 64 * 1024                  # bytes per write; one buffer per reader
HEAD_BYTES = 256 * 1024            # first probe: ftyp+moov+sidx of a normal YouTube file
HEAD_MAX = 4 * 1024 * 1024         # growth ceiling when moov/sidx is bigger
UPSTREAM_TIMEOUT = 20.0
REFRESH_MIN_INTERVAL = 1.0         # two readers may hit 403 together; resolve once
MAX_REFRESHES = 8
MAX_SESSIONS = 8
RSS_BUDGET_MB = 150.0

_UA = "nestaudiocast-v020/0.2 (+range proxy)"
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$")


class Revoked(KeyError):
    """The sid exists but was closed: answer 410, never a truncated body."""


class UpstreamExpired(RuntimeError):
    """The source answered 403/410: the token is stale, a refresh is due."""


class UpstreamError(RuntimeError):
    """The source answered something we cannot use."""


# --- MP4 boxes ------------------------------------------------------------------------------


def box_list(buf: bytes, base: int = 0) -> list[tuple[str, int, int]]:
    """Walk the top-level boxes of ``buf``: ``(type, absolute offset, size)``.

    ISO/IEC 14496-12: 32-bit size, ``1`` means a 64-bit largesize follows, ``0`` means "to the end
    of the file". A truncated tail is simply not reported (the caller grows its probe).
    """
    boxes: list[tuple[str, int, int]] = []
    pos = 0
    while pos + 8 <= len(buf):
        size = int.from_bytes(buf[pos:pos + 4], "big")
        kind = buf[pos + 4:pos + 8].decode("latin-1")
        header = 8
        if size == 1:
            if pos + 16 > len(buf):
                break
            size = int.from_bytes(buf[pos + 8:pos + 16], "big")
            header = 16
        elif size == 0:
            size = len(buf) - pos
        if size < header or pos + size > len(buf):
            # Truncated by the probe (a 2.5 GB mdat will never fit): not a usable box.
            break
        boxes.append((kind, base + pos, size))
        pos += size
    return boxes


@dataclass(frozen=True)
class Sidx:
    """A parsed ``sidx``: enough to turn media seconds into an absolute byte range."""

    offset: int
    size: int
    timescale: int
    earliest: float
    first_offset: int
    references: tuple[tuple[int, int], ...]  # (referenced_size, subsegment_duration)

    def range_at(self, seconds: float) -> tuple[int, int, float]:
        """``(start_byte, end_byte, start_seconds)`` of the subsegment holding ``seconds``."""
        if not self.references or self.timescale <= 0:
            raise ValueError("sidx has no reference")
        pos = self.offset + self.size + self.first_offset
        clock = self.earliest
        last = None
        for size, duration in self.references:
            if size <= 0:
                continue
            last = (pos, pos + size - 1, clock)
            if clock + duration / self.timescale > seconds:
                return last
            pos += size
            clock += duration / self.timescale
        return last  # type: ignore[return-value]


def parse_sidx(buf: bytes, offset: int, limit: int = 0) -> Sidx:
    """Parse the ``sidx`` box starting at ``offset`` inside ``buf`` (version 0 or 1)."""
    if offset + 12 > len(buf) or buf[offset + 4:offset + 8] != b"sidx":
        raise ValueError("no sidx at that offset")
    size = int.from_bytes(buf[offset:offset + 4], "big")
    if size == 1:
        size = int.from_bytes(buf[offset + 8:offset + 16], "big")
    if limit and size > limit:
        raise ValueError(f"sidx box ({size} B) is larger than the probe ({limit} B)")
    if offset + size > len(buf):
        raise ValueError(f"sidx box ({size} B) is truncated at {len(buf)} B")
    version = buf[offset + 8]
    pos = offset + 12
    pos += 4                                    # reference_ID
    timescale = int.from_bytes(buf[pos:pos + 4], "big")
    pos += 4
    if version == 0:
        earliest = float(int.from_bytes(buf[pos:pos + 4], "big"))
        first_offset = int.from_bytes(buf[pos + 4:pos + 8], "big")
        pos += 8
    else:
        earliest = float(int.from_bytes(buf[pos:pos + 8], "big"))
        first_offset = int.from_bytes(buf[pos + 8:pos + 16], "big")
        pos += 16
    pos += 2                                    # reserved
    count = int.from_bytes(buf[pos:pos + 2], "big")
    pos += 2
    references = []
    for _ in range(count):
        if pos + 12 > offset + size:
            break
        word = int.from_bytes(buf[pos:pos + 4], "big")
        duration = int.from_bytes(buf[pos + 4:pos + 8], "big")
        references.append((word & 0x7FFFFFFF, duration))
        pos += 12
        # reference_type (bit 31) is 0 = media, 1 = a nested sidx: YouTube never nests one here.
    return Sidx(offset=offset, size=size, timescale=timescale, earliest=earliest,
                first_offset=first_offset, references=tuple(references))


def segment_ranges(buf: bytes) -> dict[str, tuple[int, int] | None]:
    """``{"init": …, "index": …}`` from the head of a fragmented MP4.

    ``init`` covers ``ftyp`` through the end of ``moov`` (DASH ``Initialization@range``);
    ``index`` covers the ``sidx`` box (DASH ``SegmentBase@indexRange``). A progressive MP4 has no
    ``sidx``: ``index`` is then ``None`` and the whole file is one segment.
    """
    boxes = box_list(buf)
    init: tuple[int, int] | None = None
    index: tuple[int, int] | None = None
    moov_end = None
    for kind, offset, size in boxes:
        if kind == "moov":
            moov_end = offset + size
        elif kind in ("sidx", "ssix") and index is None:
            index = (offset, offset + size - 1)
    if moov_end is not None and boxes and boxes[0][0] in ("ftyp", "styp", "moov"):
        init = (0, moov_end - 1)
    return {"init": init, "index": index}


def parse_range(header: str | None, total: int) -> tuple[int, int] | None:
    """HTTP ``Range`` -> inclusive ``(start, end)``; ``None`` means "the whole thing".

    Raises ``ValueError`` when the header is present but unsatisfiable (answer 416).
    """
    if not header:
        return None
    match = _RANGE_RE.match(header.strip())
    if not match:
        raise ValueError(f"unsupported Range: {header!r}")
    first, last = match.group(1), match.group(2)
    if not first and not last:
        raise ValueError("empty Range")
    if not first:                                  # bytes=-N : the last N bytes
        length = int(last)
        if length <= 0:
            raise ValueError("zero-length suffix range")
        return max(0, total - length), total - 1
    start = int(first)
    end = min(int(last), total - 1) if last else total - 1
    if start >= total or end < start:
        raise ValueError(f"range {start}-{end} outside 0-{total - 1}")
    return start, end


def trim_memory() -> float:
    """Return freed arenas to the OS after a re-resolution (glibc only, best effort).

    yt-dlp leaves a few tens of MB in the allocator's free lists after a parse; on an always-on
    sender that never shows up as a leak but it does lower the headroom under the 150 MB bound.
    Returns the RSS in MiB after the call, or the current RSS when there is nothing to trim.
    """
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (ImportError, OSError, AttributeError):
        pass
    return rss_mb()


def rss_mb() -> float:
    """This process's resident set, in MiB (``/proc`` first, ``getrusage`` as the fallback)."""
    try:
        with open("/proc/self/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except OSError:
        pass
    try:
        import resource

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except (ImportError, OSError):
        return 0.0


# --- sessions -------------------------------------------------------------------------------


@dataclass
class _TrackState:
    """What we learned about one track's byte layout; discovered once, then cached."""

    total: int | None = None
    init_range: tuple[int, int] | None = None
    index_range: tuple[int, int] | None = None
    discovered: bool = False
    head_bytes: int = 0
    sidx: Sidx | None = None


@dataclass
class _Session:
    sid: str
    media: ResolvedMedia
    source_url: str
    created: float = field(default_factory=time.time)
    video: _TrackState = field(default_factory=_TrackState)
    audio: _TrackState = field(default_factory=_TrackState)
    lock: threading.RLock = field(default_factory=threading.RLock)
    refreshes: int = 0
    refreshed_at: float = 0.0
    revoked: bool = False
    readers: int = 0
    last_reason: str = ""

    def track(self, which: str) -> Track:
        return self.media.video if which == "video" else self.media.audio

    def state(self, which: str) -> _TrackState:
        return self.video if which == "video" else self.audio


class MediaProxy:
    """Owns the ``/media/<sid>`` namespace and the byte-range plumbing behind it."""

    def __init__(self, host: str, port: int, log=print, *, resolver=None,
                 max_height: int = 1080, timeout: float = 30.0) -> None:
        self.host, self.port, self.log = host, port, log
        self.sessions: dict[str, _Session] = {}
        self.faults: dict = {}          # test hook: {"drop_upstream_after": bytes}
        self._revoked: dict[str, tuple[float, str]] = {}
        self._lock = threading.RLock()
        self._refreshes = 0
        self._max_height = int(max_height)
        self._timeout = float(timeout)
        self._resolver = resolver or self._default_resolver
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- session directory ---------------------------------------------------------------

    def _default_resolver(self, url: str, **kwargs) -> ResolvedMedia:
        from .resolve import resolve  # local import: no yt-dlp at import time

        return resolve(url, **kwargs)

    def open_session(self, resolved: ResolvedMedia, source_url: str | None = None) -> str:
        """Register a resolved media and return its opaque ``sid``."""
        if not resolved.video.url or not resolved.audio.url:
            raise ValueError("resolved media has an empty track url")
        sid = secrets.token_urlsafe(9)
        with self._lock:
            live = [s for s in self.sessions.values() if not s.revoked]
            if len(live) >= MAX_SESSIONS:
                oldest = min(live, key=lambda s: s.created)
                self.log(f"media: too many sessions, closing {oldest.sid}")
                self.close_session(oldest.sid)
            self.sessions[sid] = _Session(
                sid=sid, media=resolved,
                source_url=source_url or f"https://www.youtube.com/watch?v={resolved.video_id}")
        self.log(f"media: open {sid} video_id={resolved.video_id} "
                 f"dur={resolved.duration_s:.0f}s itags={resolved.video.itag}/{resolved.audio.itag} "
                 f"expires_in={resolved.expires_at - time.time():.0f}s")
        return sid

    def close_session(self, sid: str) -> None:
        """Revoke a sid; readers in flight get a clean 410, never a truncated body."""
        with self._lock:
            session = self.sessions.get(sid)
            if session is None:
                self._revoked.setdefault(sid, (time.time(), "already gone"))
                return
            session.revoked = True
            self._revoked[sid] = (time.time(), "closed")
            while len(self._revoked) > 128:
                self._revoked.pop(next(iter(self._revoked)))
            draining = session.readers
        self.log(f"media: close {sid} refreshes={session.refreshes} draining_readers={draining}")
        self._gc()

    def _gc(self) -> None:
        with self._lock:
            for sid, session in list(self.sessions.items()):
                if session.revoked and session.readers == 0:
                    del self.sessions[sid]

    def _session(self, sid: str) -> _Session:
        with self._lock:
            session = self.sessions.get(sid)
            if session is None:
                if sid in self._revoked:
                    raise Revoked(sid)
                raise KeyError(sid)
            if session.revoked:
                raise Revoked(sid)
            return session

    def _enter(self, sid: str) -> _Session:
        """Take a reader slot (revocation is only checked here, before any byte is sent)."""
        session = self._session(sid)
        with self._lock:
            session.readers += 1
        return session

    def _leave(self, session: _Session) -> None:
        with self._lock:
            session.readers = max(0, session.readers - 1)
        self._gc()

    # -- manifest ------------------------------------------------------------------------

    def mpd_for(self, sid: str) -> str:
        """DASH manifest (§4): ``static``, known duration, SegmentBase + indexRange, both tracks."""
        session = self._session(sid)
        video = self._ensure(session, "video")
        audio = self._ensure(session, "audio")
        duration = max(0.001, float(session.media.duration_s))
        lines = [
            '<?xml version="1.0" encoding="utf-8"?>',
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"'
            ' profiles="urn:mpeg:dash:profile:isoff-on-demand:2011"'
            f' type="static" mediaPresentationDuration="PT{duration:.3f}S"'
            f' minBufferTime="PT2.000S">',
            ' <Period id="p0" start="PT0.000S">',
        ]
        lines += self._adaptation_set(session, "video", 1, video)
        lines += self._adaptation_set(session, "audio", 2, audio)
        lines += [' </Period>', '</MPD>', '']
        return "\n".join(lines)

    def _adaptation_set(self, session: _Session, which: str, index: int,
                        state: _TrackState) -> list[str]:
        track = session.track(which)
        head = (f'  <AdaptationSet id="{index}" contentType="{which}"'
                f' mimeType="{track.content_type}" segmentAlignment="true"'
                ' startWithSAP="1" subsegmentAlignment="true" subsegmentStartsWithSAP="1"')
        if which == "video":
            if track.width:
                head += f' maxWidth="{track.width}"'
            if track.height:
                head += f' maxHeight="{track.height}"'
            if track.fps:
                head += f' frameRate="{_num(track.fps)}"'
        rep = f'   <Representation id="{which[0]}" codecs="{track.codecs}"'
        if which == "video":
            if track.width:
                rep += f' width="{track.width}"'
            if track.height:
                rep += f' height="{track.height}"'
            if track.fps:
                rep += f' frameRate="{_num(track.fps)}"'
        bandwidth = _bandwidth(track, state, session.media.duration_s)
        if bandwidth:
            rep += f' bandwidth="{bandwidth}"'
        body = [f'   <BaseURL>{"0" if which == "video" else "1"}</BaseURL>']
        if state.index_range:
            segment = [f'   <SegmentBase indexRange="{state.index_range[0]}-'
                       f'{state.index_range[1]}">']
        else:
            # No sidx: the whole file is one segment; seeking falls back to HTTP ranges.
            segment = ['   <SegmentBase>']
        if state.init_range:
            segment.append(f'    <Initialization range="{state.init_range[0]}-'
                           f'{state.init_range[1]}"/>')
        segment.append('   </SegmentBase>')
        return [head + ">", rep + ">"] + body + segment + \
               ['  </Representation>', ' </AdaptationSet>']

    def audio_url_for(self, sid: str) -> str:
        """``http://<host>:<port>/media/<sid>/audio.m4a`` for the group's DMR."""
        return f"http://{self.host}:{self.port}{PATH_MEDIA_PREFIX}{sid}/audio.m4a"

    def manifest_url_for(self, sid: str) -> str:
        """Convenience for the sender and the probe (not part of the frozen surface)."""
        return f"http://{self.host}:{self.port}{PATH_MEDIA_PREFIX}{sid}/manifest.mpd"

    def video_url_for(self, sid: str) -> str:
        """The raw progressive video, for a receiver that will not do DASH."""
        return f"http://{self.host}:{self.port}{PATH_MEDIA_PREFIX}{sid}/0"

    # -- refresh -------------------------------------------------------------------------

    def refresh(self, sid: str, reason: str = "") -> None:
        """Re-resolve on 403/expiry and keep the reader alive; log the redacted cause."""
        with self._lock:
            session = self.sessions.get(sid)
        if session is None:
            raise Revoked(sid)
        with session.lock:
            now = time.monotonic()
            if session.refreshed_at and now - session.refreshed_at < REFRESH_MIN_INTERVAL:
                return                       # another reader just did it
            if session.refreshes >= MAX_REFRESHES:
                raise UpstreamError(f"{sid}: {session.refreshes} refreshes, giving up")
            old_itags = (session.media.video.itag, session.media.audio.itag)
            started = time.monotonic()
            fresh = self._resolver(session.source_url, max_height=self._max_height,
                                   timeout=self._timeout)
            session.media = fresh
            session.refreshes += 1
            session.refreshed_at = now
            session.last_reason = reason
            new_itags = (fresh.video.itag, fresh.audio.itag)
            if new_itags != old_itags:
                # A different representation has different byte offsets: rediscover.
                session.video = _TrackState()
                session.audio = _TrackState()
            self._refreshes += 1
            took = (time.monotonic() - started) * 1000.0
        trim_memory()
        self.log(f"media: refresh {sid} reason={reason or '-'} itags={new_itags[0]}/"
                 f"{new_itags[1]} took={took:.0f}ms expires_in="
                 f"{fresh.expires_at - time.time():.0f}s")

    def stats(self) -> dict:
        """``{"sessions": int, "rss_mb": float, "refreshes": int}`` — the memory bound is 150 MB."""
        with self._lock:
            live = sum(1 for s in self.sessions.values() if not s.revoked)
        return {"sessions": live, "rss_mb": rss_mb(), "refreshes": self._refreshes}

    # -- byte layout ---------------------------------------------------------------------

    def _ensure(self, session: _Session, which: str) -> _TrackState:
        """Discover ``init_range``/``index_range``/size for one track, once."""
        state = session.state(which)
        if state.discovered:
            return state
        with session.lock:
            if state.discovered:
                return state
            track = session.track(which)
            if track.init_range and track.index_range:
                state.init_range = track.init_range
                state.index_range = track.index_range
                state.discovered = True
                self.log(f"media: {session.sid} {which} ranges from the extractor "
                         f"init={state.init_range} index={state.index_range}")
                self._learn_total(session, which)
                return state
            wanted = HEAD_BYTES
            last_error = None
            refreshes = 0
            while wanted <= HEAD_MAX:
                try:
                    head, total = self._read_head(session, which, wanted)
                except UpstreamExpired:
                    refreshes += 1
                    if refreshes > 2:
                        raise
                    self.refresh(session.sid, reason="403 during discovery")
                    continue
                state.total = total
                state.head_bytes = len(head)
                ranges = segment_ranges(head)
                state.init_range = ranges["init"]
                state.index_range = ranges["index"]
                if ranges["index"]:
                    try:
                        state.sidx = parse_sidx(head, ranges["index"][0], limit=len(head))
                    except ValueError as exc:
                        last_error = str(exc)
                        state.sidx = None
                if ranges["init"] and (ranges["index"] is None or state.sidx is not None):
                    state.discovered = True
                    self.log(f"media: {session.sid} {which} discovered total={total} "
                             f"init={state.init_range} index={state.index_range} "
                             f"head={len(head)}B")
                    return state
                # moov or sidx ran past the probe: grow it.
                last_error = last_error or "boxes truncated"
                wanted *= 2
            state.discovered = True
            self.log(f"media: {session.sid} {which} incomplete discovery ({last_error}); "
                     f"serving as a single segment")
            return state

    def _learn_total(self, session: _Session, which: str) -> None:
        state = session.state(which)
        if state.total is not None:
            return
        try:
            _head, total = self._read_head(session, which, 1)
        except (UpstreamExpired, UpstreamError):
            return
        state.total = total

    def _read_head(self, session: _Session, which: str, size: int) -> tuple[bytes, int]:
        """One range probe at offset 0 -> ``(bytes, total size)``."""
        track = session.track(which)
        response = self._open_upstream(track.url, 0, size - 1)
        try:
            body = response.read(size)
            total = _total_of(response, body)
        except http.client.HTTPException as exc:
            raise UpstreamError(f"source broke the response: {exc}") from None
        finally:
            response.close()
        if total is None:
            raise UpstreamError("source did not report a size")
        return body, total

    def _total(self, session: _Session, which: str) -> int:
        state = self._ensure(session, which)
        if state.total is None:
            self._learn_total(session, which)
        if state.total is None:
            raise UpstreamError("unknown media size")
        return state.total

    def _open_upstream(self, url: str, start: int, end: int | None):
        headers = {"Range": f"bytes={start}-{'' if end is None else end}",
                   "User-Agent": _UA, "Accept-Encoding": "identity"}
        request = urllib.request.Request(url, headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 410):
                raise UpstreamExpired(f"source answered {exc.code}") from None
            raise UpstreamError(f"source answered {exc.code}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise UpstreamError(f"source unreachable: {exc}") from None

    # -- HTTP ----------------------------------------------------------------------------

    def handle(self, handler: BaseHTTPRequestHandler) -> bool:
        """Serve one request if it belongs to ``/media/``; return True when it was ours.

        The sender mounts the proxy with ``if proxy.handle(self): return``.
        """
        path = urllib.parse.unquote(urllib.parse.urlsplit(handler.path).path)
        if not path.startswith(PATH_MEDIA_PREFIX):
            return False
        if handler.command == "OPTIONS":
            handler.send_response(204)
            self._send_common(handler)
            handler.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            handler.send_header("Access-Control-Allow-Headers", "Range")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        parts = [p for p in path[len(PATH_MEDIA_PREFIX):].split("/") if p]
        if len(parts) != 2:
            self._error(handler, 404, "expected /media/<sid>/<track>")
            return True
        sid, leaf = parts
        try:
            if leaf == "manifest.mpd":
                body = self.mpd_for(sid).encode("utf-8")
                handler.send_response(200)
                self._send_common(handler)
                handler.send_header("Content-Type", "application/dash+xml")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                _mark_sent(handler)
                if handler.command != "HEAD":
                    self._write(handler, body)
                return True
            which = {"audio.m4a": "audio", "1": "audio", "audio": "audio",
                     "0": "video", "video": "video", "video.mp4": "video"}.get(leaf)
            if which is None:
                self._error(handler, 404, f"unknown media leaf {leaf!r}")
                return True
            self._serve_track(handler, sid, which)
            return True
        except Revoked:
            self._error(handler, 410, "session revoked")
        except KeyError:
            self._error(handler, 404, "unknown session")
        except UpstreamExpired:
            self._error(handler, 403, "source expired and refresh did not recover")
        except UpstreamError as exc:
            self.log(f"media: {sid} upstream error: {exc}")
            self._error(handler, 502, "source error")
        except ValueError as exc:
            self.log(f"media: {sid} bad request: {exc}")
            self._error(handler, 400, "bad range")
        except (BrokenPipeError, ConnectionResetError):
            return True
        return True

    def _serve_track(self, handler: BaseHTTPRequestHandler, sid: str, which: str) -> None:
        session = self._enter(sid)
        try:
            total = self._total(session, which)
            try:
                wanted = parse_range(handler.headers.get("Range"), total)
            except ValueError as exc:
                self.log(f"media: {sid} {which} 416: {exc}")
                self._error(handler, 416, "range not satisfiable",
                            extra={"Content-Range": f"bytes */{total}"})
                return
            start, end = wanted if wanted else (0, total - 1)
            length = end - start + 1
            partial = wanted is not None or length != total
            handler.send_response(206 if partial else 200)
            self._send_common(handler)
            handler.send_header("Content-Type", session.track(which).content_type)
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Content-Length", str(length))
            if partial:
                handler.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            handler.end_headers()
            _mark_sent(handler)
            if handler.command == "HEAD":
                return
            self._relay(handler, session, which, start, end)
        finally:
            self._leave(session)

    def _relay(self, handler: BaseHTTPRequestHandler, session: _Session, which: str,
               start: int, end: int) -> None:
        """Forward ``start..end`` and keep the client's response alive across source failures."""
        remaining = end - start + 1
        offset = start
        attempts = 0
        while remaining > 0:
            try:
                response = self._open_upstream(session.track(which).url, offset, end)
            except UpstreamExpired:
                attempts += 1
                if attempts > 3:
                    raise
                self.refresh(session.sid, reason=f"403 at byte {offset}")
                continue
            dropped = 0
            limit = self.faults.get("drop_upstream_after")
            try:
                if response.status != 206 and offset > 0:
                    # The source ignored Range: discard up to the offset (bounded, chunked).
                    self.log(f"media: {session.sid} {which} source ignored Range at {offset}")
                    skip = offset
                    while skip > 0:
                        junk = response.read(min(CHUNK, skip))
                        if not junk:
                            break
                        skip -= len(junk)
                    if skip > 0:
                        attempts += 1
                        if attempts > 3:
                            raise UpstreamError("source cannot resume")
                        continue
                while remaining > 0:
                    chunk = response.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    if not self._write(handler, chunk):
                        return
                    dropped += len(chunk)
                    offset += len(chunk)
                    remaining -= len(chunk)
                    if limit and dropped >= limit:
                        # Fault injection (tests and the expiry probe): the source connection
                        # dies mid-body, exactly what an expired token looks like to a reader.
                        self.log(f"media: {session.sid} {which} upstream dropped at {offset}")
                        break
            except (socket.timeout, TimeoutError, ConnectionError,
                    http.client.HTTPException):
                self.log(f"media: {session.sid} {which} source read failed at {offset}")
            finally:
                response.close()
            if remaining > 0:
                attempts += 1
                if attempts > 4:
                    self.log(f"media: {session.sid} {which} TRUNCATED at {offset} "
                             f"({remaining} B missing)")
                    raise UpstreamError("source keeps failing")

    def _write(self, handler: BaseHTTPRequestHandler, chunk: bytes) -> bool:
        try:
            handler.wfile.write(chunk)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _send_common(self, handler: BaseHTTPRequestHandler) -> None:
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Expose-Headers",
                            "Content-Length, Content-Range, Accept-Ranges")
        handler.send_header("Cache-Control", "no-store")

    def _error(self, handler: BaseHTTPRequestHandler, code: int, message: str,
               extra: dict | None = None) -> None:
        if _sent(handler):
            # Headers and some bytes are already on the wire: a second response would be worse
            # than the truncation the client can see for itself.
            self.log(f"media: body already started, cannot answer {code} ({message})")
            return
        body = (message + "\n").encode("utf-8")
        try:
            handler.send_response(code)
            self._send_common(handler)
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                if value:
                    handler.send_header(key, value)
            handler.end_headers()
            if handler.command != "HEAD":
                handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- standalone server (probe / fallback) --------------------------------------------

    def start(self) -> "MediaProxy":
        proxy = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "nestaudiocast-media/0.2"

            def do_GET(self):  # noqa: N802
                if not proxy.handle(self):
                    self.send_error(404)

            def do_HEAD(self):  # noqa: N802
                if not proxy.handle(self):
                    self.send_error(404)

            def do_OPTIONS(self):  # noqa: N802
                if not proxy.handle(self):
                    self.send_error(404)

            def log_message(self, fmt, *args):  # noqa: A003
                proxy.log("media: http " + (fmt % args))

        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]      # honours port 0 (tests, probes)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="media-http", daemon=True)
        self._thread.start()
        self.log(f"media: listening on http://{self.host}:{self.port}")
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def _mark_sent(handler: BaseHTTPRequestHandler) -> None:
    try:
        handler._nac_sent = True   # noqa: SLF001 - per-request marker, never read across threads
    except AttributeError:
        pass


def _sent(handler: BaseHTTPRequestHandler) -> bool:
    return bool(getattr(handler, "_nac_sent", False))


def _total_of(response, body: bytes) -> int | None:
    header = response.headers.get("Content-Range")
    if header:
        match = _CONTENT_RANGE_RE.match(header.strip())
        if match:
            return int(match.group(3))
    length = response.headers.get("Content-Length")
    if length and length.isdigit():
        if getattr(response, "status", 200) == 200:
            return int(length)
    return None


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _bandwidth(track: Track, state: _TrackState, duration: float) -> int | None:
    """Bits per second from the measured file size — not a guess about the codec."""
    if not state.total or duration <= 0:
        return None
    return int(round(state.total * 8.0 / duration))
