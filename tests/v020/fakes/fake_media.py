"""A resolved media and its proxy, in memory (contracts §4). No download, no disk."""

from __future__ import annotations

import time

from v020.contracts import ResolvedMedia, Track


class FakeMedia:
    @staticmethod
    def resolved(
        video_id: str = "QzomK1fdSUg", duration_s: float = 600.0, height: int = 1080
    ) -> ResolvedMedia:
        now = time.time()
        video = Track(
            itag=137,
            codecs="avc1.640028",
            content_type="video/mp4",
            url="https://rr1---sn-fake.googlevideo.com/videoplayback?token=REDACTED",
            width=1920,
            height=height,
            fps=30.0,
            init_range=(0, 740),
            index_range=(740, 1200),
        )
        audio = Track(
            itag=140,
            codecs="mp4a.40.2",
            content_type="audio/mp4",
            url="https://rr1---sn-fake.googlevideo.com/videoplayback?token=REDACTED",
            init_range=(0, 640),
            index_range=(640, 980),
        )
        return ResolvedMedia(
            video_id=video_id,
            title="fake video",
            duration_s=duration_s,
            video=video,
            audio=audio,
            fetched_at=now,
            expires_at=now + 6 * 3600,
        )


class FakeProxy:
    def __init__(self, host: str = "192.168.2.21", port: int = 7640):
        self.host, self.port = host, port
        self.sessions: dict[str, ResolvedMedia] = {}
        self.refreshes = 0

    def open_session(self, resolved: ResolvedMedia) -> str:
        sid = f"s{len(self.sessions) + 1}"
        self.sessions[sid] = resolved
        return sid

    def close_session(self, sid: str) -> None:
        self.sessions.pop(sid, None)

    def mpd_for(self, sid: str) -> str:
        if sid not in self.sessions:
            raise KeyError(sid)
        d = self.sessions[sid].duration_s
        return (
            f'<?xml version="1.0"?><MPD type="static" mediaPresentationDuration="PT{d}S"'
            f"><Period><AdaptationSet/><Period/></MPD>"
        )

    def audio_url_for(self, sid: str) -> str:
        return f"http://{self.host}:{self.port}/media/{sid}/audio.m4a"

    def refresh(self, sid: str) -> None:
        self.refreshes += 1

    def stats(self) -> dict:
        return {"sessions": len(self.sessions), "rss_mb": 12.0, "refreshes": self.refreshes}
