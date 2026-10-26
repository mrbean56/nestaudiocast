#!/usr/bin/env python3
"""The LAN measurement of task B (range proxy + synth MPD), archived in ``evidence/40-*``.

Runs on CT 121 with ``/opt/castrelay/.venv/bin/python`` and serves on **192.168.2.21:7641**
(7640 belongs to the receiver: never bind it).

    ./v020_media_probe.py all            # the whole done-when list, writes the evidence files
    ./v020_media_probe.py resolve        # 3 real videos, per-video resolve seconds
    ./v020_media_probe.py mpd            # ffprobe/ffmpeg read the served manifest
    ./v020_media_probe.py seek           # byte identity at 7200 s against the source
    ./v020_media_probe.py rss            # two concurrent readers, ps + stats()
    ./v020_media_probe.py expiry         # forced token expiry in the middle of a body
    ./v020_media_probe.py scan           # no source token in any archived file

Nothing audible is touched: no speaker, no group, no Chromecast. ffprobe reading an HTTP
manifest is silent. Every URL that reaches a log, an argv or a file goes through ``redact()``;
the only URLs ever printed are our own ``http://192.168.2.21:7641/media/<sid>/…``.

The one synthetic element is named where it is used: ``MediaProxy.faults['drop_upstream_after']``
closes the *source* connection at a byte offset, because a stale googlevideo token cannot be made
to kill an already-authorised download. The 403 that follows it is real, and so is the
re-resolution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace as dataclass_replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from v020 import media_proxy as MP  # noqa: E402
from v020.contracts import ResolvedMedia, Track  # noqa: E402
from v020.media_proxy import MediaProxy, parse_sidx  # noqa: E402
from v020.resolve import redact, resolve  # noqa: E402

DEFAULT_HOST = "192.168.2.21"
DEFAULT_PORT = 7641
DEFAULT_OUT = os.path.join(ROOT, "evidence")

#: the three videos of the done-when list, and what each one is for
VIDEOS = {
    "short": ("jNQXAC9IVRw", 1080),
    "long": ("ai_gI0Jbr_E", 1080),  # 8630 s, Pink Floyd *Pulse*
    "capped": ("dQw4w9WgXcQ", 720),  # 1080p exists, MAX_HEIGHT=720 must pick 720p
}
LONG_ID, LONG_HEIGHT = VIDEOS["long"]
SEEK_AT_S = 7200.0

TOKEN_PATTERNS = ("sig=", "lsig=", "&ei=", "sparams=", "expire=", "ip=", "id=o-", "clen=")

#: ``ffprobe`` as found on CT 121, or pointed at explicitly (the authoring host has none).
FFPROBE = os.environ.get("NAC_FFPROBE", "ffprobe")


def ffmpeg_bin() -> str:
    """``ffmpeg`` next to the ``ffprobe`` we were pointed at, else from PATH."""
    explicit = os.environ.get("NAC_FFMPEG")
    if explicit:
        return explicit
    folder = os.path.dirname(FFPROBE)
    if folder:
        candidate = os.path.join(folder, "ffmpeg")
        if os.path.exists(candidate):
            return candidate
    return "ffmpeg"


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def env_facts() -> dict:
    try:
        import yt_dlp

        ytdlp = yt_dlp.version.__version__
    except ImportError:
        ytdlp = "absent"
    return {
        "when": stamp(),
        "host": socket.gethostname(),
        "where": socket.gethostbyname(socket.gethostname()) if socket.gethostname() else "?",
        "python": sys.version.split()[0],
        "yt_dlp": ytdlp,
        "ffprobe": shutil.which(FFPROBE) or "absent",
        "ffmpeg": shutil.which("ffmpeg") or "absent",
    }


def write_json(out_dir: str, name: str, payload: dict) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return path


def annotate(media: ResolvedMedia, seconds: float) -> dict:
    """What we may archive about a resolved video: no URL, ever."""
    return {
        "video_id": media.video_id,
        "title": media.title[:80],
        "duration_s": round(media.duration_s, 3),
        "resolve_s": round(seconds, 3),
        "video": {
            "itag": media.video.itag,
            "codecs": media.video.codecs,
            "height": media.video.height,
            "width": media.video.width,
            "fps": media.video.fps,
            "host": redact(media.video.url).split("/")[2],
        },
        "audio": {
            "itag": media.audio.itag,
            "codecs": media.audio.codecs,
            "host": redact(media.audio.url).split("/")[2],
        },
        "expires_in_s": round(media.expires_at - time.time(), 1),
        "init_range": None if media.video.init_range is None else list(media.video.init_range),
        "index_range": None if media.video.index_range is None else list(media.video.index_range),
    }


def timed_resolve(video_id: str, max_height: int) -> tuple[ResolvedMedia, float]:
    started = time.monotonic()
    media = resolve(f"https://www.youtube.com/watch?v={video_id}", max_height=max_height, timeout=30.0)
    return media, time.monotonic() - started


def http_get(url: str, headers: dict | None = None, timeout: float = 60.0):
    request = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_range(url: str, start: int, end: int, timeout: float = 60.0) -> tuple[int, str]:
    """Stream ``start..end`` from ``url``; return ``(bytes, sha256)``. Nothing is kept."""
    digest = hashlib.sha256()
    count = 0
    with http_get(url, {"Range": f"bytes={start}-{end}"}, timeout=timeout) as response:
        while True:
            chunk = response.read(MP.CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
    return count, digest.hexdigest()


def run(cmd: list[str], timeout: float = 120.0) -> dict:
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {
        "cmd": [os.path.basename(cmd[0])] + cmd[1:],
        "rc": proc.returncode,
        "seconds": round(time.monotonic() - started, 2),
        "stdout": proc.stdout[-8000:],
        "stderr": proc.stderr[-4000:],
    }


def start_proxy(args) -> MediaProxy:
    proxy = MediaProxy(args.host, args.port, log=log, max_height=1080, timeout=30.0).start()
    log(f"proxy listening on http://{args.host}:{args.port} (pid {os.getpid()})")
    return proxy


# --- commands -------------------------------------------------------------------------------


def cmd_resolve(args) -> dict:
    results = []
    for label, (video_id, max_height) in VIDEOS.items():
        media, seconds = timed_resolve(video_id, max_height)
        entry = {"label": label, "max_height": max_height, **annotate(media, seconds)}
        entry["ok"] = seconds <= 5.0 and (media.video.height or 0) <= max_height
        if label == "capped":
            # The cap is only proved if the same video offers something taller.
            tallest = _tallest_avc1(video_id)
            entry["tallest_avc1"] = tallest
            entry["ok"] = bool(entry["ok"] and tallest and tallest > max_height)
        results.append(entry)
        log(
            f"resolve {label}: {video_id} {seconds:.2f}s "
            f"v={media.video.itag}@{media.video.height}p a={media.audio.itag}"
        )
    payload = {
        "what": "three real videos resolved on CT 121 (no download)",
        **env_facts(),
        "budget_s": 5.0,
        "videos": results,
        "ok": all(entry["ok"] for entry in results),
    }
    write_json(args.out, "40-resolve.json", payload)
    return payload


def _tallest_avc1(video_id: str) -> int | None:
    """Ask the extractor once more for the tallest direct avc1 this video offers."""
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception:  # noqa: BLE001 - the measurement must not die on a second call
        return None
    heights = [
        f.get("height") or 0
        for f in (info.get("formats") or [])
        if str(f.get("vcodec") or "").startswith("avc1")
        and str(f.get("protocol") or "") in ("https", "http")
        and not f.get("manifest_url")
    ]
    return max(heights) if heights else None


def cmd_mpd(args) -> dict:
    proxy = start_proxy(args)
    try:
        media, seconds = timed_resolve(LONG_ID, LONG_HEIGHT)
        sid = proxy.open_session(media)
        manifest = proxy.manifest_url_for(sid)
        mpd_text = proxy.mpd_for(sid)
        session = proxy.sessions[sid]
        probe = run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", manifest]
        )
        streams = []
        try:
            parsed = json.loads(probe["stdout"] or "{}")
            for stream in parsed.get("streams", []):
                streams.append(
                    {
                        k: stream.get(k)
                        for k in (
                            "index",
                            "codec_type",
                            "codec_name",
                            "codec_tag_string",
                            "width",
                            "height",
                            "channels",
                            "sample_rate",
                            "duration",
                        )
                        if stream.get(k) is not None
                    }
                )
            duration = (parsed.get("format") or {}).get("duration")
        except json.JSONDecodeError:
            duration = None
        listing = run([FFPROBE, "-hide_banner", "-i", manifest])
        payload = {
            "what": "ffprobe and ffmpeg read the synthesised MPD through the proxy",
            **env_facts(),
            "video_id": media.video_id,
            "resolve_s": round(seconds, 3),
            "manifest_url": manifest,
            "manifest": mpd_text,
            "sizes": {
                "video_total": session.video.total,
                "audio_total": session.audio.total,
                "video_init": session.video.init_range,
                "video_index": session.video.index_range,
                "audio_init": session.audio.init_range,
                "audio_index": session.audio.index_range,
            },
            "ffprobe": {**probe, "json": None},
            "streams": streams,
            "format_duration_s": duration,
            "ffprobe_listing": listing,
        }
        codecs = {s.get("codec_type"): s.get("codec_name") for s in streams}
        payload["ok"] = (
            probe["rc"] == 0
            and codecs.get("video") == "h264"
            and codecs.get("audio") == "aac"
            and abs(float(duration or 0) - media.duration_s) < 1.0
        )
        write_json(args.out, "40-mpd.json", payload)
        log(f"mpd: ffprobe streams={codecs} duration={duration} rc={probe['rc']}")
        return payload
    finally:
        proxy.stop()


def cmd_seek(args) -> dict:
    proxy = start_proxy(args)
    try:
        media, _ = timed_resolve(LONG_ID, LONG_HEIGHT)
        sid = proxy.open_session(media)
        proxy.mpd_for(sid)  # discovery: init/index ranges + size
        session = proxy.sessions[sid]
        head, total = proxy._read_head(session, "video", 512 * 1024)
        index = session.video.index_range
        if not index:
            raise SystemExit("no sidx: the seek measurement needs a fragmented source")
        sidx = parse_sidx(head, index[0], limit=len(head))
        start, end, media_start = sidx.range_at(SEEK_AT_S)
        log(f"seek: t={SEEK_AT_S}s -> bytes {start}-{end} (media {media_start:.1f}s, total {total})")

        source_bytes, source_sha = fetch_range(media.video.url, start, end)
        track_url = f"http://{args.host}:{args.port}/media/{sid}/0"
        proxy_bytes, proxy_sha = fetch_range(track_url, start, end)
        direct_pts = _fragment_pts(head, index, media.video.url, start, end)

        # Two client paths, both measured, both kept in the record:
        #  - the track URL itself (what a <video> element or an MSE fetcher does),
        #  - the MPD through ffmpeg's own DASH demuxer (reported, not assumed to work).
        track_seek = _probe_seek(track_url, SEEK_AT_S, timeout=60.0)
        dash_seek = _probe_seek(proxy.manifest_url_for(sid), SEEK_AT_S, timeout=25.0)
        dash_ss = _dash_ffmpeg_ss(proxy.manifest_url_for(sid), SEEK_AT_S)
        track_ok = (
            track_seek.get("first_pts_s") is not None
            and abs(track_seek["first_pts_s"] - SEEK_AT_S) < 10.0
        )

        payload = {
            "what": "a seek to 7200 s returns the source's own bytes",
            **env_facts(),
            "video_id": media.video_id,
            "at_s": SEEK_AT_S,
            "sidx_timescale": sidx.timescale,
            "subsegments": len(sidx.references),
            "byte_range": [start, end],
            "media_seconds_of_that_range": media_start,
            "source": {"bytes": source_bytes, "sha256": source_sha},
            "proxied": {"bytes": proxy_bytes, "sha256": proxy_sha},
            "direct_fragment_first_pts_s": direct_pts,
            "seek_via_track_url": track_seek,
            "seek_via_mpd_ffprobe": dash_seek,
            "seek_via_mpd_ffmpeg_ss": dash_ss,
            "ok": (
                source_bytes == proxy_bytes == end - start + 1
                and source_sha == proxy_sha
                and direct_pts is not None
                and abs(direct_pts - SEEK_AT_S) < 10.0
                and track_ok
            ),
        }
        write_json(args.out, "40-seek-7200.json", payload)
        log(
            f"seek: bytes {proxy_bytes} sha match={source_sha == proxy_sha} "
            f"fragment_pts={direct_pts} track_seek_pts={track_seek.get('first_pts_s')} "
            f"mpd_seek_pts={dash_seek.get('first_pts_s')}"
        )
        return payload
    finally:
        proxy.stop()


def _fragment_pts(head: bytes, index: tuple[int, int], url: str, start: int, end: int) -> float | None:
    """Fetch the fragment straight from the source, wrap it with the init segment, probe it.

    No token ever reaches an argv: the file on disk is the only thing ffprobe sees.
    """
    if not shutil.which(FFPROBE):
        return None
    workdir = tempfile.mkdtemp(prefix="nacmedia-seek-")
    mine = os.path.join(workdir, "fragment.mp4")
    try:
        init_end = MP.segment_ranges(head)["init"][1]
        with http_get(url, {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}) as r:
            fragment = r.read()
        with open(mine, "wb") as handle:
            handle.write(head[: init_end + 1])
            handle.write(fragment)
        result = run(
            [
                FFPROBE,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "frame=pts_time",
                "-of",
                "csv=p=0",
                "-read_intervals",
                "%+#2",
                mine,
            ]
        )
        return _first_pts(result["stdout"])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _probe_seek(url: str, at: float, timeout: float = 60.0) -> dict:
    """``ffprobe -read_intervals at%+#2`` and the first presentation time it reports."""
    if not shutil.which(FFPROBE):
        return {"first_pts_s": None, "error": "no ffprobe"}
    cmd = [
        FFPROBE,
        "-v",
        "warning",
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=pts_time",
        "-of",
        "csv=p=0",
        "-read_intervals",
        f"{at:.0f}%+#2",
        url,
    ]
    try:
        result = run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "cmd": [os.path.basename(cmd[0])] + cmd[1:],
            "timeout_s": timeout,
            "first_pts_s": None,
            "note": "no answer within the budget: the demuxer reads from 0 instead of "
            "seeking to the interval",
        }
    return {
        "cmd": result["cmd"],
        "rc": result["rc"],
        "seconds": result["seconds"],
        "first_pts_s": _first_pts(result["stdout"]),
        "stderr": result["stderr"][-800:],
    }


