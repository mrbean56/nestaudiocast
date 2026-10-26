"""Stream the resolved media to the receiver and to the group, without keeping it (contracts §4).

Stub: signatures are frozen, bodies raise NotImplementedError. Task B implements them.
"""

from __future__ import annotations

from .contracts import ResolvedMedia


class MediaProxy:
    """Owns the ``/media/<sid>`` namespace and the byte-range plumbing behind it."""

    def __init__(self, host: str, port: int, log=print) -> None:
        self.host, self.port, self.log = host, port, log
        self.sessions: dict[str, ResolvedMedia] = {}

    def open_session(self, resolved: ResolvedMedia) -> str:
        """Register a resolved media and return its opaque ``sid``."""
        raise NotImplementedError("task B")

    def close_session(self, sid: str) -> None:
        """Revoke a sid; readers in flight get a clean 410, never a truncated body."""
        raise NotImplementedError("task B")

    def mpd_for(self, sid: str) -> str:
        """DASH manifest (§4): ``static``, known duration, SegmentBase + indexRange, both tracks."""
        raise NotImplementedError("task B")

    def audio_url_for(self, sid: str) -> str:
        """``http://<host>:<port>/media/<sid>/audio.m4a`` for the group's DMR."""
        raise NotImplementedError("task B")

    def refresh(self, sid: str) -> None:
        """Re-resolve on 403/expiry and keep the reader alive; log the redacted cause."""
        raise NotImplementedError("task B")

    def stats(self) -> dict:
        """``{"sessions": int, "rss_mb": float, "refreshes": int}`` — the memory bound is 150 MB."""
        raise NotImplementedError("task B")
