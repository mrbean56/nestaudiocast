"""The control law: pure logic, no I/O (contracts §1-§2).

The architecture decision of v0.2.0 (`docs/v020-contracts.md` §1): the **kitchen group leads**
(it free-runs: its ``SET_PLAYBACK_RATE`` is a verified no-op, `evidence/17`) and the **video
follows** through ``playbackRate``. The group may never be reloaded after the start event, so
every steady-state correction must be a rate trim on our own receiver.

Two reaction speeds, never mixed (contracts §1):

=============================  ==========================================================
event                          what this module emits
=============================  ==========================================================
start, seek, resume           one *landing*: the group's ``group_play`` at a scheduled
                               instant, and the receiver's ``play_at{rx_ms, media}``.
                               At most one video-side re-landing, then no transport
                               command until the next user event.
steady (≥ 30 s after one)      ``rate`` only, |rate - 1| ≤ ``RATE_MAX``, inside a 5 ms
                               dead band, slewed (a trim is a rate, not a jump).
pause / end                    the symmetric transport command on both sides.
=============================  ==========================================================

The law in one line (``e = p_group - p_video - DELTA_V2``, positive = the kitchen is ahead):

    u = rate - 1  ⇒  ė = 1 - rate = -u
    u = K_P·ê + K_I·∫ê        (P: close the gap in 1/K_P seconds; I: no residual under drift)

``K_P`` is not a free knob. The drift-only acceptance ("never ask for a trim above
``RATE_MAX/4``") bounds it: at the dead-band edge a pure P term already asks for
``K_P · dead_band ≤ RATE_MAX/4``, i.e. ``K_P ≤ 0.1`` in the contract's numbers. ``K_P = 0.04``
(25 s to close a gap) leaves room for the estimator's own ~2 ms of noise under that ceiling;
``K_I = K_P²/4`` makes the pair critically damped (ω = 0.02 rad/s, ~50 s), slow enough for a 4 Hz
estimate that is a few hundred ms old. Both are measured in ``tests/v020/sim_v020.py`` (see
`notes/labo-sync.md`).

Group-side commands are not CASTv2 messages: §3.3/§1 gives the group `play`, `pause`, `seek`,
`stop` through :class:`v020.group.CastGroup`, and ``Command.kind`` is only documented as a
``MSG_*`` constant (contracts, :class:`~v020.contracts.Command`). The four ``group_*`` kinds
below are this module's names for those calls; see `notes/labo-sync.md` §5 for the amendment
this needs. The executor maps them to the group and honours the payload:

- ``group_play  {at: float, media: float}`` — at controller time ``at``: ``seek(media)`` if the
  group is not already there, then ``play()``. ``at`` is the instant the group's playhead must
  already be at ``media``, which is why it is *earlier* than the receiver's ``play_at`` by
  ``lead_s`` (the group's measured landing delay).
- ``group_seek  {media: float, pause: bool, at: float}`` — park the group: seek, and pause when
  ``pause`` is true, at controller time ``at``.
- ``group_pause {}``, ``group_stop {}`` — no payload.

The receiver's ``play_at`` is self-timed (§3.1): the receiver compensates its own start latency,
so no receiver latency enters this module — only the group's, through ``lead_s``.
"""

from __future__ import annotations

import statistics
from collections import deque

from .contracts import (
    MSG_IDLE,
    MSG_LOAD,
    MSG_PAUSE,
    MSG_PLAY_AT,
    MSG_RATE,
    MSG_SEEK,
    MSG_STOP,
    PLAYER_PLAYING,
    RATE_DEAD_BAND_S,
    RATE_MAX_DEFAULT,
    STEADY_AFTER_S,
    STEADY_WINDOW_S,
    ClockEstimate,
    Command,
    GroupSample,
    Sample,
    digital_error,
)

__all__ = [
    "CMD_GROUP_PLAY",
    "CMD_GROUP_PAUSE",
    "CMD_GROUP_SEEK",
    "CMD_GROUP_STOP",
    "GROUP_COMMANDS",
    "PHASE_IDLE",
    "PHASE_LANDING",
    "PHASE_SETTLING",
    "PHASE_STEADY",
    "PHASE_PAUSED",
    "PHASE_ENDED",
    "Controller",
]

# --- phases of one session -------------------------------------------------------------------

