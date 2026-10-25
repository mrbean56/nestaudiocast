#!/usr/bin/env python3
"""A/V offset measurement from a single room recording.

Method (see notes/measurement-protocol.md): the TV plays a video whose ~1 kHz beep is
time-locked to a visual flash, while the relay plays a click train in a *different,
non-harmonically-related* band (default 2.31 kHz) on the kitchen group. One recorder in
the room captures both; the perceived offset is (kitchen onset) - (TV onset).

Three failure modes are handled explicitly, because an instrument that lies is worse than
no instrument (a first version had all three, found in cross-review):

  1. **Aliasing.** With a click every `period` seconds, any true offset beyond ±period/2
     is indistinguishable from one inside it: +900 ms reads as -100 ms. We therefore
     refuse to conclude anything when the measured offset sits near ±period/2, and the
     verdict becomes AMBIGUOUS instead of PASS.
  2. **Harmonic bleed.** A 1 kHz beep reproduced by a non-linear TV speaker produces
     energy at 2 kHz, which would look like a kitchen click at zero offset (a silent
     false PASS). Two defences: the kitchen band is not a harmonic of the TV band, and
     any pair closer than `bleed_ms` is counted as suspected bleed.
  3. **Dispersion ignored.** A median of 0 ms with ±200 ms of scatter is not "in sync".
     The verdict now also requires a standard deviation under `max_stdev_ms`.

Usage:
  analyze_offset.py rec.wav [--tv-freq 1000] [--grp-freq 2310] [--period 1.0] [--json out]
"""
from __future__ import annotations

import argparse
import json
import wave

import numpy as np

MAX_OFFSET_MS = 150.0     # acceptance threshold
MAX_STDEV_MS = 40.0       # per measurement-protocol.md
BLEED_MS = 3.0            # two bands firing closer than this = suspected harmonic bleed


