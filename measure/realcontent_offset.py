#!/usr/bin/env python3
"""Perceived A/V offset with REAL content, using a single recording with alternating sources.

Why not simply correlate and look for two peaks: music is periodic, so the correlation has
many comparable peaks and the "second peak" is usually spurious (measured: 233 ms returned
for a true 0 ms, `evidence/32-realcontent-selftest.json`).

Method instead: one continuous recording, and during it the two sources are muted in turn
(TV only / both / kitchen only). Each single-source segment is correlated against the video's
own audio, which gives one sharp lag per segment. Both segments share the same recorder clock,
so the difference of the two lags is exactly the perceived offset — the unknown start of the
recording cancels out.

  realcontent_offset.py REC.wav REF.wav --ref-media-start S \
      --tv-only 3:16 --kitchen-only 22:34 [--search 1.5]
"""
import argparse
import json
import sys
import wave

import numpy as np

BAND = (200.0, 5000.0)
MIN_S = 6.0          # shortest usable single-source segment
MIN_HALVES_MS = 20.0  # the two halves of a segment must find the same lag this closely


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def bandpass(x, sr):
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / sr)
    spec[(f < BAND[0]) | (f > BAND[1])] = 0
    return np.fft.irfft(spec, len(x))


def lag_of(seg, ref, sr, expect_s, search_s):
    """Best lag of `seg` inside `ref`, searched around `expect_s`."""
    nfft = 1 << (len(seg) + len(ref) - 1).bit_length()
    fs = np.fft.rfft(seg - seg.mean(), nfft)
    fr = np.fft.rfft(ref - ref.mean(), nfft)
    c = np.fft.irfft(fs * np.conj(fr), nfft)
    zero = len(ref) - 1
    lo = max(0, int(zero + (expect_s - search_s) * sr))
    hi = min(len(c), int(zero + (expect_s + search_s) * sr))
    w = c[lo:hi]
    lags = (np.arange(lo, hi) - zero) / sr
    k = int(np.argmax(w))
    peak = float(w[k])
    # second best outside +-30 ms of the peak
    mask = np.abs(lags - lags[k]) > 0.030
    second = float(np.max(w[mask])) if mask.any() else 0.0
    ratio = peak / (second if second > 0 else 1e-12)
    # parabolic refinement for sub-sample precision
    if 0 < k < len(w) - 1:
        y0, y1, y2 = w[k - 1], w[k], w[k + 1]
        denom = (y0 - 2 * y1 + y2)
        delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-12 else 0.0
    else:
        delta = 0.0
    return {"lag_s": float(lags[k] + delta / sr), "peak": peak, "ratio": ratio}


def parse(t):
    if ":" in t:
        a, b = t.split(":")
        return float(a) * 60 + float(b)
    return float(t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rec")
    ap.add_argument("ref")
    ap.add_argument("--ref-media-start", type=float, required=True)
    ap.add_argument("--tv-only", required=True, help="start:length of the TV-only segment, s")
    ap.add_argument("--kitchen-only", required=True, help="start:length of the kitchen-only segment, s")
    ap.add_argument("--both", help="optional both-sources segment (sanity check)")
    ap.add_argument("--search", type=float, default=0.6,
                    help="lag search half-window (s); keep it narrow, music repeats")
    ap.add_argument("--json")
    a = ap.parse_args()
    rec, sr = read_wav(a.rec)
    ref, sr2 = read_wav(a.ref)
    if sr != sr2:
        raise SystemExit("sample rates differ")
    rec_b = bandpass(rec, sr)
    ref_b = bandpass(ref, sr)
    # reference excerpt covering the whole recording, with margin
    m0 = max(0.0, a.ref_media_start - 3.0)
    need = len(rec) / sr + 6.0
    seg = ref_b[int(m0 * sr): int((m0 + need) * sr)]
    res = {"rec": a.rec, "ref": a.ref, "rec_s": round(len(rec) / sr, 1),
           "ref_media_start": a.ref_media_start}
    expect = a.ref_media_start - m0          # where the recording starts inside the excerpt
    for name, spec in (("tv", a.tv_only), ("kitchen", a.kitchen_only), ("both", a.both)):
        if not spec:
            continue
        st, ln = [float(x) for x in spec.split(":")]
        if ln < MIN_S:
            res[name] = {"error": f"segment shorter than {MIN_S:g} s"}
            continue
        chunk = rec_b[int(st * sr): int((st + ln) * sr)]
        r = lag_of(chunk, seg, sr, expect + st, a.search)
        r["start_s"], r["len_s"] = st, ln
        # Robustness on periodic music: the two halves of the segment must agree. A beat-
        # shifted false peak moves the lag by the beat period and the halves disagree.
        half = int(len(chunk) / 2)
        h1 = lag_of(chunk[:half], seg, sr, expect + st, a.search)
        h2 = lag_of(chunk[half:], seg, sr, expect + st + half / sr, a.search)
        agree = abs(h1["lag_s"] - h2["lag_s"])
        r["halves_ms"] = [round(h1["lag_s"] * 1000, 1), round(h2["lag_s"] * 1000, 1)]
        r["halves_agree_ms"] = round(agree * 1000, 1)
        r["ok"] = agree <= 0.020
        res[name] = r
    tv, kt = res.get("tv", {}), res.get("kitchen", {})
    if tv.get("ok") and kt.get("ok"):
        off = (kt["lag_s"] - tv["lag_s"]) * 1000
        res["offset_ms"] = round(off, 1)
        res["verdict"] = (f"offset {off:+.0f} ms (kitchen minus TV); halves agree "
                          f"within {tv['halves_agree_ms']:.0f} / {kt['halves_agree_ms']:.0f} ms")
    else:
        res["verdict"] = ("INVALID — halves of a segment disagree "
                          f"(tv: {tv.get('halves_agree_ms')} ms, kitchen: {kt.get('halves_agree_ms')} ms)")
    print(json.dumps(res, indent=1))
    if a.json:
        json.dump(res, open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
