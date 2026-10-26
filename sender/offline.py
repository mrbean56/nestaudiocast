"""The offline seam set — what makes the skeleton runnable tonight (brief G).

``seams()`` wires the in-memory fakes of ``tests/v020/fakes`` (no socket, no speaker, no
Chromecast, no packet) and a resolver stub that manufactures a :class:`ResolvedMedia`. This is
the mode every test and the ``--self-test`` run use.

The two controllers here are for offline proof, not for production:

* :class:`TrimController` behaves like the control law of contracts §1-§2: a single start plan,
  then rate trims only.
* :class:`HostileController` deliberately misbehaves (it asks for a ``load`` and a ``seek`` on
  every tick) so the session's own guard — not the controller's good manners — is what proves
  "no gaps after start".

Both are offline-only helpers of the owner app; nothing imports them from the product path.
"""

from __future__ import annotations

import re
import sys
import os

from v020.contracts import (
    MSG_LOAD, MSG_PLAY_AT, MSG_RATE, PLAYER_PLAYING, Command, ResolvedMedia,
)

TESTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "tests", "v020")

#: A33's lesson: extract the id with an anchored regex, never a prefix match.
_YT_ID = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})(?:[?&#/]|$)")


class FakeClock:
    """A monotonic clock a test advances by hand, with a matching wall clock."""

    def __init__(self, start: float = 1000.0, wall: float = 1_790_000_000.0) -> None:
        self.value = float(start)
        self.wall = float(wall)

    def __call__(self) -> float:
        return self.value

    def now(self) -> float:
        """Wall clock, advancing with the monotonic one."""
        return self.wall + (self.value - 1000.0)

    def advance(self, seconds: float) -> float:
        self.value += float(seconds)
        return self.value


def video_id_of(url: str) -> str:
    """11-char id from a YouTube URL, or a stable placeholder for offline runs."""
    match = _YT_ID.search(url or "")
    return match.group(1) if match else "QzomK1fdSUg"


def offline_resolver(url: str, *, max_height: int = 1080, timeout: float = 30.0) -> ResolvedMedia:
    """Stand-in for task B: a resolved media that never leaves this process."""
    from fakes import FakeMedia  # tests/v020/fakes — imported lazily, only in offline mode

    return FakeMedia.resolved(video_id=video_id_of(url))


def seams(*, video_id: str | None = None, clock=None):
    """``(link, group, proxy, resolver)`` built from the tests' fakes.

    ``clock`` is handed to both devices so a simulated run shares one clock; the fakes default
    to ``time.monotonic``, which would make the group and the receiver disagree with a
    hand-advanced clock.
    """
    if TESTS_DIR not in sys.path:
        sys.path.insert(0, TESTS_DIR)
    from fakes import FakeGroup, FakeProxy, FakeReceiver

    extra = {} if clock is None else {"clock": clock}
    link = FakeReceiver(**extra)
    group = FakeGroup(**extra)
    proxy = FakeProxy()
    resolver = offline_resolver
    if video_id is not None:
        resolver = lambda url, **kw: offline_resolver(video_id, **kw)  # noqa: E731
    return link, group, proxy, resolver


class TrimController:
    """A controller that obeys §1: one start plan, then rate trims only."""

    def __init__(self, *, rate: float = 1.0, start_position: float = 0.0,
                 always_trim: bool = False) -> None:
        self.rate = float(rate)
        self.start_position = float(start_position)
        self.always_trim = bool(always_trim)
        self.started = False
        self.steps = 0
        self.planned = 0

    def start(self, now: float, video_ready: float, group_ready: float,
              lead_s: float = 0.0) -> list[Command]:
        self.planned += 1
        return [
            Command(kind=MSG_LOAD, payload={"url": "", "content_type": "application/dash+xml",
                                            "media": self.start_position, "autoplay": False}),
            Command(kind="play", payload={"target": "group"}),
            Command(kind=MSG_PLAY_AT, payload={"rx_ms": 0.0, "media": self.start_position}),
        ]

    def step(self, now, video, group) -> list[Command]:
        self.steps += 1
        if not self.started:
            self.started = True
            return []
        # A steady-state correction is a rate trim, never a reload (contracts §1).
        if self.always_trim:
            return [Command(kind=MSG_RATE, payload={"value": 1.0 + self.rate * 0.001})]
        if video is not None and group is not None:
            error_ms = (group.media - video.media) * 1000.0
            if abs(error_ms) > 5.0:
                return [Command(kind=MSG_RATE, payload={"value": 1.0 + self.rate * 0.001})]
        return []

    # unused parts of the contract surface, kept so the object is a drop-in
    def on_seek(self, now, position):
        return [Command(kind="group_seek", payload={"media": float(position)}),
                Command(kind="seek", payload={"media": float(position)})]

    def on_pause(self, now):
        return []

    def on_resume(self, now, group_position):
        return [Command(kind="play", payload={"target": "group"}),
                Command(kind=MSG_PLAY_AT, payload={"rx_ms": 0.0, "media": float(group_position)})]

    def on_end(self, now):
        return []


class HostileController(TrimController):
    """Misbehaves on purpose: asks for a reload (and a re-landing) on every single tick."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.asked_loads = 0
        self.asked_seeks = 0
        self.asked_play_at = 0

    def step(self, now, video, group) -> list[Command]:
        self.steps += 1
        self.asked_loads += 1
        self.asked_seeks += 1
        self.asked_play_at += 1
        return [
            Command(kind=MSG_LOAD, payload={"url": "", "content_type": "application/dash+xml",
                                            "media": 0.0, "autoplay": False}),
            Command(kind="seek", payload={"media": 0.0}),
            Command(kind=MSG_PLAY_AT, payload={"rx_ms": 0.0, "media": 0.0}),
        ]


def is_playing(sample) -> bool:
    return getattr(sample, "player_state", None) == PLAYER_PLAYING