def _dash_ffmpeg_ss(manifest: str, at: float) -> dict:
    """What ffmpeg's DASH demuxer does with ``-ss``: reported verbatim, including the byte
    offset it asked the proxy for (which proves the seek arithmetic reached it)."""
    if not shutil.which(FFPROBE):
        return {"error": "no ffmpeg"}
    workdir = tempfile.mkdtemp(prefix="nacmedia-dash-")
    target = os.path.join(workdir, "seek.mp4")
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-v",
        "warning",
        "-ss",
        f"{at:.0f}",
        "-copyts",
        "-i",
        manifest,
        "-t",
        "2",
        "-c",
        "copy",
        "-y",
        target,
    ]
    try:
        result = run(cmd, timeout=30.0)
    except subprocess.TimeoutExpired:
        return {
            "timeout_s": 30.0,
            "stderr": "",
            "requested_offset": None,
            "note": "ffmpeg kept reading from the start instead of seeking",
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    stderr = result["stderr"] or ""
    offset = re.search(r"root atom offset 0x([0-9a-f]+)", stderr)
    return {
        "cmd": result["cmd"],
        "rc": result["rc"],
        "requested_offset": int(offset.group(1), 16) if offset else None,
        "seek_refused": "could not seek to position" in stderr,
        "stderr": stderr[-800:],
    }


def _first_pts(stdout: str) -> float | None:
    """First presentation time in ffprobe's csv output (``7195.840000,`` on this build)."""
    for line in stdout.splitlines():
        token = line.split(",")[0].strip()
        if not token or token.upper() == "N/A":
            continue
        try:
            return float(token)
        except ValueError:
            continue
    return None


def cmd_rss(args) -> dict:
    proxy = start_proxy(args)
    try:
        media, _ = timed_resolve(LONG_ID, LONG_HEIGHT)
        sid = proxy.open_session(media)
        leaf = "0"
        span = 32 * 1024 * 1024
        children = [
            _spawn_reader(args, sid, leaf, 0, span, chunk=64 * 1024, delay=0.005),
            _spawn_reader(args, sid, leaf, 8 * 1024 * 1024, span, chunk=64 * 1024, delay=0.005),
        ]
        samples = []
        started = time.monotonic()
        while any(child.poll() is None for child in children):
            samples.append(
                {
                    "t": round(time.monotonic() - started, 2),
                    "rss_mb": proxy.stats()["rss_mb"],
                    "ps_rss_mb": _ps_rss_mb(os.getpid()),
                }
            )
            time.sleep(0.1)
            if time.monotonic() - started > 120:
                break
        results = [_reader_result(child) for child in children]
        peak = max((s["rss_mb"] for s in samples), default=0.0)
        peak_ps = max((s["ps_rss_mb"] or 0.0 for s in samples), default=0.0)
        payload = {
            "what": "RSS with two concurrent readers (stats() + ps), budget 150 MB",
            **env_facts(),
            "video_id": media.video_id,
            "sid": sid,
            "readers": results,
            "span_bytes_each": span,
            "samples": samples[:: max(1, len(samples) // 20)],
            "peak_stats_rss_mb": peak,
            "peak_ps_rss_mb": peak_ps,
            "proxy_stats_final": proxy.stats(),
            "budget_mb": MP.RSS_BUDGET_MB,
            "ok": bool(
                all(r.get("ok") for r in results)
                and peak < MP.RSS_BUDGET_MB
                and peak_ps < MP.RSS_BUDGET_MB
            ),
        }
        write_json(args.out, "40-rss.json", payload)
        log(f"rss: peak stats={peak} MB ps={peak_ps} MB readers={[r.get('bytes') for r in results]}")
        return payload
    finally:
        proxy.stop()


def _ps_rss_mb(pid: int) -> float | None:
    proc = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    text = proc.stdout.strip()
    return round(int(text) / 1024.0, 1) if text.isdigit() else None


def _spawn_reader(
    args, sid: str, leaf: str, start: int, count: int, *, chunk: int, delay: float
) -> subprocess.Popen:
    cmd = [
        sys.executable,
        os.path.abspath(__file__),
        "reader",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--sid",
        sid,
        "--leaf",
        leaf,
        "--start",
        str(start),
        "--bytes",
        str(count),
        "--chunk",
        str(chunk),
        "--delay",
        str(delay),
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)


def _reader_result(child: subprocess.Popen, watcher=None) -> dict:
    progress: list[int] = []
    final: str | None = None
    for line in child.stdout or []:
        line = line.strip()
        if not line:
            continue
        if line.startswith("progress "):
            progress.append(int(line.split()[1]))
            if watcher is not None:
                watcher(progress[-1])
        else:
            final = line
    child.wait(timeout=180)
    result: dict = {"progress_max": progress[-1] if progress else 0}
    if final:
        try:
            result.update(json.loads(final))
        except json.JSONDecodeError:
            result["raw"] = final
    err = (child.stderr.read() if child.stderr else "") or ""
    if err.strip():
        result["stderr"] = err[-600:]
    result["rc"] = child.returncode
    result["ok"] = bool(
        result.get("bytes") == result.get("declared") and result.get("rc") == 0 and result.get("auth")
    )
    return result


def cmd_expiry(args) -> dict:
    """Force the source token stale in the middle of a body; the same client response must finish."""
    proxy = start_proxy(args)
    try:
        media, _ = timed_resolve(LONG_ID, LONG_HEIGHT)
        sid = proxy.open_session(media)
        proxy.mpd_for(sid)
        session = proxy.sessions[sid]
        span = 32 * 1024 * 1024
        proxy.faults["drop_upstream_after"] = 12 * 1024 * 1024
        expired_at: dict = {}

        def stale(_progress: int) -> None:
            if expired_at:
                return
            with session.lock:
                session.media = _stale(media)
            expired_at["bytes"] = _progress
            expired_at["when"] = time.monotonic()
            log(f"expiry: source token made stale at reader {_progress} B")

        child = _spawn_reader(args, sid, "0", 0, span, chunk=64 * 1024, delay=0.005)
        stop_watch = threading.Event()

        def clear_fault_when_refreshed() -> None:
            """One dropped connection: after the re-resolution the source is healthy again."""
            while not stop_watch.wait(0.05):
                if session.refreshes >= 1:
                    proxy.faults.pop("drop_upstream_after", None)
                    log("expiry: fault cleared after the refresh")
                    return

        watcher = threading.Thread(target=clear_fault_when_refreshed, daemon=True)
        watcher.start()
        result = _reader_result(child, watcher=stale)
        stop_watch.set()
        fresh, _ = timed_resolve(LONG_ID, LONG_HEIGHT)
        source_bytes, source_sha = fetch_range(fresh.video.url, 0, span - 1)
        payload = {
            "what": "a forced token expiry in the middle of a body, recovered in the same response",
            **env_facts(),
            "video_id": media.video_id,
            "sid": sid,
            "reader": result,
            "expected_bytes": span,
            "stale_token_at_reader_bytes": expired_at.get("bytes"),
            "fault_injection": (
                "proxy.faults['drop_upstream_after']=12 MiB: the source "
                "connection is closed mid-body, because an already-authorised "
                "download does not die of its own expired token"
            ),
            "source": {"bytes": source_bytes, "sha256": source_sha},
            "refreshes": session.refreshes,
            "proxy_stats": proxy.stats(),
            "ok": bool(
                result.get("ok")
                and result.get("bytes") == span
                and result.get("sha256") == source_sha
                and session.refreshes >= 1
            ),
        }
        write_json(args.out, "40-expiry.json", payload)
        log(
            f"expiry: bytes={result.get('bytes')} sha_match="
            f"{result.get('sha256') == source_sha} refreshes={session.refreshes}"
        )
        return payload
    finally:
        proxy.stop()


def _stale(media: ResolvedMedia) -> ResolvedMedia:
    """Same file, same itags, dead token: only the URL is rewritten."""
    past = int(time.time()) - 120

    def rewrite(url: str) -> str:
        if "expire=" in url:
            head, _, rest = url.partition("expire=")
            tail = rest.split("&", 1)
            return head + f"expire={past}" + ("&" + tail[1] if len(tail) > 1 else "")
        return url + f"&expire={past}"

    return dataclass_replace(
        media,
        video=Track(**{**media.video.__dict__, "url": rewrite(media.video.url)}),
        audio=Track(**{**media.audio.__dict__, "url": rewrite(media.audio.url)}),
    )


def _classify(text: str, own: str) -> dict:
    """Every URL in ``text``, split into ours and the source's; plus token parameter names."""
    urls = re.findall(r"https?://[^\s\"\'\\)]+", text)
    source = [
        u for u in urls if "googlevideo" in u or "youtube.com/watch" in u or "manifest.googlevideo" in u
    ]
    ours = [u for u in urls if u not in source and (own in u or "127.0.0.1" in u)]
    other = [u for u in urls if u not in source and u not in ours]
    return {
        "urls": len(urls),
        "own": len(ours),
        "source": source,
        "other": other,
        "token_params": [p for p in TOKEN_PATTERNS if p in text],
    }


def cmd_scan(args) -> dict:
    """Grep our own evidence before claiming anything: no source token may survive.

    The scanner is self-tested first: a synthetic googlevideo URL must be flagged, otherwise a
    clean verdict would only mean the predicate is broken (A33).
    """
    own = f"{args.host}:{args.port}"
    control = _classify('{"u": "https://rr1---sn-x.googlevideo.com/videoplayback?expire=1&sig=S"}', own)
    control_ok = (
        len(control["source"]) == 1
        and "sig=" in control["token_params"]
        and "expire=" in control["token_params"]
    )
    report = []
    ok = control_ok
    for name in sorted(os.listdir(args.out)):
        if not name.startswith("40-") or name.endswith("40-token-scan.txt"):
            continue
        path = os.path.join(args.out, name)
        if not os.path.isfile(path):
            continue
        found = _classify(open(path, encoding="utf-8", errors="replace").read(), own)
        entry = {
            "file": name,
            "urls": found["urls"],
            "own": found["own"],
            "source": [u[:48] for u in found["source"]][:5],
            "other": [u[:48] for u in found["other"]][:5],
            "token_params": found["token_params"],
        }
        report.append(entry)
        ok = ok and not found["source"] and not found["token_params"] and not found["other"]
    lines = [
        "# token scan of evidence/40-* — task B",
        f"# when: {stamp()}",
        f"# host: {socket.gethostname()}  own URLs = http://{own}/media/...",
        f"# scanner self-test (must flag a synthetic token): {'PASS' if control_ok else 'FAIL'}",
        "",
    ]
    for entry in report:
        lines.append(
            f"{entry['file']}: urls={entry['urls']} own={entry['own']} "
            f"source={entry['source']} other={entry['other']} "
            f"token_params={entry['token_params']}"
        )
    lines += ["", f"VERDICT: {'clean' if ok else 'TOKEN LEAK OR BROKEN SCANNER'}"]
    path = os.path.join(args.out, "40-token-scan.txt")
    os.makedirs(args.out, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    log(
        f"scan: {'clean' if ok else 'LEAK'} ({len(report)} files, self-test "
        f"{'PASS' if control_ok else 'FAIL'}) -> {path}"
    )
    return {"ok": ok, "self_test": control_ok, "files": report}


def cmd_reader(args) -> int:
    """Child process: read a range through the proxy, hash it, report. No token in argv."""
    url = f"http://{args.host}:{args.port}/media/{args.sid}/{args.leaf}"
    end = args.start + args.bytes - 1
    digest = hashlib.sha256()
    count = 0
    declared = None
    auth = True
    try:
        request = urllib.request.Request(url, headers={"Range": f"bytes={args.start}-{end}"})
        with urllib.request.urlopen(request, timeout=120) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            reported = 0
            while True:
                chunk = response.read(args.chunk)
                if not chunk:
                    break
                digest.update(chunk)
                count += len(chunk)
                if count - reported >= 1024 * 1024:
                    reported = count
                    print(f"progress {count}", flush=True)
                if args.delay:
                    time.sleep(args.delay)
    except urllib.error.HTTPError as exc:
        auth = False
        print("progress 0", flush=True)
        print(
            json.dumps(
                {"error": f"HTTP {exc.code}", "bytes": 0, "declared": 0, "sha256": "", "auth": False}
            ),
            flush=True,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - a child reports, it does not raise
        print(
            json.dumps(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "bytes": count,
                    "declared": declared,
                    "sha256": digest.hexdigest(),
                    "auth": auth,
                }
            ),
            flush=True,
        )
        return 1
    print(
        json.dumps({"bytes": count, "declared": declared, "sha256": digest.hexdigest(), "auth": auth}),
        flush=True,
    )
    return 0


def cmd_all(args) -> dict:
    """Each step in its own process: every evidence file comes from a fresh interpreter, exactly
    as if the command had been run alone — and one failure cannot take the others down."""
    summary = {"what": "task B done-when, in order", **env_facts(), "steps": {}}
    for name in ("resolve", "mpd", "seek", "rss", "expiry"):
        log(f"=== {name} ===")
        cmd = [
            sys.executable,
            os.path.abspath(__file__),
            name,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--out",
            args.out,
            "--ffprobe",
            FFPROBE,
        ]
        try:
            result = run(cmd, timeout=900.0)
            summary["steps"][name] = {
                "ok": result["rc"] == 0,
                "rc": result["rc"],
                "seconds": result["seconds"],
                "tail": result["stdout"][-1200:],
            }
        except subprocess.TimeoutExpired:
            summary["steps"][name] = {"ok": False, "error": "timed out after 900 s"}
    scan = cmd_scan(args)
    summary["steps"]["scan"] = {"ok": scan["ok"], "files": len(scan["files"])}
    summary["ok"] = all(step.get("ok") for step in summary["steps"].values())
    write_json(args.out, "40-summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    global FFPROBE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["all", "resolve", "mpd", "seek", "rss", "expiry", "scan", "reader"]
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--sid", default="")
    parser.add_argument("--leaf", default="0")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--bytes", type=int, default=1024)
    parser.add_argument("--chunk", type=int, default=64 * 1024)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--ffprobe", default=FFPROBE)
    args = parser.parse_args(argv)
    FFPROBE = args.ffprobe

    if args.command == "reader":
        return cmd_reader(args)

    commands = {
        "all": cmd_all,
        "resolve": cmd_resolve,
        "mpd": cmd_mpd,
        "seek": cmd_seek,
        "rss": cmd_rss,
        "expiry": cmd_expiry,
        "scan": cmd_scan,
    }
    payload = commands[args.command](args)
    if args.command != "all":
        print(json.dumps({k: v for k, v in payload.items() if k != "manifest"}, indent=1)[:3000])
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
