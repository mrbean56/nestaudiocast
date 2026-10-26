"""A group that exists only in memory (contracts §1). No socket, no sound, ever."""

from __future__ import annotations

import time

from v020.contracts import (
    PLAYER_IDLE,
    PLAYER_PAUSED,
    PLAYER_PLAYING,
    GroupSample,
)


class UnsafeGroup(RuntimeError):
    pass


class FakeGroup:
    def __init__(self, name: str = "Cuisine", clock=time.monotonic, unsafe: bool = False):
        self.name = name
        self.clock = clock
        self.unsafe = unsafe
        self.connected = False
        self.media = 0.0
        self.state = PLAYER_IDLE
        self.rate = 1.0
        self.session_id: int | None = None
        self.volume: float | None = None
        self.muted: bool | None = None
        self.calls: list[str] = []  # every state-changing call, in order
        self.prepared_at: float | None = None
        self.played_at: float | None = None
        self._playing_since: float | None = None

    # -- lifecycle ---------------------------------------------------------------------------
    def connect(self) -> None:
        if self.unsafe:
            raise UnsafeGroup("group contains a video device")
        self.connected = True
        self.session_id = 4242

    def reconnect(self) -> None:
        self.connected = False
        self.connect()

    def close(self) -> None:
        self.connected = False

    # -- transport ---------------------------------------------------------------------------
    def prepare(self, url: str, content_type: str, at_time: float) -> bool:
        self.calls.append("prepare")
        self.media = float(at_time)
        self.state = PLAYER_PAUSED
        self.prepared_at = self.clock()
        return True

    def play(self) -> None:
        self.calls.append("play")
        self.played_at = self.clock()
        self._playing_since = self.media
        self.state = PLAYER_PLAYING

    def pause(self) -> None:
        self.calls.append("pause")
        self.media = self._advance()
        self._playing_since = None
        self.state = PLAYER_PAUSED

    def seek(self, at_time: float) -> None:
        self.calls.append("seek")
        self.media = float(at_time)
        self._playing_since = float(at_time) if self.state == PLAYER_PLAYING else None

    def set_volume(self, level: float, muted: bool | None = None) -> None:
        self.calls.append("volume")
        self.volume, self.muted = float(level), muted

    def stop(self) -> None:
        self.calls.append("stop")
        self.state = PLAYER_IDLE
        self.media = 0.0
        self._playing_since = None

    # -- observation -------------------------------------------------------------------------
    def _advance(self) -> float:
        if self._playing_since is not None and self.state == PLAYER_PLAYING:
            return self._playing_since + (self.clock() - (self.played_at or self.clock()))
        return self.media

    def sample(self) -> GroupSample | None:
        return GroupSample(
            wall=self.clock(), media=self._advance(), player_state=self.state, session_id=self.session_id
        )

    def playhead(self, wall: float) -> float | None:
        if self._playing_since is None:
            return self.media
        return self._playing_since + (wall - (self.played_at or wall))

    def transport_calls(self) -> list[str]:
        """Calls that change what is heard — the list the "no reload after start" test inspects."""
        return [c for c in self.calls if c in ("prepare", "seek", "play", "pause")]