def read_wav(path):
    with wave.open(path, "rb") as w:
        n, ch, sw, sr = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if sw == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    elif sw == 1:
        data = (np.frombuffer(raw, dtype="<u1").astype(np.float64) - 128) / 128.0
    else:
        raise ValueError(f"unsupported sample width {sw}")
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def band_envelope(x, sr, freq, win=1024, hop=96, halfwidth=1):
    """Short-time magnitude around `freq`, one value per hop."""
    n_frames = max(0, 1 + (len(x) - win) // hop)
    if n_frames <= 0:
        return np.zeros(0), 0.0
    idx = np.arange(win)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(win)[None, :]
    spec = np.abs(np.fft.rfft(frames, axis=1))
    bin_hz = sr / win
    centre = int(round(freq / bin_hz))
    lo, hi = max(0, centre - halfwidth), min(spec.shape[1] - 1, centre + halfwidth)
    return spec[:, lo:hi + 1].max(axis=1), hop / sr


def onsets(env, hop_s, k=8.0, min_gap=0.25, floor_frac=0.06):
    """Rising-edge onsets with their envelope amplitude (amplitude is what lets us tell a
    real kitchen click from a harmonic of the TV beep).

    Returns (times, amplitudes).
    """
    if env.size == 0:
        return [], []
    noise = np.median(env) + 1e-12
    thr = max(noise * k, env.max() * floor_frac)
    times, amps = [], []
    above = env > thr
    last = -1e9
    for i in range(1, len(above)):
        t = i * hop_s
        if above[i] and not above[i - 1] and t - last >= min_gap:
            times.append(t)
            amps.append(float(env[i:i + 3].max()))
            last = t
    return times, amps


def pair(tv, grp, max_gap):
    pairs = []
    used = set()
    for t in tv:
        best, bd = None, 1e9
        for j, g in enumerate(grp):
            if j in used:
                continue
            d = abs(g - t)
            if d < bd:
                best, bd = j, d
        if best is not None and bd <= max_gap:
            used.add(best)
            pairs.append((t, grp[best], grp[best] - t, best))
    return pairs


def analyze(path, tv_freq=1000.0, grp_freq=2310.0, period=1.0, expect_ms=None):
    x, sr = read_wav(path)
    tv_env, hop = band_envelope(x, sr, tv_freq)
    gr_env, _ = band_envelope(x, sr, grp_freq)
    tv, tv_amp = onsets(tv_env, hop)
    gr, gr_amp = onsets(gr_env, hop)
    pairs = pair(tv, gr, max_gap=period * 0.6)

    res = {"file": path, "sr": sr, "duration_s": round(len(x) / sr, 2),
           "tv_freq": tv_freq, "grp_freq": grp_freq, "period_s": period,
           "tv_onsets": len(tv), "grp_onsets": len(gr),
           "pairs": len(pairs), "hop_ms": round(hop * 1000, 2)}
    if not pairs:
        res["verdict"] = "INVALID — no paired onsets (check levels, frequencies, positions)"
        return res

    deltas = np.array([p[2] for p in pairs])
    times = np.array([p[0] for p in pairs])

    # Robust step: a few pairs are simply wrong (a missed TV beep pairs with the neighbouring
    # kitchen click). On the first real recording 39/40 pairs agreed to +-6 ms while five
    # outliers pushed sigma to 59 ms. Keep the cluster around the median (MAD-based) and
    # report what was dropped, instead of letting pairing errors decide the verdict.
    med0 = float(np.median(deltas))
    mad = float(np.median(np.abs(deltas - med0))) or 1e-4
    keep = np.abs(deltas - med0) <= max(0.020, 4.0 * mad)
    rejected = int((~keep).sum())
    deltas, times = deltas[keep], times[keep]
    med = float(np.median(deltas)) * 1000
    sd = float(deltas.std(ddof=1)) * 1000 if len(deltas) > 1 else 0.0
    half = period * 500.0                      # aliasing half-window, ms

    # Harmonic bleed: a non-linear TV speaker puts energy in the kitchen band at the TV
    # onset instant, but much weaker than a real kitchen click. Timing alone cannot tell
    # "bleed" from "perfectly aligned", so compare amplitudes.
    gr_med = float(np.median(gr_amp)) if len(gr_amp) else 1.0
    tv_med = float(np.median(tv_amp)) if len(tv_amp) else 1.0
    bleed = 0
    for (t, g, d, j) in pairs:
        if abs(d) < BLEED_MS / 1000.0 and j < len(gr_amp):
            if gr_amp[j] < 0.35 * (tv_med / max(1e-12, gr_med)) * gr_med:
                bleed += 1

    res.update({
        "offset_median_ms": round(med, 1),
        "offset_mean_ms": round(float(deltas.mean()) * 1000, 1),
        "offset_stdev_ms": round(sd, 1),
        "offset_min_ms": round(float(deltas.min()) * 1000, 1),
        "offset_max_ms": round(float(deltas.max()) * 1000, 1),
        "pairs_ms": [round(float(v) * 1000, 1) for v in deltas],
        "suspected_bleed_pairs": bleed,
        "pairs_kept": int(len(deltas)), "pairs_rejected": rejected,
        "mad_ms": round(float(mad) * 1000, 1),
        "ambiguity_window_ms": round(half, 1),
    })

    # Aliasing: with a `period`-second click train, an offset of X is indistinguishable from
    # X - k*period. Report the whole family instead of pretending to one value.
    k_range = range(-3, 4)
    candidates = sorted(round(med + k * period * 1000, 1) for k in k_range)
    res["alias_candidates_ms"] = candidates
    if expect_ms is not None:
        best = min(candidates, key=lambda c: abs(c - expect_ms))
        res["alias_corrected_ms"] = best
        res["alias_correction_ms"] = round(best - med, 1)
        used = best
    else:
        used = med

    if len(deltas) >= 4:
        a, b = np.polyfit(times, deltas, 1)
        res["drift_ms_per_min"] = round(float(a) * 60 * 1000, 1)
        res["drift_projected_10min_ms"] = round(float(a) * 600 * 1000, 1)
        resid = deltas - (a * times + b)
        sxx = float(np.sum((times - times.mean()) ** 2))
        if sxx > 0 and len(deltas) > 2:
            se = (float(np.sum(resid ** 2)) / (len(deltas) - 2) / sxx) ** 0.5
            res["drift_sigma_ms_per_min"] = round(float(se) * 60 * 1000, 1)

    reasons = []
    if abs(med) > 0.9 * half:
        reasons.append(f"measured {med:.0f} ms sits at the ±{half:.0f} ms aliasing limit of a "
                       f"{period:g} s click period")
    if bleed > 0.2 * len(deltas):
        reasons.append(f"{bleed}/{len(deltas)} pairs look like harmonic bleed from the TV beep")
    if sd > MAX_STDEV_MS:
        reasons.append(f"dispersion {sd:.0f} ms > {MAX_STDEV_MS:g} ms")
    if len(deltas) < 8:
        reasons.append(f"only {len(deltas)} pairs")

    if reasons:
        res["verdict"] = "AMBIGUOUS — " + "; ".join(reasons)
    elif abs(used) <= MAX_OFFSET_MS:
        res["verdict"] = (f"PASS — offset {used:.0f} ms (<= {MAX_OFFSET_MS:g} ms), "
                          f"sigma {sd:.0f} ms; alias family {candidates[0]:.0f}..{candidates[-1]:.0f} ms")
    else:
        res["verdict"] = f"FAIL — offset {used:.0f} ms (> {MAX_OFFSET_MS:g} ms)"
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--tv-freq", type=float, default=1000.0)
    ap.add_argument("--grp-freq", type=float, default=2310.0)
    ap.add_argument("--period", type=float, default=1.0)
    ap.add_argument("--expect-ms", type=float, default=None,
                    help="expected offset from the digital estimate; picks the right alias")
    ap.add_argument("--json")
    a = ap.parse_args()
    r = analyze(a.wav, a.tv_freq, a.grp_freq, a.period, a.expect_ms)
    print(json.dumps({k: v for k, v in r.items() if k != "pairs_ms"}, indent=2))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(r, fh, indent=2)
