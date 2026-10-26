"""The control law: pure logic, no I/O (contracts §1-§2).

Stub: signatures are frozen, bodies raise NotImplementedError. Task F implements them, and
tests/v020/sim_v020.py proves them offline with the measured noise of evidence/17, 19, 27.
The controller may never emit ``load`` or ``seek`` once a session is in its steady state.
"""

from __future__ import annotations

from .contracts import Command, GroupSample, Sample


class Controller:
    """Decides what to tell the two sides, given their samples. Emits nothing by itself."""

    def __init__(self, delta_v2: float = 0.0, rate_max: float = 0.002,
                 dead_band_s: float = 0.005, log=None) -> None:
        self.delta_v2, self.rate_max = delta_v2, rate_max
        self.dead_band_s, self.log = dead_band_s, log

    # -- session lifecycle -------------------------------------------------------------------
    def start(self, now: float, video_ready: float, group_ready: float,
              lead_s: float = 0.0) -> list[Command]:
        """Plan the one start event: park the group, compute the receiver's ``play_at``.

        Returns at most one ``play_at`` and one ``play``; schedulable, no I/O (§1).
        """
        raise NotImplementedError("task F")

    def step(self, now: float, video: Sample | None, group: GroupSample | None) -> list[Command]:
        """One control tick. Steady state: rate trims only."""
        raise NotImplementedError("task F")

    def on_seek(self, now: float, position: float) -> list[Command]:
        """User seek: one re-landing is allowed, then steady again."""
        raise NotImplementedError("task F")

    def on_pause(self, now: float) -> list[Command]:
        raise NotImplementedError("task F")

    def on_resume(self, now: float, group_position: float) -> list[Command]:
        """The group leads; the video is told where to be and when (§1)."""
        raise NotImplementedError("task F")

    def on_end(self, now: float) -> list[Command]:
        raise NotImplementedError("task F")

    # -- observation -------------------------------------------------------------------------
    def error(self, group_pos: float, video_pos: float) -> float:
        """``p_group - p_video - DELTA_V2`` (§2)."""
        raise NotImplementedError("task F")

    def steady(self, now: float) -> bool:
        raise NotImplementedError("task F")

    def window_median_ms(self) -> float | None:
        """Median digital error of the last 5 s window of steady samples (§2)."""
        raise NotImplementedError("task F")
