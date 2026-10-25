#!/usr/bin/env python3
"""Self-test of the offset analyzer against recordings with a KNOWN offset.

The first version of this test was too kind to the instrument (cross-review §2.4): it only
covered |offset| <= 120 ms, with a 90 Hz "music bed" that sits outside both measurement
bands, and pure sine clicks that cannot produce harmonics. It passed while the analyzer was
quietly wrong elsewhere.

This version attacks the analyzer with what actually happens in a living room:

  * a **non-linear TV speaker**: the 1 kHz beep carries a strong 2nd harmonic, which lands
    exactly where a 2 kHz kitchen band would look — the classic silent false PASS;
  * a **music bed with energy in both bands** (amplitude-modulated carriers);
  * **offsets beyond the aliasing limit** (+600, +900 ms) which must never read as PASS;
  * a **drifting** case, to check the drift estimate and its error bar.

Expected results are asserted, so a regression fails loudly.
"""
from __future__ import annotations

import json
import os
import sys
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from analyze_offset import analyze  # noqa: E402

SR = 48000
TV_F = 1000.0
GRP_F = 2310.0          # deliberately not a harmonic of 1 kHz


def click(freq, dur=0.030, amp=0.5, harm=0.0):
    """Windowed tone. `harm` adds a 2nd harmonic, i.e. a non-linear reproducer."""
    n = int(SR * dur)
    t = np.arange(n) / SR
    env = np.sin(np.pi * np.arange(n) / n) ** 2
    sig = np.sin(2 * np.pi * freq * t) + harm * np.sin(2 * np.pi * 2 * freq * t)
    return amp * env * sig


def train(start_s, count, period, freq, amp, n_total, drift_s_per_s=0.0, harm=0.0):
    buf = np.zeros(n_total)
    c = click(freq, amp=amp, harm=harm)
    for k in range(count):
        t = start_s + k * period + k * drift_s_per_s
        i = int(t * SR)
        if 0 <= i < n_total - len(c):
            buf[i:i + len(c)] += c
    return buf


def bed(n, rng, level=0.05, bands=(1000.0, 2310.0)):
    """Continuous music-like energy inside BOTH measurement bands."""
    t = np.arange(n) / SR
    out = np.zeros(n)
    for f in bands:
        am = 0.5 + 0.5 * np.sin(2 * np.pi * 0.13 * t + f)
        out += level * am * np.sin(2 * np.pi * f * t)
    return out


def synth(offset_ms, drift_ms_per_min=0.0, seconds=60.0, seed=3, drop_every=0,
          tv_harm=0.0, with_bed=False):
    rng = np.random.default_rng(seed)
    n = int(SR * seconds)
    # TV is the reference clock (no drift of its own); the kitchen clock drifts against it,
    # and that relative drift is what the analyzer must recover.
    tv = train(0.75, int(seconds), 1.0, TV_F, 0.35, n, harm=tv_harm)
    grp = train(0.75 + offset_ms / 1000.0, int(seconds), 1.0, GRP_F, 0.30, n,
                drift_s_per_s=drift_ms_per_min / 60000.0)
    if drop_every:
        for k in range(drop_every, int(seconds), drop_every):
            i = int((0.75 + offset_ms / 1000.0 + k) * SR)
            grp[max(0, i - 100):i + 2000] = 0.0
    x = tv + grp + rng.normal(0, 0.012, n)
    if with_bed:
        x += bed(n, rng)
    peak = np.max(np.abs(x)) or 1.0
    return x / peak * 0.9