PHASE_IDLE = "idle"
PHASE_LANDING = "landing"  # a start/seek/resume landing is in flight; one re-landing left
PHASE_SETTLING = "settling"  # landed, waiting out the 30 s before rate trims are allowed
PHASE_STEADY = "steady"  # rate trims only
PHASE_PAUSED = "paused"
PHASE_ENDED = "ended"

# --- group-side command kinds (not CASTv2: see the module docstring) --------------------------

CMD_GROUP_PLAY = "group_play"
CMD_GROUP_PAUSE = "group_pause"
CMD_GROUP_SEEK = "group_seek"
CMD_GROUP_STOP = "group_stop"
GROUP_COMMANDS = (CMD_GROUP_PLAY, CMD_GROUP_PAUSE, CMD_GROUP_SEEK, CMD_GROUP_STOP)

#: commands that may never appear once the session is steady (§1: no reload after start)
RELOAD_COMMANDS = (MSG_LOAD, MSG_SEEK, CMD_GROUP_SEEK)

#: commands this module may emit at all — ``load`` is the executor's job, before ``start``
EMITTABLE = (MSG_PLAY_AT, MSG_RATE, MSG_PAUSE, MSG_IDLE, MSG_STOP) + GROUP_COMMANDS


VDO_WINDOW_S = 30.0  # the mean's window; measured, not assumed (evidence/49)
VDO_WARM_S = 2.5  # enough history (several refreshes at 4 Hz) before it is trusted
VDO_JUMP_S = 0.40  # a read this far from the estimate is a seek, not noise
VDO_MIN_SAMPLES = 8


class VideoClock:
    """Filtered video playhead, in the receiver's clock domain (contracts §2).

    Measured on 2026-10-26 (`evidence/49-playhead-noise-dmr.json`, 238 reads at 4 Hz): the
    Chromecast reports a **sawtooth** playhead — σ 60 ms, −153…+146 ms, 31 % of second-to-second
    moves larger than 20 ms — so a law fed the raw value chases it, and the simulation shows what
    that costs (10 ms of symmetric noise is already enough to blow the 10 s convergence target:
    `evidence/44b-sim-video-side.json`).

    Which filter? Not a matter of taste: the candidates were scored on those 238 real reads against
    a local least-squares truth, over identical windows (`notes/labo-sync.md` §5):

        estimator            σ       p50     p95     bias
        10 s mean          8.6 ms   5.2 ms  16.4 ms  +1.7 ms   <- chosen
        10 s median        9.9      22.4    35.1    +21.0
        upper envelope     5.2      82.8    92.1    +83.0   (v0.1's TvClock, tuned for the TV)

    The TV's sawtooth only ever ran *slow* between refreshes, so its envelope is the truth (A20).
    This device scatters on **both sides** of the truth: no percentile is unbiased, but the
    time-average is. Hence a plain mean, and the window is measured too — scored against a single
    global fit of the same 238 reads:

        window    σ       p50     p95     max
        10 s    10.7 ms  9.5 ms  17.2 ms  21.8
        20 s     8.6      7.0    15.3    15.8
        30 s     5.1      4.7    10.2    11.7   <- chosen

    The device carries a slow wobble of a few tens of seconds, which only a long window removes.
    The lag that comes with it costs nothing: a landing resets the window (``_reset_estimator``)
    and the law may not trim until 30 s after an event anyway (§2), by which time the window is
    full again. It also cannot inject fast corrections, which is what a low dead band needs.
    """

    def __init__(
        self, window_s: float = VDO_WINDOW_S, warm_s: float = VDO_WARM_S, jump_s: float = VDO_JUMP_S
    ) -> None:
        self.window_s, self.warm_s, self.jump_s = window_s, warm_s, jump_s
        self.samples: list[tuple[float, float]] = []  # (rx seconds, media - rx)
        self.resets = 0

    def reset(self) -> None:
        self.samples.clear()

    def offset(self) -> float | None:
        """The window's estimate of ``media - rx`` (seconds), or None while empty."""
        if not self.samples:
            return None
        return sum(o for _, o in self.samples) / len(self.samples)

    def add(self, rx_ms: float, media: float) -> bool:
        """Feed one read. False means a discontinuity: the window restarted on this read."""
        rx = float(rx_ms) / 1000.0
        o = float(media) - rx
        cur = self.offset()
        if cur is not None and abs(o - cur) > self.jump_s:
            self.samples = [(rx, o)]
            self.resets += 1
            return False
        self.samples.append((rx, o))
        self.samples = [(r, x) for r, x in self.samples if r > rx - self.window_s]
        return True

    def ready(self) -> bool:
        return (
            len(self.samples) >= VDO_MIN_SAMPLES
            and self.samples[-1][0] - self.samples[0][0] >= self.warm_s
        )

    def at(self, rx: float) -> float | None:
        """Media position at receiver time ``rx`` (seconds), or None while warming up."""
        off = self.offset()
        return None if off is None else rx + off


