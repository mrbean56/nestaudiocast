"""YouTube URL -> playable streams, with no media kept on disk (contracts §4).

One extraction of one video id yields both tracks (§4: encoder priming identical on the receiver
and on the group). The URLs carry short-lived tokens: they live in :class:`Track` only, and every
log or evidence line goes through :func:`redact`.

Selection rules (contracts §4), measured on CT 121 with yt-dlp 2026.10.21:
- video: `avc1` (H.264), height <= ``max_height``, **direct https** only. yt-dlp also lists HLS
  variants (`protocol == "m3u8_native"`, a ``manifest_url`` and no ``url``): those are not a byte
  range of an MP4, so they are excluded — a range proxy can only forward what it can address.
- audio: AAC (``mp4a``), format 140 when present, else the nearest bitrate.
- live / upcoming / post-live, age-gated, private: :class:`ResolveError` — a diagnosis, not a
  traceback.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.parse

from .contracts import ResolvedMedia, Track

_REDACT_TAIL = 6

#: A YouTube id is exactly 11 characters of this alphabet — the whole token, never a prefix
#: (A33: a test whose predicate was a prefix match passed on a string ending in ``.m4a``).
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: Hosts we accept a link from. ``youtu.be`` is the short form.
_SHORT_HOST = "youtu.be"
_LONG_HOSTS = frozenset(
    {
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
    }
)
#: Path prefixes that carry the id as the second segment.
_PATH_FORMS = frozenset({"shorts", "embed", "live", "v", "e"})

#: yt-dlp is asked for every format; selection is ours, so the rule stays in one place.
_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "format": None,
    "noprogress": True,
    "cachedir": False,
    "extractor_retries": 1,
    "retries": 1,
}

#: The extractor call, isolated so the offline suite can substitute a recorded answer.
#: (``resolve()``'s signature is frozen — contracts test checks its exact parameter list — so the
#: seam is a module attribute, not a keyword argument.)
_ytdlp_extract = None


class ResolveError(RuntimeError):
    """The URL cannot be resolved (private video, age gate, live stream, no avc1 track…)."""


def redact(url: str) -> str:
    """``scheme://host/<first 6 chars>…`` — the only form allowed in a log or an evidence file."""
    m = re.match(r"^(?P<scheme>[a-z0-9+.-]+)://(?P<host>[^/?#]+)(?P<path>[^?#]*)", url or "")
    if not m:
        return "<unparsable-url>"
    tail = m.group("path").lstrip("/")[:_REDACT_TAIL]
    return f"{m.group('scheme')}://{m.group('host')}/{tail}…"


def video_id_of(url: str) -> str:
    """11-char YouTube id, or raise ResolveError. Never a prefix match (A33: a test whose
    predicate was wrong passed happily).

    Accepted: a bare id, ``youtu.be/<id>``, ``/watch?v=<id>``, ``/shorts|embed|live|v|e/<id>``,
    with any extra query string. Everything else — including an id followed by more path, or a
    12-character token — raises.
    """
    if not isinstance(url, str):
        raise ResolveError(f"not a string: {type(url).__name__}")
    text = url.strip()
    if not text:
        raise ResolveError("empty url")
    if _ID_RE.match(text):
        return text
    if "://" not in text:
        raise ResolveError(f"not a URL and not an 11-char id: {redact(text)}")

    parts = urllib.parse.urlsplit(text)
    host = (parts.hostname or "").lower()
    bare = host[4:] if host.startswith("www.") else host
    if bare != _SHORT_HOST and bare not in _LONG_HOSTS:
        raise ResolveError(f"not a YouTube host: {bare or '<none>'}")

    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if "v" in query:
        candidates = query["v"]
        if len(candidates) != 1:
            raise ResolveError(f"ambiguous v= parameter ({len(candidates)} values)")
        if not _ID_RE.match(candidates[0]):
            raise ResolveError(f"bad v= value: {candidates[0]!r}")
        return candidates[0]

    segments = [s for s in parts.path.split("/") if s]
    if bare == _SHORT_HOST:
        if len(segments) == 1 and _ID_RE.match(segments[0]):
            return segments[0]
        raise ResolveError(f"not a youtu.be/<id> path: {redact(text)}")
    if len(segments) == 2 and segments[0] in _PATH_FORMS and _ID_RE.match(segments[1]):
        return segments[1]
    raise ResolveError(f"no 11-char video id in {redact(text)}")


# --- extraction -----------------------------------------------------------------------------


