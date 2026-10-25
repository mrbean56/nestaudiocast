#!/usr/bin/env python3
"""Closed-loop test of the relay controller against a simulated sink.

Purpose: prove the *control law* before it is ever pointed at a real group. The
simulated sink reproduces the numbers we actually measured on the hardware
(evidence/07, 08): ~0.4 s start latency, ~0.5 s seek latency, ±90 ms seek
dispersion, honouring of SET_PLAYBACK_RATE, and a playhead that runs on its own
crystal.

Scenarios, and what each one actually proves (cross-review §1 A8 corrected the labels):

  S1 "realistic" — 91 ppm. Over 72 s that is 6.6 ms, i.e. BELOW the 30 ms deadband: this case
      exercises the *events* (seek, pause, content switch, stop, new session), NOT drift.
  S2 stress      — 3000 ppm, ~55x the measured value. This is the only case that exercises
      drift. Note: on the real hardware the fine actuator does not exist (PLAYBACK_RATE is a
      verified no-op, evidence/17), so S2 demonstrates what the *control law* could do, not
      what the device does. Real drift over 600 s was measured at -12 ms/10 min, so drift is
      not actually the problem this PoC has to solve.
  S3 delta       — shows the loop happily holds a *wrong* delta, which is why the acoustic
      calibration is mandatory. It does NOT prove delta is tracked.

Events exercised: initial start, mid-session seek, pause, resume, content switch
(ad-like), stop, and a brand-new session.
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, HERE)

from cast_relay import Relay  # noqa: E402

START_LATENCY = 0.60   # matches the relay's START_LEAD_S; measured range 0.54-0.65
SEEK_LATENCY = 0.50
SEEK_DISPERSION = 0.060   # sigma, real measurement showed ±90 ms spread
READ_NOISE = 0.008


class NullSink:
    """Simulated Default Media Receiver with realistic (measured) behaviour.

    `start_latency` is the device's own LOAD->audio delay. Setting it different from the
    relay's START_LEAD_S models a latency mismatch, which is exactly what the PoC cannot
    correct for: there is no fine actuator on the real hardware (evidence/17).
    """

    def __init__(self, drift_ppm=0.0, seed=7, start_latency=None):
        self.start_latency = START_LATENCY if start_latency is None else start_latency
        self.drift = 1.0 + drift_ppm / 1e6
        self.rng = random.Random(seed)
        self.state = "IDLE"
        self.rate = 1.0
        self._t = 0.0            # playhead at _anchor_wall
        self._anchor = None
        self._pending_start = None
        self._pending_seek = None
        self.content = None

    # --- media lifetime
    def start(self, url, ctype, at_time, content_id=None):
        self.content = url
        self.state = "BUFFERING"
        self._pending_start = (time.time() + self.start_latency, max(0.0, at_time))
        self._pending_seek = None
        self.rate = 1.0
        return {"playerState": "BUFFERING", "currentTime": at_time}

    def seek(self, t):
        if self.state in ("IDLE", "BUFFERING"):
            self._pending_start = (time.time() + self.start_latency, t)
            return
        self._pending_seek = (time.time() + SEEK_LATENCY, t)
        self.state = "BUFFERING"

    def set_rate(self, r):
        self._advance()
        self.rate = r

    def pause(self):
        self._advance()
        self.state = "PAUSED"

    def play(self):
        self._anchor = time.time()
        self.state = "PLAYING"

    def stop(self):
        self.state = "IDLE"
        self._anchor = None

    def close(self):
        pass

    # --- clock
    def _advance(self):
        now = time.time()
        if self.state == "BUFFERING":
            if self._pending_seek and now >= self._pending_seek[0]:
                self._t = self._pending_seek[1] + self.rng.gauss(0, SEEK_DISPERSION)
                self._pending_seek = None
                self._anchor = now
                self.state = "PLAYING"
            elif self._pending_start and now >= self._pending_start[0]:
                self._t = self._pending_start[1]
                self._pending_start = None
                self._anchor = now
                self.state = "PLAYING"
        elif self.state == "PLAYING" and self._anchor is not None:
            self._t += (now - self._anchor) * self.rate * self.drift
            self._anchor = now

    def snapshot(self):
        self._advance()
        t = self._t
        if self.state == "PLAYING":
            t += (time.time() - self._anchor) * self.rate * self.drift
        return {"wall": time.time(), "t": round(t, 6) + self.rng.gauss(0, READ_NOISE),
                "state": self.state, "rate": self.rate}


class SyntheticTv:
    """Scripted TV session: a list of (at_seconds, action)."""

    def __init__(self, script, content="dQw4w9WgXcQ", content_b="aqz-KE-bpKQ"):
        self.script = sorted(script)
        self.t0 = time.time()
        self.state = "IDLE"
        self.t = 0.0
        self.anchor = None
        self.content = content
        self.content_b = content_b
        self._i = 0

    def _set_playhead(self, t):
        self.t = t
        self.anchor = time.time()

    def snapshot(self):
        now = time.time()
        while self._i < len(self.script) and now - self.t0 >= self.script[self._i][0]:
            _, action = self.script[self._i]
            self._i += 1
            if action == "play":
                self.state, self.anchor = "PLAYING", now
            elif action == "pause":
                self.t = self.t + (now - self.anchor) if self.anchor else self.t
                self.state, self.anchor = "PAUSED", now
            elif action.startswith("seek:"):
                self._set_playhead(float(action.split(":")[1]))
            elif action == "switch":
                self.content = self.content_b
                self._set_playhead(0.0)
            elif action == "switch_back":
                self.content, _ = self.content, None
                self._set_playhead(0.0)
            elif action == "stop":
                self.state, self.anchor = "IDLE", None
            elif action == "newsession":
                self.content = "M7lc1UVf-VE"
                self._set_playhead(0.0)
                self.state, self.anchor = "PLAYING", now
        t = self.t
        if self.state == "PLAYING" and self.anchor:
            t += now - self.anchor
        return {"wall": now, "state": self.state, "t": round(t, 4) if self.state != "IDLE" else None,
                "content": self.content, "app": "YouTube", "app_id": "233637DE"}


class FakeAudio:
    def url_for(self, vid):
        return (f"http://192.168.2.18:8099/yt/{vid}.m4a", "audio/mp4")

    def fetch_async(self, vid):
        pass


def run_case(name, drift_ppm, script, duration, delta=0.0, start_latency=None):
    sink = NullSink(drift_ppm, start_latency=start_latency)
    tv = SyntheticTv(script)
    log = os.path.join(ROOT, "evidence", "sim-%s.jsonl" % name)
    open(log, "w").close()
    relay = Relay(tv, sink, FakeAudio(), delta=delta, poll=0.25,
                  log_path=log, verbose=False)
    t_end = time.time() + duration
    while time.time() < t_end:
        relay.step()
        time.sleep(0.25)
    return {"name": name, "drift_ppm": drift_ppm, "duration": duration,
            "delta": delta, "stats": relay.stats, "log": log}


def analyze(log_path, delta=0.0, skip_after_event=3.0):
    """Error statistics of the in-sync regime, ignoring the settling right after an event."""
    rows = [json.loads(l) for l in open(log_path)]
    ev_times = [r["wall"] for r in rows if r.get("ev") in
                ("content_changed", "sink_started", "tv_not_playing") and "wall" in r]
    errs = []
    for r in rows:
        if r.get("ev") != "sample" or r.get("err_ms") is None:
            continue
        # a sample taken while the *user* seeks the video is an event, not a steady-state
        # error (it reached -585 s in S1); "outlier_ignored" samples are stale reads.
        if r.get("action") in ("seek", "dry_run", "outlier_ignored", "realign"):
            continue
        if abs(r["err_ms"]) > 1000:
            continue
        if any(0 <= r["wall"] - t < skip_after_event for t in ev_times):
            continue
        errs.append(r["err_ms"])
    if not errs:
        return {"n": 0}
    errs_sorted = sorted(errs)
    return {
        "n": len(errs),
        "mean_ms": round(sum(errs) / len(errs), 1),
        "p95_abs_ms": round(errs_sorted[int(0.95 * (len(errs) - 1))], 1),
        "max_abs_ms": round(max(abs(e) for e in errs), 1),
        "within_50ms_pct": round(100 * sum(1 for e in errs if abs(e) <= 50) / len(errs), 1),
        "within_150ms_pct": round(100 * sum(1 for e in errs if abs(e) <= 150) / len(errs), 1),
    }


def main():
    scenarios = [
        ("S1-realistic-91ppm",
         91,
         [(0, "play"),
          (15, "seek:600"),      # user scrubs forward 10 min
          (28, "pause"), (34, "play"),
          (42, "switch"),        # content change (ad-like)
          (52, "switch_back"),
          (60, "stop"), (63, "newsession")],
         72),
        ("S2-stress-law-only-3000ppm", 3000, [(0, "play")], 60),
        ("S3-delta200ms", 91, [(0, "play")], 45, 0.200),
        # the device starts 100 ms later than the configured lead: nothing can trim this
        # without PLAYBACK_RATE, which the hardware ignores. This is the honest limit.
        ("S4-latency-mismatch-100ms", 91, [(0, "play")], 45, 0.0, 0.50),
    ]
    out = []
    for case in scenarios:
        name, ppm, script, dur = case[0], case[1], case[2], case[3]
        delta = case[4] if len(case) > 4 else 0.0
        slat = case[5] if len(case) > 5 else None
        print(f"\n=== {name} ({dur}s, drift {ppm} ppm, delta {delta:+.3f}s)")
        r = run_case(name, ppm, script, dur, delta, slat)
        r["analysis"] = analyze(r["log"], delta)
        out.append(r)
        print(json.dumps(r["stats"]), "\n", json.dumps(r["analysis"]))
    json.dump(out, open(os.path.join(ROOT, "evidence", "13-controller-sim.json"), "w"), indent=2)
    print("\nsaved evidence/13-controller-sim.json")


if __name__ == "__main__":
    main()