class Controller:
    """Decides what to tell the two sides, given their samples. Emits nothing by itself.

    The controller is a pure state machine: every method takes the current controller time
    (seconds, monotonic) and the samples it has, and returns :class:`Command` objects. It never
    sleeps, never touches a socket, never reads a clock. The executor applies the commands and
    feeds back samples; ``clock`` estimates come in through :meth:`on_clock`.
    """

    # --- tuning knobs ---------------------------------------------------------------------
    #: the receiver's ``play_at`` target must be in the future by more than one RTT (≤ 40 ms
    #: measured) plus the clock residual; 0.5 s is 10x that, and costs 0.5 s of start delay.
    START_GUARD_S = 0.50
    #: how long to watch the landing before deciding on the single re-landing
    RELAND_AFTER_S = 3.00
    #: how far ahead the re-landing's target instant is put (one RTT + margin)
    RELAND_GUARD_S = 0.50
    #: re-land when the measured landing error is worse than this (the acceptance target)
    RELAND_TOL_S = 0.010
    #: an error sample is only taken once each side has been PLAYING this long
    SAMPLE_AFTER_S = 0.50
    #: proportional gain, 1/s: a pure P term at the dead-band edge asks for
    #: K_P·dead_band = 2.0e-4, two fifths of the RATE_MAX/4 that drift may ever ask for, so
    #: the estimator's own ~2 ms of noise (see EST_WINDOW_S) still fits under the ceiling
    K_P = 0.04
    #: integral gain, 1/s²: K_P²/4 → critically damped (ω = 0.02 rad/s, ~50 s)
    K_I = 0.0004
    #: the integral may never contribute more than this to |rate - 1| (it cancels the
    #: measured drift, 2.07e-5, not the landing error: that is the P term's job)
    INTEGRAL_MAX_U_RATIO = 1.0 / 32.0
    #: the commanded rate may not move faster than this per second (a trim, not a jump)
    SLEW_PER_S = 0.001
    #: do not re-send a trim more often than this: a trim is an event, and `steady()` needs
    #: 2 s of quiet, so the loop must breathe much slower than that
    RATE_MIN_INTERVAL_S = 8.0
    #: ... nor for a change smaller than this (0.02 %), so estimate noise never chatters
    RATE_STEP_MIN = 2e-4
    #: the error is the median of the samples of this window: 8 s of 4 Hz reads bring the
    #: group's σ 10.2 ms down to ~2.3 ms, which is what a 5 ms dead band needs to make sense
    EST_WINDOW_S = 8.0
    #: a read this far from its neighbours is stale, not a movement (A15: outliers ±95-162 ms)
    OUTLIER_S = 0.060
    #: consecutive non-PLAYING reads before a side counts as stopped (A36 hysteresis)
    NP_STREAK = 2
    #: steady() needs the last rate command older than this (§2)
    RATE_QUIET_S = 2.0
    #: steady() needs both sides PLAYING for this long (§2)
    PLAYING_FOR_S = 2.0

    def __init__(
        self,
        delta_v2: float = 0.0,
        rate_max: float = RATE_MAX_DEFAULT,
        dead_band_s: float = RATE_DEAD_BAND_S,
        log=None,
    ) -> None:
        self.delta_v2 = float(delta_v2)
        self.rate_max = float(rate_max)
        self.dead_band_s = float(dead_band_s)
        self.log = log

        self.now = 0.0
        self.phase = PHASE_IDLE
        #: the group's learned landing delay in seconds (positive = it lands *late*, so it is
        #: commanded that much earlier). Set by ``start`` and reused by seek/resume; the
        #: executor persists it across sessions, this module never guesses it.
        self.lead_s = 0.0
        self.clock_offset_ms = 0.0  # receiver clock minus controller clock (§3)
        self.clock_residual_ms: float | None = None

        # event bookkeeping
        self._event_at = float("-inf")  # last transport event (start/seek/resume/pause/end)
        self._landing_at = 0.0  # the instant both sides must be aligned
        self._relanded = False
        #: how often the single re-landing was spent/refused, for the lab note
        self.relandings = 0
        self.relanding_refused = 0

        # samples
        self._last_video: Sample | None = None
        self._last_group: GroupSample | None = None
        self._err: deque[tuple[float, float]] = deque(maxlen=256)
        self._last_raw: float | None = None  # last single-tick error, before filtering
        self._steady_window: list[tuple[float, float]] = []
        self.rejected = 0  # stale reads thrown out by the outlier filter
        #: filtered video playhead (evidence/49: the device reports a sawtooth)
        self.video_clock = VideoClock()
        self._np = {"video": 0, "group": 0}
        self._playing_since: dict[str, float | None] = {"video": None, "group": None}
        self._last_nonplaying: dict[str, float | None] = {"video": None, "group": None}

        # rate actuator
        self._last_tick: float | None = None
        self._integral = 0.0
        self._u_cmd = 0.0  # the last commanded rate - 1
        self._rate_sent = 1.0  # the last rate actually put on the wire
        self._rate_at = float("-inf")
        self._needs_reset = False  # a trim is on the receiver; a landing must undo it

        # telemetry (§5 GET /api/status reads some of this through snapshot())
        self.counts: dict[str, int] = {}
        self.after_steady: dict[str, int] = {}
        self._steady_since: float | None = None

    # -- session lifecycle -------------------------------------------------------------------
    def start(
        self, now: float, video_ready: float, group_ready: float, lead_s: float = 0.0
    ) -> list[Command]:
        """Plan the one start event: park the group, compute the receiver's ``play_at``.

        ``video_ready`` and ``group_ready`` are **media positions**, in seconds, at which the
        two sides sit parked and loaded when this is called (the executor has already done
        ``load``/``prepare``; this module never loads anything). The start position is their
        maximum, so neither side has to move backwards; if that is not where the group sits,
        one ``group_seek`` parks it there — it is part of the same start event, before the
        group's single ``play``, never after.

        ``lead_s`` is the group's measured landing delay: it lands ``lead_s`` *after* the
        instant it is told to play, so it is told ``lead_s`` earlier. The receiver needs no
        lead of its own: ``play_at`` carries the instant it must be at the media position
        (§3.1), so its start latency is inside the receiver.
        """
        now = float(now)
        self.now = now
        self.lead_s = float(lead_s)
        media = max(float(video_ready), float(group_ready))
        land = now + self.START_GUARD_S
        cmds = self._land(land, media, seek_group=float(group_ready) != media)
        self._begin_event(now, land)
        return self._record(cmds)

    def step(self, now: float, video: Sample | None, group: GroupSample | None) -> list[Command]:
        """One control tick. Steady state: rate trims only."""
        now = float(now)
        self.now = now
        if video is None or group is None:
            self._last_tick = now
            return []
        self._track(now, video, group)
        cmds: list[Command] = []
        if self.phase in (PHASE_IDLE, PHASE_ENDED, PHASE_PAUSED):
            self._last_tick = now
            return cmds
        # no sample before the landing instant has passed: both devices announce PLAYING
        # before their playhead is where the plan put it (A4: an unreliable state)
        if self._both_playing(now) and now >= self._landing_at + self.SAMPLE_AFTER_S:
            e_raw = self.error(self._group_pos(now, group), self._video_pos(now, video))
            self._last_raw = e_raw
            self._sample(now, e_raw)
            if self.phase == PHASE_LANDING:
                cmds = self._landing_tick(now)
            elif self.phase == PHASE_SETTLING:
                cmds = self._settling_tick(now)
            elif self.phase == PHASE_STEADY:
                cmds = self._steady_tick(now)
        # The assertion that makes "steady state can only emit rate" a fact, not a promise.
        if self.phase == PHASE_STEADY:
            for c in cmds:
                if c.kind != MSG_RATE:
                    raise AssertionError(
                        f"steady state emitted {c.kind!r}: after the start event the video may "
                        f"only be trimmed, never reloaded (contracts §1)"
                    )
        self._record(cmds)
        self._last_tick = now
        return cmds

    def on_seek(self, now: float, position: float) -> list[Command]:
        """User seek: one re-landing is allowed, then steady again.

        ``position`` is the media instant both sides must reach: the group's target carries the
        calibration offset (``position + DELTA_V2``), the video's is ``position``.
        """
        now = float(now)
        self.now = now
        media = float(position)
        land = now + self.START_GUARD_S
        cmds = self._land(land, media + self.delta_v2, media_video=media, seek_group=True)
        self._begin_event(now, land)
        return self._record(cmds)

    def on_pause(self, now: float) -> list[Command]:
        """Pause both sides. The group goes first: it is the audible one."""
        now = float(now)
        self.now = now
        self.phase = PHASE_PAUSED
        self._event_at = now
        self._playing_since = {"video": None, "group": None}
        self._reset_estimator()
        return self._record([Command(CMD_GROUP_PAUSE), Command(MSG_PAUSE)])

    def on_resume(self, now: float, group_position: float) -> list[Command]:
        """The group leads; the video is told where to be and when (§1).

        ``group_position`` is where the group restarts (media seconds). Both sides are anchored
        at that instant, the video one ``DELTA_V2`` behind the group, and the residual of the
        group's own landing is taken out by the single re-landing.
        """
        now = float(now)
        self.now = now
        media = float(group_position)
        land = now + self.START_GUARD_S
        cmds = self._land(land, media)
        self._begin_event(now, land)
        return self._record(cmds)

    def on_end(self, now: float) -> list[Command]:
        """Media ended naturally: stop both sides (v0.1 did the same, A21)."""
        now = float(now)
        self.now = now
        self.phase = PHASE_ENDED
        self._event_at = now
        self._playing_since = {"video": None, "group": None}
        self._reset_estimator()
        self._integral = 0.0
        self._u_cmd = 0.0
        self._rate_sent = 1.0
        self._needs_reset = False
        return self._record([Command(CMD_GROUP_STOP), Command(MSG_IDLE)])

    # -- observation -------------------------------------------------------------------------
    def error(self, group_pos: float, video_pos: float) -> float:
        """``p_group - p_video - DELTA_V2`` (§2)."""
        return digital_error(group_pos, video_pos, self.delta_v2)

    def steady(self, now: float) -> bool:
        """§2: 30 s since the last transport event, last trim 2 s old, both sides playing."""
        now = float(now)
        if self.phase != PHASE_STEADY:
            return False
        if now - self._event_at < STEADY_AFTER_S:
            return False
        if now - self._rate_at < self.RATE_QUIET_S:
            return False
        for side in ("video", "group"):
            since = self._playing_since[side]
            if since is None or now - since < self.PLAYING_FOR_S:
                return False
            last_bad = self._last_nonplaying[side]
            if last_bad is not None and now - last_bad < self.PLAYING_FOR_S:
                return False
        return True

    def window_median_ms(self) -> float | None:
        """Median digital error of the last 5 s window of steady samples (§2)."""
        if not self._steady_window:
            return None
        low = self.now - STEADY_WINDOW_S
        vals = [e for t, e in self._steady_window if t >= low]
        if len(vals) < 3:
            return None
        return statistics.median(vals) * 1000.0

    def last_raw_error(self) -> float | None:
        """The last single-tick error, before the outlier filter and the median.

        This is the number the contract calls the *raw* error: it carries the group's read
        noise (σ 10.2 ms, A15) and is reported next to the medians, never instead of them.
        """
        return self._last_raw

    def estimated_error(self) -> float | None:
        """The filtered error the law is acting on, in seconds (``None`` = not enough yet)."""
        vals = [e for t, e in self._err if t >= self.now - self.EST_WINDOW_S]
        if len(vals) < 3:
            return None
        return statistics.median(vals)

    def snapshot(self) -> dict:
        """What the owner app needs for ``GET /api/status`` (§5), from the same state."""
        est = self.estimated_error()
        wm = self.window_median_ms()
        return {
            "phase": self.phase,
            "digital_error_ms": None if est is None else round(est * 1000.0, 2),
            "window_median_ms": None if wm is None else round(wm, 2),
            "rate": round(1.0 + self._u_cmd, 6),
            "steady_since": self._steady_since,
            "steady": self.steady(self.now),
            "clock_residual_ms": self.clock_residual_ms,
            "rejected_reads": self.rejected,
            "commands": dict(self.counts),
        }

    def on_clock(self, estimate: ClockEstimate) -> None:
        """Feed the ping/pong clock estimate (§3): window and offset are measured, not assumed."""
        self.clock_offset_ms = float(estimate.offset_ms)
        self.clock_residual_ms = float(estimate.residual_ms)

    # -- the landing -------------------------------------------------------------------------
    def _land(
        self, land: float, media_group: float, media_video: float | None = None, seek_group: bool = False
    ) -> list[Command]:
        """The one start event: park (if needed), release at ``land - lead``, anchor the video.

        ``media_video`` defaults to the invariant's value, ``media_group - DELTA_V2`` (§2); a
        seek passes its own because the user's target is the content instant, not the group's.
        """
        cmds: list[Command] = []
        if self._needs_reset:
            # a trim from the previous regime is still on the receiver: put it back first, so
            # the landing is not computed on a device that is playing 0.2 % fast
            cmds.append(Command(MSG_RATE, {"value": 1.0}))
            self._needs_reset = False
            self._u_cmd = 0.0
            self._rate_sent = 1.0
        if seek_group:
            cmds.append(Command(CMD_GROUP_SEEK, {"media": media_group, "pause": True, "at": self.now}))
        cmds.append(Command(CMD_GROUP_PLAY, {"at": land - self.lead_s, "media": media_group}))
        if media_video is None:
            media_video = media_group - self.delta_v2
        cmds.append(Command(MSG_PLAY_AT, {"rx_ms": self._rx_ms(land), "media": media_video}))
        return cmds

    def _begin_event(self, now: float, land: float) -> None:
        self.phase = PHASE_LANDING
        self._event_at = now
        self._landing_at = land
        self._relanded = False
        self._reset_estimator()

    def _reset_estimator(self) -> None:
        self.video_clock.reset()  # a landing moves the playhead: the envelope is invalid
        self._err.clear()
        self._last_raw = None
        self._steady_window.clear()
        self._integral = 0.0
        self._u_cmd = 0.0
        self._last_tick = self.now

    def _landing_tick(self, now: float) -> list[Command]:
        if now < self._landing_at + self.RELAND_AFTER_S:
            return []
        est = self.estimated_error()
        if est is None:
            return []
        if not self._relanded and abs(est) > self.RELAND_TOL_S:
            video = self._last_video
            if video is None:
                return []
            land = now + self.RELAND_GUARD_S
            # Anchor the video where it *would be* at the target instant, moved by the error it
            # is measured to have: that removes the group's own landing dispersion
            # (evidence/27: σ 30 ms) without touching the group, i.e. with no reload and no gap.
            media_video = self._video_pos(land, video) + est
            self._relanded = True
            self.relandings += 1
            self._landing_at = land
            self._event_at = now
            self._reset_estimator()
            return [Command(MSG_PLAY_AT, {"rx_ms": self._rx_ms(land), "media": media_video})]
        if not self._relanded:
            self.relanding_refused += 1
        self.phase = PHASE_SETTLING
        return []

    def _settling_tick(self, now: float) -> list[Command]:
        if now - self._event_at >= STEADY_AFTER_S:
            self.phase = PHASE_STEADY
            self._steady_since = now
            self._integral = 0.0
        return []

    # -- the steady-state law (rate trims only) ----------------------------------------------
    def _steady_tick(self, now: float) -> list[Command]:
        est = self.estimated_error()
        if est is None:
            return []
        dt = 0.0 if self._last_tick is None else max(0.0, min(now - self._last_tick, 1.0))
        i_max = self.rate_max * self.INTEGRAL_MAX_U_RATIO
        u = 0.0
        if abs(est) > self.dead_band_s:
            self._integral += est * dt
            # anti-windup: the integral alone may never ask for more than I_MAX
            lim = i_max / self.K_I
            self._integral = max(-lim, min(lim, self._integral))
            u = max(-self.rate_max, min(self.rate_max, self.K_P * est + self.K_I * self._integral))
        else:
            # inside the dead band the loop commands neutral; a wound integral must not sit
            # there commanding a rate forever, so it bleeds away slowly (2 min)
            self._integral *= max(0.0, 1.0 - dt / 120.0)
        # bounded slew: the rate walks, it does not jump (2 s from neutral to full scale)
        slew = self.SLEW_PER_S * dt
        u = max(self._u_cmd - slew, min(self._u_cmd + slew, u))
        u = max(-self.rate_max, min(self.rate_max, u))
        self._u_cmd = u
        rate = 1.0 + u
        if now - self._rate_at < self.RATE_MIN_INTERVAL_S:
            return []
        if abs(rate - self._rate_sent) < self.RATE_STEP_MIN:
            return []
        self._rate_sent = rate
        self._rate_at = now
        self._needs_reset = abs(u) > 0.0
        return [Command(MSG_RATE, {"value": round(rate, 6)})]

    # -- sample handling ---------------------------------------------------------------------
    def _track(self, now: float, video: Sample, group: GroupSample) -> None:
        self._last_video, self._last_group = video, group
        if video.player_state == PLAYER_PLAYING and video.media is not None:
            self.video_clock.add(video.rx_ms, video.media)
        for side, state in (("video", video.player_state), ("group", group.player_state)):
            if state == PLAYER_PLAYING:
                self._np[side] = 0
                if self._playing_since[side] is None:
                    self._playing_since[side] = now
            else:
                # A36: a single non-PLAYING read is a stale read, not a stop (that bug cost
                # 1.6 s of silence per bad read). Two in a row are a real stop.
                self._np[side] += 1
                if self._np[side] >= self.NP_STREAK:
                    self._last_nonplaying[side] = now
                    self._playing_since[side] = None

    def _both_playing(self, now: float) -> bool:
        for side in ("video", "group"):
            since = self._playing_since[side]
            if self._np[side] or since is None or now - since < self.SAMPLE_AFTER_S:
                return False
        return True

    def _sample(self, now: float, e_raw: float) -> None:
        if self.phase == PHASE_STEADY and self.steady(now):
            self._steady_window.append((now, e_raw))
            while self._steady_window and self._steady_window[0][0] < now - 2 * STEADY_WINDOW_S:
                self._steady_window.pop(0)
        if len(self._err) >= 3:
            recent = statistics.median(e for _, e in list(self._err)[-3:])
            if abs(e_raw - recent) > self.OUTLIER_S:
                self.rejected += 1  # a stale read, not a movement (A15)
                return
        self._err.append((now, e_raw))

    def _video_pos(self, now: float, video: Sample) -> float:
        """Video position, from the filtered envelope once it has warmed up (evidence/49).

        Raw fallback only while warming up or when the device reports a non-playing state: the
        law must never be fed a sawtooth if a filtered value exists.
        """
        if video.player_state != PLAYER_PLAYING:
            return float(video.media)
        if self.video_clock.ready():
            rx = self._rx_ms(now) / 1000.0
            est = self.video_clock.at(rx)
            if est is not None:
                return est
        t_rx = float(video.rx_ms) / 1000.0 - self.clock_offset_ms / 1000.0
        return float(video.media) + (now - t_rx) * float(video.rate)

    def _group_pos(self, now: float, group: GroupSample) -> float:
        if group.player_state != PLAYER_PLAYING:
            return float(group.media)
        # the group free-runs at nominal 1.0; its own crystal error is what the loop measures
        return float(group.media) + (now - float(group.wall))

    def _rx_ms(self, t: float) -> float:
        return t * 1000.0 + self.clock_offset_ms

    def _record(self, cmds: list[Command]) -> list[Command]:
        """Count what goes out, and refuse anything the law must never emit."""
        now = self.now
        if self._steady_since is not None and now >= self._steady_since:
            for c in cmds:
                self.after_steady[c.kind] = self.after_steady.get(c.kind, 0) + 1
        for c in cmds:
            if c.kind == MSG_LOAD:
                raise AssertionError("the control law never loads: the executor does (§1)")
            if c.kind not in EMITTABLE:
                raise AssertionError(f"unknown command kind {c.kind!r}")
            self.counts[c.kind] = self.counts.get(c.kind, 0) + 1
        return cmds