def _extract(url: str, timeout: float) -> dict:
    """Run one extraction under a hard wall-clock deadline; any failure becomes ResolveError."""
    global _ytdlp_extract
    box: dict = {}

    def work() -> None:
        try:
            box["info"] = _ytdlp_extract(url, timeout)
        except BaseException as exc:  # noqa: BLE001 - reported as a diagnosis, never re-raised raw
            box["error"] = exc

    thread = threading.Thread(target=work, name="resolve-extract", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise ResolveError(f"extraction timed out after {timeout:.0f}s")
    if "error" in box:
        raise ResolveError(_message(box["error"]))
    info = box.get("info")
    if not isinstance(info, dict):
        raise ResolveError("extractor returned no information")
    return info


def _message(exc: BaseException) -> str:
    """yt-dlp's exceptions carry the token in their text: keep the reason, drop the URLs."""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    text = re.sub(r"https?://\S+", "<url>", text)
    return f"{type(exc).__name__}: {text[:200]}"


def _ytdlp(url: str, timeout: float) -> dict:
    import yt_dlp  # local: keeps the module importable without the package (contracts test)

    opts = dict(_YDL_OPTS)
    opts["socket_timeout"] = max(1.0, float(timeout))
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if isinstance(info, dict) and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if len(entries) != 1:
            raise ResolveError(f"playlist with {len(entries)} entries, need one video")
        info = entries[0]
    return info


_ytdlp_extract = _ytdlp


# --- selection ------------------------------------------------------------------------------


def _is_direct(fmt: dict) -> bool:
    """A byte-addressable MP4 over https — no playlist, no DRM."""
    if not fmt.get("url") or fmt.get("manifest_url"):
        return False
    if str(fmt.get("protocol") or "") not in ("https", "http"):
        return False
    if fmt.get("has_drm"):
        return False
    return not fmt.get("fragments")


def _pick_video(formats: list[dict], max_height: int) -> dict:
    usable = [
        f
        for f in formats
        if _is_direct(f)
        and str(f.get("vcodec") or "").startswith("avc1")
        and str(f.get("acodec") or "none") in ("none", "")
        and isinstance(f.get("height"), int)
    ]
    capped = [f for f in usable if f["height"] <= int(max_height)]
    if not capped:
        smallest = min((f["height"] for f in usable), default=None)
        if smallest is None:
            raise ResolveError("no direct avc1 video track in the extraction")
        raise ResolveError(f"no avc1 track at or below {max_height}p (smallest available: {smallest}p)")
    return max(capped, key=lambda f: (f["height"], f.get("fps") or 0.0, f.get("tbr") or 0.0))


def _pick_audio(formats: list[dict]) -> dict:
    usable = [
        f
        for f in formats
        if _is_direct(f)
        and str(f.get("acodec") or "").startswith("mp4a")
        and str(f.get("vcodec") or "none") in ("none", "")
    ]
    if not usable:
        raise ResolveError("no direct AAC (mp4a) audio track in the extraction")
    exact = [f for f in usable if str(f.get("format_id")) == "140"]
    if exact:
        return exact[0]
    return min(usable, key=lambda f: abs(float(f.get("abr") or f.get("tbr") or 0.0) - 128.0))


def _itag(fmt: dict) -> int:
    fid = str(fmt.get("format_id") or "")
    return int(fid) if fid.isdigit() else 0


def _range_pair(fmt: dict, key: str) -> tuple[int, int] | None:
    value = fmt.get(key)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    return None


def _expiry(url: str) -> float | None:
    """The source token's own ``expire=<unix>``, if the extractor left it in the URL."""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        stamp = query.get("expire", [None])[0]
        return float(stamp) if stamp and stamp.isdigit() else None
    except (ValueError, AttributeError):
        return None


def _check_not_live(info: dict) -> None:
    status = str(info.get("live_status") or "").lower()
    if info.get("is_live") or status in ("is_live", "is_upcoming", "post_live"):
        raise ResolveError(f"live stream ({status or 'is_live'}): no known duration, not a bounded file")
    if int(info.get("age_limit") or 0) >= 18:
        raise ResolveError("age-gated video: the extractor returns no playable formats")


def resolve(url: str, *, max_height: int = 1080, timeout: float = 30.0) -> ResolvedMedia:
    """Extract the best ``avc1`` ≤ ``max_height`` and an AAC audio track for ``url``.

    Returns both tracks from the same extraction (§4: same video id, same moment, so encoder
    priming is identical for the receiver and for the group). ``expires_at`` is the earliest
    expiry of the two source URLs; the proxy refreshes before it.
    """
    video_id = video_id_of(url)
    target = url.strip() if "://" in url else f"https://www.youtube.com/watch?v={video_id}"
    info = _extract(target, float(timeout))
    _check_not_live(info)

    resolved_id = str(info.get("id") or "")
    if resolved_id != video_id:
        # A redirect to another id would silently pair two different extractions.
        raise ResolveError(f"extractor returned id {resolved_id!r} for {video_id!r}")

    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise ResolveError(f"no duration ({duration!r}): not a bounded file")

    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    if not formats:
        raise ResolveError("extractor returned no formats")

    vfmt = _pick_video(formats, max_height)
    afmt = _pick_audio(formats)
    fetched_at = time.time()

    video = Track(
        itag=_itag(vfmt),
        codecs=str(vfmt.get("vcodec") or ""),
        content_type="video/mp4",
        url=str(vfmt.get("url") or ""),
        width=vfmt.get("width"),
        height=vfmt.get("height"),
        fps=float(vfmt["fps"]) if vfmt.get("fps") else None,
        init_range=_range_pair(vfmt, "init_range"),
        index_range=_range_pair(vfmt, "index_range"),
    )
    audio = Track(
        itag=_itag(afmt),
        codecs=str(afmt.get("acodec") or ""),
        content_type="audio/mp4",
        url=str(afmt.get("url") or ""),
        width=None,
        height=None,
        fps=None,
        init_range=_range_pair(afmt, "init_range"),
        index_range=_range_pair(afmt, "index_range"),
    )

    stamps = [s for s in (_expiry(video.url), _expiry(audio.url)) if s]
    # 60 s of headroom: a refresh that starts one chunk too late truncates a body.
    expires_at = (min(stamps) - 60.0) if stamps else fetched_at + 1800.0

    return ResolvedMedia(
        video_id=video_id,
        title=str(info.get("title") or ""),
        duration_s=float(duration),
        video=video,
        audio=audio,
        fetched_at=fetched_at,
        expires_at=expires_at,
    )
