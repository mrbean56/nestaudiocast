#!/usr/bin/env python3
"""How noisy is a Chromecast's reported playhead? (review 2026-10-26, consensus §8.5.3)

The control law compares the video playhead with the group's. The group's reads are filtered
(A15: σ 10.2 ms plus isolated ±150 ms spikes); the video's are used raw, and the simulation shows
that is what makes it chase noise: with 10 ms of read noise it takes 36 s to settle instead of 10.
This tool measures the **shape** of that noise on the device, so a filter can be designed against a
measurement rather than a guess.

Method (the shape of `probe/rate_gain.py` / evidence/17, so the numbers compare): poll
`MEDIA_STATUS` at 4 Hz for N seconds, timestamp each read `(t_send + t_recv)/2`, fit `currentTime`
against that timestamp by least squares, report the residual (σ, percentiles, frame-step size) and
a sawtooth score: the fraction of second-to-second moves larger than 20 ms, which is what the
YouTube app's playhead did every 1-1.5 s (A20).

The device's HDMI is out of the TV and the media is a silent test pattern, so this is silent.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, HERE)

import receiver_probe as probe  # noqa: E402

from castv2 import receiver_app  # noqa: E402

DEVICE = "192.168.2.80"
MEDIA_PORT = 7641  # task B's staging static server on CT 121; 7640 is the receiver's frozen URL


def _least_squares(points):
    n = len(points)
    if n < 3:
        return None
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    sxx = sum((t - mt) ** 2 for t, _ in points)
    sxy = sum((t - mt) * (v - mv) for t, v in points)
    slope = sxy / sxx if sxx else 1.0
    intercept = mv - slope * mt
    return slope, [v - (intercept + slope * t) for t, v in points]


def analyse(rows: list[dict]) -> dict:
    rows = [r for r in rows if r.get("media") is not None and r.get("player_state") == "PLAYING"]
    if len(rows) < 10:
        return {"ok": False, "why": f"only {len(rows)} playing samples"}
    start = rows[0]["t"]
    pts = [(r["t"] - start, float(r["media"])) for r in rows]
    fit = _least_squares(pts)
    if not fit:
        return {"ok": False, "why": "not enough samples"}
    slope, resid = fit
    ms = [x * 1000.0 for x in resid]
    absms = sorted(abs(x) for x in ms)
    n = len(ms)
    sigma = statistics.pstdev(ms) if n > 1 else 0.0
    jumps = [
        abs(pts[i][1] - pts[i - 1][1] - slope * (pts[i][0] - pts[i - 1][0])) * 1000.0
        for i in range(1, n)
    ]
    big = sum(1 for j in jumps if j > 20.0)
    frac = big / len(jumps) if jumps else None
    if frac is not None and frac > 0.05 and sigma > 20:
        verdict = "sawtooth-like"
    elif jumps and statistics.median(jumps) > 5:
        verdict = "frame-quantised"
    else:
        verdict = "clean"
    return {
        "ok": True,
        "samples": n,
        "span_s": round(pts[-1][0] - pts[0][0], 2),
        "rate_measured": round(slope, 6),
        "rtt_ms_median": round(statistics.median(r["t1"] - r["t0"] for r in rows) * 1000, 2),
        "residual_ms": {
            "sigma": round(sigma, 2),
            "p50_abs": round(statistics.median(absms), 2),
            "p95_abs": round(absms[int(0.95 * (n - 1))], 2),
            "max_abs": round(absms[-1], 2),
            "min_signed": round(min(ms), 2),
            "max_signed": round(max(ms), 2),
        },
        "frame_step_ms_median": round(statistics.median(jumps), 2) if jumps else None,
        "big_moves_over_20ms": big,
        "big_move_fraction": round(frac, 3) if frac is not None else None,
        "verdict": verdict,
        "worth_for_the_filter": (
            "an upper-envelope filter (v0.1's TvClock, A20) fits a sawtooth; a median fits "
            "outlier spikes; a 4 Hz read of a clean device needs neither"
        ),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--url", default=f"http://192.168.2.21:{MEDIA_PORT}/media/rate-90s.mp4")
    p.add_argument("--seconds", type=float, default=60.0)
    p.add_argument("--hz", type=float, default=4.0)
    p.add_argument("--json", default=None)
    a = p.parse_args(argv)

    dmr = probe.Dmr(DEVICE, log=lambda *_: None)
    dmr.connect()
    app_id, _name, _tr = receiver_app(dmr.link.receiver_status())
    if app_id != "CC1AD845":
        print(
            f"preflight: device app is {app_id} (Backdrop is fine; anything else is not ours)",
            file=sys.stderr,
        )
        if app_id not in (None, "E8C28D3C"):
            dmr.close()
            return 2
    launched = dmr.launch()
    loaded = dmr.load(a.url, "video/mp4")
    if not loaded.get("media_session_id"):
        print(f"load failed: {loaded}", file=sys.stderr)
        dmr.close()
        return 3
    time.sleep(1.5)
    rows = dmr.poll(a.seconds, a.hz)
    dmr.stop()
    dmr.close()

    res = analyse(rows)
    out = {
        "task": "video playhead read noise (review 2026-10-26)",
        "command": " ".join([os.path.basename(sys.argv[0])] + list(argv if argv else sys.argv[1:])),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "device": DEVICE,
        "backend": "platform Default Media Receiver CC1AD845 (not our page)",
        "asset": a.url,
        "hz": a.hz,
        "launch": launched,
        "load": loaded,
        "result": res,
        "rows_sample": rows[:3] + rows[-3:],
        "raw_rows": rows,
    }
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(out, fh, indent=1)
        print(f"saved {a.json}")
    print(json.dumps(res, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
