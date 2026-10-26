#!/usr/bin/env python3
"""Task C's remaining measurements, on **our** receiver page (review 2026-10-26).

The first night could only measure the platform's Default Media Receiver, because
`28028CD2` was `APP_UNAVAILABLE`. Those numbers are archived as `evidence/41a-c`; they stand in
for the device's media pipeline, not for our page. This tool measures the page itself:

  --what clock      clock offset and residual over 200 pings at 5 Hz (§3.1)
  --what noise      the page's reported playhead, scored like evidence/49 (σ, p95, shape)
  --what start      10 x play_at: where the picture actually starts, vs the instant asked for
  --what rate       60 s at 1.000 / 0.998 / 1.002: does the page's element honour playbackRate
  --what all        the four above, in that order

Every measurement is silent: the device's HDMI is out of the TV (a test pattern with no sound
plays), and nothing is sent to the group or to the TV.
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

DEVICE = "192.168.2.80"
APP_ID = "28028CD2"
MEDIA = "http://192.168.2.21:7641/media/rate-90s.mp4"


class Page:
    """Our receiver, over its own namespace."""

    def __init__(self, log=print):
        self.log = log
        self.d = probe.Recv(DEVICE, APP_ID, log=lambda *_: None)

    def __enter__(self):
        self.d.connect()
        status = self.d.app_status()
        if status[0] not in (None, "E8C28D3C", APP_ID):
            raise SystemExit(f"device is busy with {status[1]}: refusing to take it")
        self.d.launch()
        self.d.attach()
        hello = self.d.wait_hello(20)
        if not hello:
            raise SystemExit("no hello: the page is not answering our namespace")
        self.hello = hello
        return self

    def __exit__(self, *exc):
        try:
            self.d.send("stop")
            time.sleep(0.2)
            self.link_stop()
        except Exception:  # noqa: BLE001
            pass
        self.d.close()

    def link_stop(self):
        try:
            tr = self.d.transport
            self.d.link.stop_app(tr, timeout=6)
        except Exception:  # noqa: BLE001
            pass

    # -- messages ---------------------------------------------------------------------------
    def send(self, msg_type, **fields):
        return self.d.send(msg_type, **fields)

    def collect(self, seconds: float, hz: float = 10.0):
        out, end = [], time.time() + seconds
        while time.time() < end:
            t = time.time()
            out += self.d.pump(0.05)
            time.sleep(max(0.0, 1.0 / hz - (time.time() - t)))
        return out

    def states(self, seconds: float):
        return [m for m in self.collect(seconds) if m.get("type") == "state"]

    def load(self, seconds_wait: float = 8.0, autoplay: bool = False, media: float = 0.0):
        self.send("load", url=MEDIA, content_type="video/mp4", media=media, autoplay=autoplay)
        return self.states(seconds_wait)


# ---------------------------------------------------------------------------------------------
def measure_clock(page: Page, n: int = 200, hz: float = 5.0) -> dict:
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        page.send("clock")
        deadline = time.time() + 1.0
        reply = None
        while time.time() < deadline:
            for m in page.d.pump(0.01):
                if m.get("type") == "clock_reply":
                    reply = m
                    break
            if reply:
                break
        t3 = time.perf_counter()
        if reply is None:
            continue
        samples.append(
            {
                "rtt_ms": (t3 - t0) * 1000.0,
                # the reply's own clock reading, versus the midpoint of our round trip
                "err_ms": reply["rx_ms"] - ((t0 * 1000.0) + (t3 - t0) * 500.0),
                "rx_ms": reply["rx_ms"],
            }
        )
        time.sleep(max(0.0, 1.0 / hz - (t3 - t0)))
    if not samples:
        return {"ok": False, "why": "no clock replies"}
    best = sorted(samples, key=lambda s: s["rtt_ms"])[: max(3, len(samples) // 10)]
    offs = [s["err_ms"] for s in best]
    return {
        "ok": True,
        "pings": len(samples),
        "replies": len(samples),
        "rtt_ms": {
            "median": round(statistics.median(s["rtt_ms"] for s in samples), 2),
            "p95": round(sorted(s["rtt_ms"] for s in samples)[int(0.95 * (len(samples) - 1))], 2),
            "min": round(min(s["rtt_ms"] for s in samples), 2),
        },
        "offset_from_min_rtt_ms": {
            "median": round(statistics.median(offs), 2),
            "sigma": round(statistics.pstdev(offs), 2) if len(offs) > 1 else 0.0,
            "samples": len(best),
        },
        "note": (
            "offset_from_min_rtt is monotonic-domain (perf_counter) minus rx_ms: only its "
            "scatter matters here; the absolute offset is what SenderLink.ping() reports"
        ),
    }


def _fit(points):
    n = len(points)
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    sxx = sum((t - mt) ** 2 for t, _ in points)
    slope = sum((t - mt) * (v - mv) for t, v in points) / sxx if sxx else 1.0
    return slope, [(v - (mv + slope * (t - mt))) for t, v in points]


def measure_noise(page: Page, seconds: float = 60.0) -> dict:
    states = page.load()
    rows = [s for s in states if s.get("media") is not None]
    if not rows:
        return {"ok": False, "why": "no state messages: the page is not reporting"}
    more = page.states(seconds)
    rows += [s for s in more if s.get("media") is not None and s.get("player_state") == "playing"]
    rows = [s for s in rows if s.get("player_state") == "playing" and s.get("media") is not None]
    if len(rows) < 20:
        return {"ok": False, "why": f"only {len(rows)} playing states"}
    start = rows[0]["rx_ms"] / 1000.0
    pts = [(r["rx_ms"] / 1000.0 - start, float(r["media"])) for r in rows]
    slope, resid = _fit(pts)
    ms = [x * 1000.0 for x in resid]
    absms = sorted(abs(x) for x in ms)
    jumps = [
        abs(pts[i][1] - pts[i - 1][1] - slope * (pts[i][0] - pts[i - 1][0])) * 1000.0
        for i in range(1, len(pts))
    ]
    big = sum(1 for j in jumps if j > 20.0)
    return {
        "ok": True,
        "samples": len(ms),
        "span_s": round(pts[-1][0], 2),
        "state_hz": round(len(ms) / max(0.1, pts[-1][0]), 2),
        "rate_measured": round(slope, 6),
        "residual_ms": {
            "sigma": round(statistics.pstdev(ms), 2),
            "p50_abs": round(statistics.median(absms), 2),
            "p95_abs": round(absms[int(0.95 * (len(absms) - 1))], 2),
            "max_abs": round(absms[-1], 2),
        },
        "big_moves_over_20ms": big,
        "big_move_fraction": round(big / len(jumps), 3) if jumps else None,
        "shape": (
            "sawtooth-like"
            if jumps and big / len(jumps) > 0.05
            else "step-quantised"
            if jumps and statistics.median(jumps) > 5
            else "clean"
        ),
    }


def measure_start(page: Page, tries: int = 10, target: float = 20.0) -> dict:
    errors, missed = [], 0
    for _ in range(tries):
        page.send("load", url=MEDIA, content_type="video/mp4", media=target, autoplay=False)
        page.states(1.5)
        page.send("pause")
        rx = page.d.est_rx()
        if rx is None:
            missed += 1
            continue
        page.send("play_at", rx_ms=rx + 400.0, media=target)
        seen = page.collect(2.5, hz=10.0)
        playing = [m for m in seen if m.get("type") == "state" and m.get("player_state") == "playing"]
        if not playing:
            missed += 1
            continue
        first = playing[0]
        # where the picture was when the device first said it was playing
        errors.append((first["media"] - target) * 1000.0)
        page.send("stop")
        time.sleep(0.3)
    if not errors:
        return {"ok": False, "why": f"no playing state observed ({missed}/{tries})"}
    absms = sorted(abs(e) for e in errors)
    return {
        "ok": True,
        "tries": tries,
        "landed": len(errors),
        "error_ms": {
            "median": round(statistics.median(errors), 2),
            "mean": round(statistics.mean(errors), 2),
            "sigma": round(statistics.pstdev(errors), 2) if len(errors) > 1 else 0.0,
            "p95_abs": round(absms[int(0.95 * (len(absms) - 1))], 2),
            "max_abs": round(absms[-1], 2),
        },
        "note": (
            "read from the first 'playing' state, so it includes the page's own reaction "
            "time and the 4 Hz reporting: an upper bound on the true start error"
        ),
        "raw_ms": [round(e, 1) for e in errors],
    }


def measure_rate(page: Page, seconds: float = 60.0) -> dict:
    out = []
    for asked in (1.0, 0.998, 1.002):
        page.send("load", url=MEDIA, content_type="video/mp4", media=0.0, autoplay=True)
        page.states(2.0)
        page.send("rate", value=asked)
        time.sleep(1.0)
        rows = [
            s
            for s in page.states(seconds)
            if s.get("player_state") == "playing" and s.get("media") is not None
        ]
        page.send("stop")
        time.sleep(0.3)
        if len(rows) < 30:
            out.append({"asked": asked, "ok": False, "samples": len(rows)})
            continue
        start = rows[0]["rx_ms"] / 1000.0
        pts = [(r["rx_ms"] / 1000.0 - start, float(r["media"])) for r in rows]
        slope, resid = _fit(pts)
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        sxx = sum((t - mt) ** 2 for t, _ in pts)
        sigma = statistics.pstdev(resid) / (sxx**0.5) if sxx else None
        out.append(
            {
                "asked": asked,
                "ok": True,
                "samples": n,
                "measured_ratio": round(slope, 6),
                "ratio_sigma": None if sigma is None else round(sigma, 6),
                "measured_gain": round(slope - 1.0, 6),
                "asked_gain": round(asked - 1.0, 6),
            }
        )
    return {"ok": all(r.get("ok") for r in out), "runs": out}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--what",
        default="clock,noise,start",
        help="comma list of clock,noise,start,rate,all (default clock,noise,start)",
    )
    p.add_argument("--json", default=None)
    p.add_argument("--seconds", type=float, default=60.0)
    a = p.parse_args(argv)
    wanted = a.what.split(",") if a.what != "all" else ["clock", "noise", "start", "rate"]
    results = {}
    with Page() as page:
        results["hello"] = {k: page.hello.get(k) for k in ("ua", "codecs", "version")}
        if "clock" in wanted:
            results["clock"] = measure_clock(page)
        if "noise" in wanted:
            results["noise"] = measure_noise(page, a.seconds)
        if "start" in wanted:
            results["start"] = measure_start(page)
        if "rate" in wanted:
            results["rate"] = measure_rate(page, a.seconds)
    doc = {
        "task": "C on our own page (review 2026-10-26)",
        "command": " ".join([os.path.basename(sys.argv[0])] + list(argv if argv else sys.argv[1:])),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "device": DEVICE,
        "app_id": APP_ID,
        "media": MEDIA,
        "results": results,
    }
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(doc, fh, indent=1)
        print(f"saved {a.json}")
    print(json.dumps(results, indent=1))
    return 0 if all(v.get("ok", True) for v in results.values() if isinstance(v, dict)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