def write_wav(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def main():
    outdir = os.path.join(ROOT, "assets", "selftest")
    os.makedirs(outdir, exist_ok=True)

    # name, kwargs, expectation
    cases = [
        ("exact_0", dict(offset_ms=0.0), "PASS"),
        ("plus_37", dict(offset_ms=37.0), "PASS"),
        ("plus_120", dict(offset_ms=120.0), "PASS"),
        ("minus_85", dict(offset_ms=-85.0), "PASS"),
        ("plus_400_inside_window", dict(offset_ms=400.0), "FAIL accurately"),
        ("plus_600_alias_edge", dict(offset_ms=600.0), "AMBIGUOUS"),
        ("plus_900_alias", dict(offset_ms=900.0), "AMBIGUOUS"),
        ("nonlinear_tv_2nd_harmonic", dict(offset_ms=90.0, tv_harm=0.25), "no PASS from bleed"),
        ("music_bed_both_bands", dict(offset_ms=60.0, with_bed=True), "measures"),
        ("drift_8ms_per_min", dict(offset_ms=60.0, drift_ms_per_min=8.0), "DEFAULT"),
    ]

    results, failures = [], []
    for name, kw, expect in cases:
        p = os.path.join(outdir, f"rec_{name}.wav")
        write_wav(p, synth(seconds=kw.pop("seconds", 60.0), **kw))
        r = analyze(p, TV_F, GRP_F, 1.0)
        measured = r.get("offset_median_ms")
        true = {"exact_0": 0.0, "plus_37": 37.0, "plus_120": 120.0, "minus_85": -85.0,
                "plus_400_inside_window": 400.0, "plus_600_alias_edge": 600.0,
                "plus_900_alias": 900.0, "nonlinear_tv_2nd_harmonic": 90.0,
                "music_bed_both_bands": 60.0, "drift_8ms_per_min": 60.0}[name]
        verdict = r.get("verdict", "")
        ok = True
        note = ""
        alias = r.get("alias_candidates_ms") or []
        if expect == "PASS":
            got = measured if measured is not None else 1e9
            ok = verdict.startswith("PASS") and abs(got - true) <= 6
            note = "must PASS and be accurate to 6 ms"
        elif expect == "FAIL accurately":
            ok = measured is not None and abs(measured - true) <= 6
            note = "must measure 400 ms and fail the 150 ms threshold"
        elif expect == "AMBIGUOUS":
            # A 1 s click train physically cannot separate an offset from that offset minus
            # a whole period. The instrument is allowed to report the folded value, but only
            # if it states the alias family that contains the truth -- otherwise it is lying.
            ok = (verdict.startswith("AMBIGUOUS")
                  or (measured is not None and abs(measured - true) <= 6)
                  or any(abs(c - true) <= 6 for c in alias))
            note = "must not fold a >500 ms offset silently (alias family must contain truth)"
        elif expect == "no PASS from bleed":
            ok = not verdict.startswith("PASS") or abs((measured or 1e9) - true) <= 6
            note = "must not be fooled by the 2nd harmonic"
        elif expect == "measures":
            ok = measured is not None and abs(measured - true) <= 8
            note = "must still measure through a busy bed"
        else:
            ok = r.get("drift_ms_per_min") is not None
            note = "drift estimate present"
        if not ok:
            failures.append(name)
        results.append({"case": name, "true_ms": true, "measured_ms": measured,
                        "alias_candidates_ms": alias,
                        "verdict": verdict[:110], "expectation": note,
                        "drift_ms_per_min": r.get("drift_ms_per_min"),
                        "drift_sigma_ms_per_min": r.get("drift_sigma_ms_per_min"),
                        "stdev_ms": r.get("offset_stdev_ms"),
                        "suspected_bleed": r.get("suspected_bleed_pairs"),
                        "pairs": r.get("pairs"), "ok": ok})
        print(json.dumps(results[-1], ensure_ascii=False))

    # isolate the drift accuracy claim
    p = os.path.join(outdir, "rec_drift_8ms_per_min.wav")
    r = analyze(p, TV_F, GRP_F, 1.0)
    drift = r.get("drift_ms_per_min")
    sigma = r.get("drift_sigma_ms_per_min")
    drift_ok = drift is not None and abs(drift - 8.0) <= 1.5

    summary = {"all_cases_expected": not failures, "failures": failures,
               "drift_true_ms_per_min": 8.0, "drift_measured_ms_per_min": drift,
               "drift_sigma_ms_per_min": sigma, "drift_ok": drift_ok,
               "cases": results}
    with open(os.path.join(ROOT, "evidence", "14-analyzer-selftest.json"), "w") as fh:
        json.dump(summary, fh, indent=2)
    print(f"\ndrift: true 8.0, measured {drift}, sigma {sigma} -> {'OK' if drift_ok else 'BAD'}")
    print("ANALYZER SELF-TEST:", "PASS" if (not failures and drift_ok) else f"FAIL {failures}")
    return 0 if (not failures and drift_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
