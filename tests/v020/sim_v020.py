#!/usr/bin/env python3
"""Task F: the control law against **the measured noise** — an offline simulation.

Nothing here opens a socket, launches an app, or makes a sound. Every number this file uses is
traceable to `evidence/`; the parameter list at the top of :class:`Options` gives the source of
each one, and each is overridable from the command line so task C's real receiver measurements
(rate gain, rate stall, start dispersion) can be dropped in without touching this file:

    ./tests/v020/sim_v020.py --json evidence/44-sim.json
    ./tests/v020/sim_v020.py --rate-gain 0.5 --start-dispersion-ms 40

What is simulated
-----------------
Two independent players on one controller clock:

* **the group** (kitchen) — parked, released by a scheduled ``PLAY``, then free-running with its
  own crystal (drift ``-12.4 ± 1.0 ms / 10 min``, `evidence/17`), reads with ``σ 10.2 ms`` and
  isolated stale outliers of ``±95…162 ms`` (`evidence/17`, A15);
* **the receiver** (video) — honours ``play_at{rx_ms, media}`` (self-timed, §3.1), applies
  ``rate`` with a gain (default 1.0: *not measured*, task C) and an optional stall cost
  (default 0), and reads its own clock exactly as §3 says it must.

Landings use the measured dispersions: ``PAUSE→SEEK→PLAY σ 30 ms`` and ``LOAD σ 202 ms``
(`evidence/27`), so the ``load`` case exists to show *why* the park-and-release policy is the
only one v0.2 may use: a LOAD landing is 6.7x more dispersed than the 150 ms perceptual budget
can absorb on the first, uncorrectable landing.

The controller never sees the truth: it gets reads stamped by each device, delayed by a
``0…40 ms`` RTT, with a clock offset it estimates by ping/pong (best of 8, so the residual is at
most half the smallest RTT). ``e_true`` in the output is the physical digital error
(``p_group - p_video - DELTA_V2`` from the true playheads) — that is what the analysis scores
against. ``e_meas`` is what the loop acted on. Never report one as the other.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
import zlib
from dataclasses import dataclass, field, replace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LIB = os.path.join(ROOT, "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from v020.contracts import (  # noqa: E402
    MSG_IDLE, MSG_LOAD, MSG_PAUSE, MSG_PLAY_AT, MSG_RATE, MSG_SEEK, MSG_STOP,
    PLAYER_IDLE, PLAYER_PAUSED, PLAYER_PLAYING, STEADY_WINDOW_S,
    ClockEstimate, GroupSample, Sample,
)
from v020.sync import (  # noqa: E402
    CMD_GROUP_PAUSE, CMD_GROUP_PLAY, CMD_GROUP_SEEK, CMD_GROUP_STOP,
    PHASE_STEADY, Controller,
)

#: the pass criterion of the task: steady-state p95 |e| ≤ 10 ms, on the 5 s window medians
TARGET_P95_MS = 10.0
#: convergence: |5 s window median| ≤ 10 ms, held for 5 s, within 10 s of the event
CONVERGE_S = 10.0
#: the perceptual budget the whole project is built on (notes/ACCEPTANCE.md §4)
PERCEPTUAL_MS = 150.0


# --- parameters ------------------------------------------------------------------------------
@dataclass
class Options:
    """Every knob of the simulation, with the record entry it comes from."""

    # -- session
    duration_s: float = 600.0
    tick_s: float = 0.25                 # §3: the receiver reports at 4 Hz; the loop runs with it
    seed: int = 20261025
    repeats: int = 1
    delta_v2: float = 0.265              # notes/labo-sync.md §2 (v0.1's measured DELTA)
    events: list = field(default_factory=list)   # [(t, "seek:<pos>" | "pause" | "resume" | "end")]

    # -- the group: evidence/17 (A15)
    drift_ms_10min: float = -12.4       # evidence/17, 600 s, 1148 points
    group_noise_ms: float = 10.2        # evidence/17 residual σ
    outlier_rate: float = 0.005         # 4 stale reads in 1148 samples, A15
    outlier_ms: float = 150.0           # A15: +162 / -156 / +114 / -95

    # -- landings: evidence/27 (8 draws each)
    landing_mode: str = "park"          # "park" (PAUSE→SEEK→PLAY) or "load" (LOAD)
    landing_sigma_ms: float = 30.0      # evidence/27 C
    landing_mean_ms: float = -43.6      # evidence/27 C
    load_sigma_ms: float = 202.0        # evidence/27 A
    load_mean_ms: float = -314.7        # evidence/27 A
    lead_error_ms: float = 0.0          # how wrong the *learned* lead is (0 = perfectly learned)

    # -- clocks: contracts §3
    true_offset_ms: float = 1234.5      # arbitrary receiver clock offset
    rtt_max_ms: float = 40.0            # measured RTTs up to 192 ms on WiFi; 40 ms is the norm
    clock_pings: int = 8                # the estimator keeps the best of these
    clock_residual_ms: float | None = None   # force a residual instead of drawing one

    # -- the receiver: task C's unknowns
    rate_gain: float = 1.0              # 0 = the device echoes the rate and ignores it (ev/17)
    rate_stall_ms: float = 0.0          # the playhead freezes this long after a rate change
    start_dispersion_ms: float = 0.0    # play_at landing jitter
    video_noise_ms: float = 0.0         # §3: the receiver timestamps at the same statement

    # -- reads
    stale_rate: float = 0.0             # probability that a read reports a stale PAUSED state (A36)

    def spec(self) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if k != "events" and not k.startswith("_")}


# --- devices ---------------------------------------------------------------------------------
class SimGroup:
    """The kitchen group: parked, released at an instant, then free-running on its own crystal."""

    def __init__(self, opt: Options, rng: random.Random) -> None:
        self.opt = opt
        self.rng = rng
        self.media = 0.0                 # parked position, and the playhead origin
        self.start_at: float | None = None
        self.playing = False
        self.state = PLAYER_IDLE
        self.crystal = 1.0 + opt.drift_ms_10min / 1000.0 / 600.0
        self.calls: list[tuple] = []
        self._prev_read: tuple[float, float, str] | None = None

    # -- transport (the executor's side of the contracts) ----
    def prepare(self, media: float) -> None:
        self.media, self.start_at = float(media), None
        self.playing, self.state = False, PLAYER_PAUSED

    def seek_cmd(self, at: float, media: float, pause: bool = True) -> None:
        self.calls.append(("seek", at, media))
        self.media, self.start_at = float(media), None
        if pause:
            self.playing, self.state = False, PLAYER_PAUSED

    def play_cmd(self, at: float, media: float) -> None:
        """``PLAY`` fired at controller time ``at``; the playhead starts at ``at + landing``."""
        self.calls.append(("play", at, media))
        if float(media) != self.media:
            self.media = float(media)
        self.start_at = at + self._landing()
        self.playing, self.state = True, PLAYER_PLAYING

    def pause_cmd(self, at: float) -> None:
        self.calls.append(("pause", at))
        self.media, self.start_at = self.position(at), None
        self.playing, self.state = False, PLAYER_PAUSED

    def stop_cmd(self, at: float) -> None:
        self.calls.append(("stop", at))
        self.media, self.start_at = 0.0, None
        self.playing, self.state = False, PLAYER_IDLE

    # -- physics ----
    def position(self, t: float) -> float:
        if not self.playing or self.start_at is None or t <= self.start_at:
            return self.media
        return self.media + (t - self.start_at) * self.crystal

    def _landing(self) -> float:
        o = self.opt
        if o.landing_mode == "load":
            return (o.load_mean_ms + self.rng.gauss(0.0, o.load_sigma_ms)) / 1000.0
        return (o.landing_mean_ms + self.rng.gauss(0.0, o.landing_sigma_ms)) / 1000.0

    # -- observation: one read, stamped when it was taken, delivered one RTT later ----
    def read(self, t: float) -> GroupSample:
        rtt = self.rng.uniform(0.0, self.opt.rtt_max_ms) / 1000.0
        t_read = t - rtt
        wall = t_read
        media = self.position(t_read) + self.rng.gauss(0.0, self.opt.group_noise_ms) / 1000.0
        state = self.state
        if self.rng.random() < self.opt.outlier_rate:      # A15: a stale read, ±95…162 ms
            media += (1.0 if self.rng.random() < 0.5 else -1.0) * \
                self.rng.uniform(0.63, 1.08) * self.opt.outlier_ms / 1000.0
        if self.opt.stale_rate and self.rng.random() < self.opt.stale_rate:
            # A36: one read reported a state the group was not in; the sender must need two
            if self._prev_read is not None:
                wall, media, state = self._prev_read[0], self._prev_read[1], PLAYER_PAUSED
            elif state == PLAYER_PLAYING:
                state = PLAYER_PAUSED
        self._prev_read = (wall, media, state)
        return GroupSample(wall=wall, media=media, player_state=state)


class SimReceiver:
    """The Chromecast receiver of §3: self-timed ``play_at``, rate with a gain and a stall cost."""

    def __init__(self, opt: Options, rng: random.Random) -> None:
        self.opt = opt
        self.rng = rng
        self.media = 0.0
        self.rate = 1.0                  # what the device echoes
        self.applied = 1.0               # what it actually plays at (rate_gain)
        self.state = PLAYER_IDLE
        self.playing = False
        self.anchor_t: float | None = None
        self.calls: list[tuple] = []
        self._stall_until = 0.0

    def load(self, media: float = 0.0) -> None:
        self.calls.append(("load", media))
        self.media, self.rate, self.applied = float(media), 1.0, 1.0
        self.state, self.playing, self.anchor_t = PLAYER_PAUSED, False, None

    def play_at(self, t: float, target_t: float, media: float) -> None:
        self.calls.append(("play_at", t, target_t, media))
        jitter = self.opt.start_dispersion_ms / 1000.0
        self.anchor_t = target_t + (self.rng.gauss(0.0, jitter) if jitter else 0.0)
        self.media, self.state, self.playing = float(media), PLAYER_PLAYING, True

    def rate_cmd(self, t: float, value: float) -> None:
        self.calls.append(("rate", t, value))
        # re-anchor at the instant of the change: the new rate applies from here on, it does
        # not retroactively rewrite where the playhead has been (that would be a seek)
        self.media = self.position(t)
        self.anchor_t = t
        self.rate = float(value)
        self.applied = 1.0 + (self.rate - 1.0) * self.opt.rate_gain
        if self.opt.rate_stall_ms:
            self._stall_until = t + self.opt.rate_stall_ms / 1000.0

    def pause_cmd(self, t: float) -> None:
        self.calls.append(("pause", t))
        self.media, self.anchor_t = self.position(t), t
        self.playing, self.state = False, PLAYER_PAUSED

    def stop_cmd(self, t: float) -> None:
        self.calls.append(("stop", t))
        self.media, self.anchor_t = 0.0, None
        self.playing, self.state = False, PLAYER_IDLE

    def position(self, t: float) -> float:
        if not self.playing or self.anchor_t is None:
            return self.media
        dt = t - self.anchor_t
        if dt <= 0.0:                    # before its own target instant it reports behind
            return self.media + dt * self.applied
        stall = 0.0
        if self._stall_until > self.anchor_t:
            stall = min(max(0.0, self._stall_until - self.anchor_t), dt)
        return self.media + (dt - stall) * self.applied

    def read(self, t: float) -> Sample:
        rtt = self.rng.uniform(0.0, self.opt.rtt_max_ms) / 1000.0
        t_read = t - rtt
        media = self.position(t_read)
        if self.opt.video_noise_ms:
            media += self.rng.gauss(0.0, self.opt.video_noise_ms) / 1000.0
        return Sample(rx_ms=t_read * 1000.0 + self.opt.true_offset_ms, media=media,
                      rate=self.applied, player_state=self.state,
                      buffered_end=media + 30.0)


class SimClock:
    """Ping/pong of §3: best of ``clock_pings`` round trips, so a residual ≤ min_rtt / 2."""

    def __init__(self, opt: Options, rng: random.Random) -> None:
        self.opt = opt
        if opt.clock_residual_ms is not None:
            self.residual_ms = float(opt.clock_residual_ms)
            self.rtt_ms = 2.0 * abs(self.residual_ms)
        else:
            rtts = [rng.uniform(0.0, opt.rtt_max_ms) for _ in range(max(1, opt.clock_pings))]
            self.rtt_ms = min(rtts)
            self.residual_ms = rng.uniform(0.0, self.rtt_ms / 2.0)
        self.offset_ms = opt.true_offset_ms + self.residual_ms

    def estimate(self, t: float) -> ClockEstimate:
        return ClockEstimate(offset_ms=self.offset_ms, residual_ms=self.residual_ms,
                             rtt_ms=self.rtt_ms, samples=self.opt.clock_pings, measured_at=t)


# --- one run ---------------------------------------------------------------------------------

def run_once(opt: Options, name: str, rep: int = 0, collect: bool = True) -> dict:
    """One deterministic run. Returns the trajectory, the command log and the devices."""
    seed = opt.seed ^ zlib.crc32(f"{name}:{rep}".encode())
    rng = random.Random(seed)
    clock = SimClock(opt, rng)
    group = SimGroup(opt, rng)
    recv = SimReceiver(opt, rng)
    ctl = Controller(delta_v2=opt.delta_v2)
    ctl.on_clock(clock.estimate(0.0))

    learned_lead = (opt.landing_mean_ms + opt.lead_error_ms) / 1000.0
    if opt.landing_mode == "load":       # the controller still believes the park landing
        learned_lead = (opt.landing_mean_ms + opt.lead_error_ms) / 1000.0

    events = sorted(opt.events)
    pending: list[tuple[float, dict]] = []
    command_log: list[tuple[float, str, dict]] = []
    traj: list[dict] = []
    landings: list[dict] = []
    event_times: list[float] = [0.0]

    def fire(t: float, cmds: list, source: str = "loop") -> None:
        for c in cmds:
            command_log.append((t, c.kind, dict(c.payload), source,
                                ctl.phase if source == "loop" else None))
            if c.kind == CMD_GROUP_PLAY:
                pending.append((float(c.payload["at"]), c.payload))
            elif c.kind == CMD_GROUP_SEEK:
                group.seek_cmd(t, c.payload["media"], bool(c.payload.get("pause", False)))
            elif c.kind == CMD_GROUP_PAUSE:
                group.pause_cmd(t)
            elif c.kind == CMD_GROUP_STOP:
                group.stop_cmd(t)
            elif c.kind == MSG_PLAY_AT:
                target = (float(c.payload["rx_ms"]) - opt.true_offset_ms) / 1000.0
                recv.play_at(t, target, c.payload["media"])
                landings.append({"at": target, "media": c.payload["media"], "t_cmd": t})
            elif c.kind == MSG_RATE:
                recv.rate_cmd(t, c.payload["value"])
            elif c.kind == MSG_PAUSE:
                recv.pause_cmd(t)
            elif c.kind in (MSG_IDLE, MSG_STOP):
                recv.stop_cmd(t)
            elif c.kind == MSG_LOAD:
                raise AssertionError("the controller emitted load — it never may (§1)")

    group.prepare(0.0)
    recv.load(0.0)
    fire(0.0, ctl.start(0.0, video_ready=recv.media, group_ready=group.media,
                        lead_s=learned_lead), source="event")

    t = 0.0
    while t <= opt.duration_s + 1e-9:
        # scheduled group releases, fired at their exact instant (the executor's job)
        due = sorted([p for p in pending if p[0] <= t + 1e-12], key=lambda p: p[0])
        for at, payload in due:
            pending.remove((at, payload))
            group.play_cmd(at, payload["media"])
        # user / transport events
        while events and events[0][0] <= t + 1e-12:
            at, action = events.pop(0)
            event_times.append(at)
            if action.startswith("seek:"):
                cmds = ctl.on_seek(at, float(action.split(":", 1)[1]))
            elif action == "pause":
                cmds = ctl.on_pause(at)
            elif action == "resume":
                cmds = ctl.on_resume(at, group.position(at))
            elif action == "end":
                cmds = ctl.on_end(at)
            else:
                raise ValueError(action)
            fire(at, cmds, source="event")
        gsample = group.read(t)
        vsample = recv.read(t)
        fire(t, ctl.step(t, vsample, gsample))
        if collect:
            snap = ctl.snapshot()
            traj.append({
                "t": round(t, 4),
                "e_true_ms": round((group.position(t) - recv.position(t) - opt.delta_v2)
                                   * 1000.0, 3),
                "e_meas_ms": snap["digital_error_ms"],
                "e_raw_ms": None if ctl.last_raw_error() is None
                            else round(ctl.last_raw_error() * 1000.0, 3),
                "phase": snap["phase"],
                "steady": snap["steady"],
                "rate": snap["rate"],
                "rate_cmd": round(ctl._u_cmd + 1.0, 6),
                "video": round(recv.position(t), 4),
                "group": round(group.position(t), 4),
            })
        t = round(t + opt.tick_s, 9)

    return {"traj": traj, "commands": command_log, "landings": landings,
            "events": event_times, "ctl": ctl, "group": group, "recv": recv,
            "clock": clock, "seed": seed}


# --- analysis --------------------------------------------------------------------------------
def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def _window_medians(traj: list[dict], key: str = "e_true_ms",
                    steady_only: bool = True) -> list[float]:
    """Median of the error per 5 s window — the contract's criterion (§2)."""
    buckets: dict[int, list[float]] = {}
    for row in traj:
        if steady_only and not row["steady"]:
            continue
        if row[key] is None:
            continue
        buckets.setdefault(int(row["t"] // STEADY_WINDOW_S), []).append(row[key])
    out = []
    for k in sorted(buckets):
        vals = buckets[k]
        if len(vals) >= 4:               # a 4 Hz tick gives ~20; fewer is not a window
            out.append(statistics.median(vals))
    return out


def _convergence_s(traj: list[dict], event_t: float, tol_ms: float = TARGET_P95_MS,
                   hold_s: float = STEADY_WINDOW_S) -> float | None:
    """First instant after ``event_t`` from which |1 s median| stays ≤ tol for ``hold_s``."""
    if not traj:
        return None
    buckets: dict[int, list[float]] = {}
    for row in traj:
        if row["t"] < event_t:
            continue
        buckets.setdefault(int(row["t"]), []).append(row["e_true_ms"])
    keys = sorted(buckets)
    meds = {k: statistics.median(buckets[k]) for k in keys}
    for k in keys:
        span = [k2 for k2 in keys if k <= k2 < k + hold_s + 1]
        if len(span) < max(2, int(hold_s)):      # the window must be complete
            continue
        if all(abs(meds[k2]) <= tol_ms for k2 in span):
            return round(k - event_t, 2)
    return None


def analyze(run: dict, opt: Options) -> dict:
    traj, ctl, cmds = run["traj"], run["ctl"], run["commands"]
    wm = _window_medians(traj, "e_true_ms")
    wm_meas = _window_medians(traj, "e_meas_ms")
    raw = [abs(r["e_true_ms"]) for r in traj if r["steady"]]
    reads = [abs(r["e_raw_ms"]) for r in traj if r["steady"] and r["e_raw_ms"] is not None]
    meas = [abs(r["e_meas_ms"]) for r in traj if r["steady"] and r["e_meas_ms"] is not None]

    steady_at = next((r["t"] for r in traj if r["steady"]), None)
    after = {}
    for t, kind, payload, source, phase in cmds:
        # a user event may move what the user asked to move; what matters is that the *loop*,
        # while it says it is steady, never reloads anything (contracts §1, §2)
        if source != "loop" or phase != PHASE_STEADY:
            continue
        after[kind] = after.get(kind, 0) + 1

    rates = [(t, abs(c["value"] - 1.0)) for t, k, c, _, _ in cmds if k == MSG_RATE]
    settle_at = None if steady_at is None else steady_at + 30.0
    rates_steady = [(r["t"], abs(r["rate_cmd"] - 1.0)) for r in traj if r["steady"]]
    rates_settled = [u for t, u in rates_steady if settle_at is not None and t >= settle_at]

    conv = {}
    for et in run["events"]:
        conv[str(et)] = _convergence_s(traj, et)
    conv_vals = [v for v in conv.values() if v is not None]
    # a null convergence is the honest worst case: report it as "never"
    conv_max = max(conv_vals) if conv_vals else None

    landing_err = None
    reland_err = None
    if run["landings"]:
        first = run["landings"][0]["at"]
        near = [r["e_true_ms"] for r in traj if first <= r["t"] <= first + 1.0]
        if near:
            landing_err = round(statistics.median(near), 2)
        if len(run["landings"]) > 1:
            second = run["landings"][1]["at"]
            near2 = [r["e_true_ms"] for r in traj if second + 0.5 <= r["t"] <= second + 2.0]
            if near2:
                reland_err = round(statistics.median(near2), 2)

    return {
        "duration_s": opt.duration_s,
        "steady_samples": len(raw),
        "steady_from_s": steady_at,
        "window_medians_ms": {"n": len(wm), "p50": _pct([abs(x) for x in wm], 0.50),
                              "p95": _pct([abs(x) for x in wm], 0.95),
                              "max_abs": round(max((abs(x) for x in wm), default=0.0), 2),
                              "mean_signed": round(statistics.fmean(wm), 2) if wm else None},
        "window_medians_estimate_ms": {
            "n": len(wm_meas),
            "p50": _pct([abs(x) for x in wm_meas], 0.50),
            "p95": _pct([abs(x) for x in wm_meas], 0.95),
            "max_abs": round(max((abs(x) for x in wm_meas), default=0.0), 2),
        },
        # e_true: the physical digital error (what the loop achieved)
        "raw_p95_abs_ms": _pct(raw, 0.95),
        "raw_max_abs_ms": round(max(raw), 2) if raw else None,
        # a single read: carries the group's σ 10.2 ms and its stale outliers (A15)
        "read_p95_abs_ms": _pct(reads, 0.95),
        "read_max_abs_ms": round(max(reads), 2) if reads else None,
        # the filtered estimate the law actually acts on
        "estimate_p95_abs_ms": _pct(meas, 0.95),
        "convergence_s": conv,
        "convergence_max_s": conv_max,
        "rate": {"n": len(rates),
                 "max_abs_u": round(max(u for _, u in rates), 6) if rates else 0.0,
                 "max_abs_u_steady": (round(max(u for _, u in rates_steady), 6)
                                      if rates_steady else 0.0),
                 "max_abs_u_settled": (round(max(rates_settled), 6)
                                       if rates_settled else 0.0),
                 "distinct": len({round(u, 6) for _, u in rates}),
                 "rate_max": ctl.rate_max},
        "commands": dict(ctl.counts),
        "after_steady": after,
        "relandings": ctl.relandings,
        "relanding_refused": ctl.relanding_refused,
        "rejected_reads": ctl.rejected,
        "first_landing_error_ms": landing_err,
        "after_relanding_error_ms": reland_err,
        "clock_residual_ms": round(run["clock"].residual_ms, 2),
    }


def verdict(stats: dict, opt: Options, expect_pass: bool = True) -> dict:
    """The task's 'done when' criteria, checked, not eyeballed."""
    if "window_medians_ms" not in stats:
        # a landing-distribution case: the law's own criterion is the post-re-landing error;
        # the first-landing numbers are reported next to it, they are the point of the case.
        reland = (stats.get("after_relanding_ms") or {}).get("p95")
        checks = {"after_relanding_p95_le_10ms": reland is not None and reland <= TARGET_P95_MS}
        return {"expected": "informational", "ok": all(checks.values()), "checks": checks,
                "verdict": "INFO (landing distribution)"}
    checks: dict[str, bool] = {}
    checks["steady_p95_le_10ms"] = (stats["window_medians_ms"]["p95"] or 1e9) <= TARGET_P95_MS
    reloads = {k: v for k, v in stats["after_steady"].items()
               if k in (MSG_LOAD, MSG_SEEK, CMD_GROUP_SEEK)}
    checks["no_load_seek_after_steady"] = not reloads
    checks["no_play_at_after_steady"] = MSG_PLAY_AT not in stats["after_steady"]
    checks["convergence_le_10s"] = (stats["convergence_max_s"] is not None
                                    and stats["convergence_max_s"] <= CONVERGE_S)
    checks["drift_trim_le_rate_max_quarter"] = (
        stats["rate"]["max_abs_u_settled"] <= stats["rate"]["rate_max"] / 4.0 + 1e-12)
    wanted = getattr(opt, "_checks", None) or ALL_CHECKS
    checks = {k: v for k, v in checks.items() if k in wanted}
    ok = all(checks.values())
    if expect_pass:
        label = "PASS" if ok else "FAIL"
    else:
        label = "FAIL (expected)" if not ok else "PASS (unexpected)"
    return {"expected": "pass" if expect_pass else "fail", "ok": ok, "checks": checks,
            "verdict": label}


# --- the cases -------------------------------------------------------------------------------
#: which of the 'done when' criteria each case must satisfy. The drift criterion is the
#: drift-only case's own criterion: everywhere else a trim may legitimately be closing a
#: landing residual, which is the P term's job, not drift's.
ALL_CHECKS = ("steady_p95_le_10ms", "no_load_seek_after_steady", "no_play_at_after_steady",
              "convergence_le_10s")
DRIFT_CHECK = "drift_trim_le_rate_max_quarter"

CASES: dict[str, dict] = {
    # nominal: one start event, then ten minutes of drift and read noise
    "cold_start": dict(duration_s=600.0),
    # the same session with no events at all: does drift alone ever ask for a big trim?
    "drift_only": dict(duration_s=900.0, checks=ALL_CHECKS + (DRIFT_CHECK,)),
    # a user seek mirrored on the group, then six quiet minutes
    "seek": dict(duration_s=480.0, events=[(120.0, "seek:300")]),
    # pause, wait 20 s, resume: the group is the reference
    "pause_resume": dict(duration_s=420.0, events=[(120.0, "pause"), (140.0, "resume")]),
    # a clock estimate that is wrong by 12 ms: the loop can be no better than its clock
    "clock_residual": dict(duration_s=600.0, clock_residual_ms=12.0),
    # 2 % of the reads report a stale PAUSED state (A36): no reload may follow
    "stale_reads": dict(duration_s=420.0, stale_rate=0.02),
    # why LOAD may never be used to start: 40 draws of each landing policy
    "landing_park": dict(duration_s=20.0, repeats=40),
    "landing_load": dict(duration_s=20.0, repeats=40, landing_mode="load"),
    # the pessimistic case: task C measures a rate gain of 0.5 and 40 ms start dispersion
    "pessimistic": dict(duration_s=300.0, events=[(60.0, "seek:200")],
                        rate_gain=0.5, start_dispersion_ms=40.0),
    # task C measures a rate gain of 0 (the v0.1 finding, on our own receiver): nothing the
    # law can do in steady state, and drift alone walks past 10 ms — the actuator is mandatory
    "rate_dead": dict(duration_s=600.0, rate_gain=0.0),
    # a lead that is wrong by 200 ms: the single re-landing must absorb it, the session holds
    "wrong_lead": dict(duration_s=300.0, lead_error_ms=200.0),
}

#: cases whose *failure* is the deliverable (the sim must report it, not hide it)
EXPECT_FAIL = {"pessimistic", "clock_residual"}

#: what each case is worth, in one line, next to its numbers in the evidence file
NOTES = {
    "cold_start": "nominal: one start event, ten minutes, drift and read noise as measured",
    "drift_only": "the drift criterion's own case: |rate-1| must never exceed RATE_MAX/4",
    "seek": "a mirrored user seek at 120 s, then six quiet minutes",
    "pause_resume": "pause 20 s, resume: the group is the reference, the video is placed there",
    "clock_residual": "a 12 ms clock estimate error: the loop is no better than its clock",
    "stale_reads": "2 % of reads report a stale PAUSED state (A36): no reload may follow",
    "landing_park": "PAUSE->SEEK->PLAY, 40 draws: what the first landing costs",
    "landing_load": "LOAD, 40 draws: why a LOAD start may never be used",
    "pessimistic": "rate gain 0.5 + 40 ms start dispersion: must fail, with its number",
    "rate_dead": "rate gain 0 (v0.1's finding on our own receiver): the margin without a trim",
    "wrong_lead": "a lead learned 200 ms wrong: the single re-landing absorbs it",
}


def options_for(name: str, **overrides) -> Options:
    """The case's own parameters with any CLI/tests override applied."""
    case = dict(CASES[name])
    wanted = case.pop("checks", None)
    base = Options(**case)
    opt = replace(base, **overrides)
    opt.events = list(base.events)
    if wanted:
        opt._checks = tuple(wanted)
    return opt


def run_case(name: str, opt: Options) -> dict:
    stats_all = []
    for rep in range(opt.repeats):
        run = run_once(opt, name, rep)
        stats_all.append(analyze(run, opt))
    if opt.repeats == 1:
        out = stats_all[0]
    else:
        landings = [s["first_landing_error_ms"] for s in stats_all
                    if s["first_landing_error_ms"] is not None]
        relands = [s["after_relanding_error_ms"] for s in stats_all
                   if s["after_relanding_error_ms"] is not None]
        out = {
            "repeats": opt.repeats,
            "duration_s": opt.duration_s,
            "landing_ms": {"n": len(landings),
                           "p50": _pct([abs(x) for x in landings], 0.50),
                           "p95": _pct([abs(x) for x in landings], 0.95),
                           "max_abs": round(max((abs(x) for x in landings), default=0.0), 2),
                           "over_150ms_pct": round(
                               100.0 * sum(1 for x in landings if abs(x) > PERCEPTUAL_MS)
                               / len(landings), 1) if landings else None},
            "after_relanding_ms": {"n": len(relands),
                                   "p50": _pct([abs(x) for x in relands], 0.50),
                                   "p95": _pct([abs(x) for x in relands], 0.95)},
            "rate": stats_all[0]["rate"],
            "commands": stats_all[0]["commands"],
            "after_steady": stats_all[0]["after_steady"],
        }
    out["params"] = opt.spec()
    out["note"] = NOTES.get(name, "")
    out["verdict"] = verdict(out, opt, expect_pass=name not in EXPECT_FAIL)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", metavar="PATH", help="write the results here (evidence/44-sim.json)")
    ap.add_argument("--case", action="append", default=None,
                    help=f"run only these cases (repeatable): {', '.join(CASES)}")
    ap.add_argument("--duration", type=float, default=None, help="override the case duration")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--tick", type=float, default=None, help="control tick, seconds")
    ap.add_argument("--delta", type=float, default=None, help="DELTA_V2, media seconds")
    ap.add_argument("--drift-ms-10min", type=float, default=None)
    ap.add_argument("--group-noise-ms", type=float, default=None)
    ap.add_argument("--outlier-rate", type=float, default=None)
    ap.add_argument("--outlier-ms", type=float, default=None)
    ap.add_argument("--landing-mode", choices=("park", "load"), default=None)
    ap.add_argument("--landing-sigma-ms", type=float, default=None)
    ap.add_argument("--landing-mean-ms", type=float, default=None)
    ap.add_argument("--load-sigma-ms", type=float, default=None)
    ap.add_argument("--load-mean-ms", type=float, default=None)
    ap.add_argument("--lead-error-ms", type=float, default=None)
    ap.add_argument("--rtt-max-ms", type=float, default=None)
    ap.add_argument("--clock-pings", type=int, default=None)
    ap.add_argument("--clock-residual-ms", type=float, default=None)
    ap.add_argument("--rate-gain", type=float, default=None)
    ap.add_argument("--rate-stall-ms", type=float, default=None)
    ap.add_argument("--start-dispersion-ms", type=float, default=None)
    ap.add_argument("--video-noise-ms", type=float, default=None)
    ap.add_argument("--stale-rate", type=float, default=None)
    ap.add_argument("--repeats", type=int, default=None)
    ap.add_argument("--quiet", action="store_true", help="no per-case table")
    a = ap.parse_args(argv)

    overrides = {
        "duration_s": a.duration, "seed": a.seed, "tick_s": a.tick, "delta_v2": a.delta,
        "drift_ms_10min": a.drift_ms_10min, "group_noise_ms": a.group_noise_ms,
        "outlier_rate": a.outlier_rate, "outlier_ms": a.outlier_ms,
        "landing_mode": a.landing_mode, "landing_sigma_ms": a.landing_sigma_ms,
        "landing_mean_ms": a.landing_mean_ms, "load_sigma_ms": a.load_sigma_ms,
        "load_mean_ms": a.load_mean_ms, "lead_error_ms": a.lead_error_ms,
        "rtt_max_ms": a.rtt_max_ms, "clock_pings": a.clock_pings,
        "clock_residual_ms": a.clock_residual_ms, "rate_gain": a.rate_gain,
        "rate_stall_ms": a.rate_stall_ms, "start_dispersion_ms": a.start_dispersion_ms,
        "video_noise_ms": a.video_noise_ms, "stale_rate": a.stale_rate,
        "repeats": a.repeats,
    }
    overrides = {k: v for k, v in overrides.items() if v is not None}

    names = a.case or list(CASES)
    results: dict[str, dict] = {}
    for name in names:
        if name not in CASES:
            print(f"unknown case {name!r}; known: {', '.join(CASES)}", file=sys.stderr)
            return 2
        opt = options_for(name, **overrides)
        results[name] = run_case(name, opt)
        if not a.quiet:
            v = results[name]["verdict"]
            if "window_medians_ms" in results[name]:
                wm = results[name]["window_medians_ms"]
                rp95 = _fmt(results[name]["raw_p95_abs_ms"])
                print(f"{name:14s} {v['verdict']:20s} "
                      f"p95(5s med)={_fmt(wm['p95'])}ms  e_true p95={rp95}ms"
                      f"  conv={results[name]['convergence_max_s']}s"
                      f"  |u|max={results[name]['rate']['max_abs_u']:.5f}"
                      f"  rates={results[name]['rate']['n']}"
                      f"  after={results[name]['after_steady']}")
            else:
                lm = results[name]["landing_ms"]
                print(f"{name:14s} {v['verdict']:20s} landing p50={_fmt(lm['p50'])}ms "
                      f"p95={_fmt(lm['p95'])}ms max={_fmt(lm['max_abs'])}ms "
                      f">{PERCEPTUAL_MS:.0f}ms={lm['over_150ms_pct']}%")
            failed = [k for k, ok in v["checks"].items() if not ok]
            if failed:
                print(f"{'':14s} failed criteria: {', '.join(failed)}")

    doc = {
        "task": "F — the control law, proved in simulation",
        "command": " ".join(["tests/v020/sim_v020.py"]
                            + (argv if argv is not None else sys.argv[1:])),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": os.uname().nodename,
        "python": sys.version.split()[0],
        "sources": {
            "group drift -12.4±1.0 ms/10min": "evidence/17-rate-gain-and-drift.json",
            "group read σ 10.2 ms, stale outliers ±95…162 ms": "evidence/17, notes/00-labo A15",
            "PAUSE→SEEK→PLAY landing σ 30 ms, LOAD σ 202 ms":
                "evidence/27-actuator-repeatability.json",
            "perceptual budget 150 ms": "notes/ACCEPTANCE.md §4",
            "rate gain / stall / start dispersion": "unmeasured here: task C, defaults 1.0 / 0 / 0",
        },
        "cases": results,
    }
    if a.json:
        path = a.json if os.path.isabs(a.json) else os.path.join(ROOT, a.json)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(doc, fh, indent=2, sort_keys=False)
            fh.write("\n")
        print(f"\nsaved {os.path.relpath(path, ROOT)}")
    return 0


def _fmt(v) -> str:
    return "—" if v is None else f"{v:.1f}"


if __name__ == "__main__":
    sys.exit(main())
