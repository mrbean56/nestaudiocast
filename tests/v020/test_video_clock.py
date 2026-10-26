"""The video filter, judged on the device's own samples (evidence/49, review 2026-10-26).

`evidence/49-playhead-noise-dmr.json` holds 238 reads of the Chromecast's playhead: desktop-style
reasoning said the reads would be clean; they are a sawtooth (σ 60 ms, ±152 ms). This test replays
those exact rows through `sync.VideoClock` and checks what the control law would have seen.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import unittest

LIB = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from v020 import sync  # noqa: E402

EVIDENCE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "evidence", "49-playhead-noise-dmr.json")
)


def rows() -> list[dict]:
    with open(EVIDENCE) as fh:
        doc = json.load(fh)
    return [r for r in doc["raw_rows"] if r.get("media") is not None and r["player_state"] == "PLAYING"]


class TestAgainstTheDevice(unittest.TestCase):
    def test_the_archive_is_the_sawtooth_we_think_it_is(self):
        r = rows()
        self.assertGreater(len(r), 100)
        start = r[0]["t"]
        pts = [(x["t"] - start, x["media"]) for x in r]
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mv = sum(v for _, v in pts) / n
        sxx = sum((t - mt) ** 2 for t, _ in pts)
        slope = sum((t - mt) * (v - mv) for t, v in pts) / sxx
        resid = [(v - (mv + slope * (t - mt))) * 1000 for t, v in pts]
        self.assertGreater(statistics.pstdev(resid), 30.0, "a clean device would make the filter moot")

    def test_the_filter_turns_it_into_a_usable_position(self):
        """Against the media clock: one global least-squares line through the capture (§5).

        The media clock advances linearly; a window local to each sample would follow the
        device's own reporting wobble and flatter any filter. This is the stricter test.
        """
        r = rows()
        clock = sync.VideoClock()
        start = r[0]["t"]
        pts = [(x["t"] - start, x["media"]) for x in r]
        truth = _line(pts)  # the media clock is linear over 60 s
        errors = []
        for x in r:
            rx = x["t"] - start
            clock.add(rx * 1000.0, x["media"])
            # only once the window is full: that is the state the law trims in (§2, 30 s)
            if not clock.ready() or rx < sync.VDO_WINDOW_S + 2.0:
                continue
            est = clock.at(rx)
            errors.append((est - truth(rx)) * 1000.0)
        self.assertGreater(len(errors), 100)
        absms = sorted(abs(e) for e in errors)
        sigma = statistics.pstdev(errors)
        p95 = absms[int(0.95 * (len(absms) - 1))]
        self.assertLess(sigma, 7.0, f"filtered σ {sigma:.1f} ms")
        self.assertLess(p95, 12.0, f"filtered p95 {p95:.1f} ms")
        self.assertLess(absms[-1], 15.0, f"filtered max {absms[-1]:.1f} ms")

    def test_it_is_the_unbiased_candidate(self):
        """The choice is a measurement (lab note §5): only the mean leaves no DC offset.

        The upper envelope has a slightly smaller σ but sits **+82 ms** high on these reads, and
        that offset moves with the sawtooth's phase (33 ms of range): a calibration cannot absorb
        a bias that wanders, and `DELTA_V2` must keep meaning "render latency", not "estimator
        bug". Median is both biased (+22 ms) and noisier.
        """
        r = rows()
        start = r[0]["t"]
        pts = [(x["t"] - start, x["media"]) for x in r]

        def errors(pick):
            out = []
            for t, _ in pts:
                if t < 6.0:
                    continue
                w = [(tt, vv) for tt, vv in pts if t - 10.0 <= tt <= t]
                est = t + pick([vv - tt for tt, vv in w])
                out.append((est - _line(pts)(t)) * 1000.0)
            return out

        mean_err = errors(lambda o: sum(o) / len(o))
        median_err = errors(lambda o: statistics.median(o))
        envelope_err = errors(lambda o: sorted(o)[-2])
        self.assertLess(abs(statistics.mean(mean_err)), 5.0)
        for name, other in (("median", median_err), ("envelope", envelope_err)):
            with self.subTest(worse=name):
                self.assertGreater(abs(statistics.mean(other)), 3 * abs(statistics.mean(mean_err)))

    def test_a_seek_resets_the_window(self):
        r = rows()
        clock = sync.VideoClock()
        start = r[0]["t"]
        for x in r[:40]:
            clock.add((x["t"] - start) * 1000.0, x["media"])
        self.assertTrue(clock.samples)
        jumps = clock.resets
        x = r[40]
        clock.add((x["t"] - start) * 1000.0, x["media"] + 120.0)  # a 2-minute seek
        self.assertEqual(clock.resets, jumps + 1)
        self.assertLessEqual(len(clock.samples), 2)


def _line(points: "list[tuple[float, float]] | list[dict]", start: float = 0.0):
    """Least-squares line through (t, media) points, or through archive rows when ``start``."""
    pts = (
        [(x["t"] - start, x["media"]) for x in points]
        if points and isinstance(points[0], dict)
        else list(points)
    )
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mv = sum(v for _, v in pts) / n
    sxx = sum((t - mt) ** 2 for t, _ in pts)
    slope = sum((t - mt) * (v - mv) for t, v in pts) / sxx
    return lambda t: mv + slope * (t - mt)


if __name__ == "__main__":
    unittest.main()
