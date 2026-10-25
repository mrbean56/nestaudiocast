#!/usr/bin/env python3
"""Build the kitchen measurement track for a YouTube sync-test video.

  measure_track.py VID [--freq 2310]

Reads assets/yt/VID.m4a, finds the video's own beep onsets (media time), and writes
assets/measure/VID.m4a: clicks at FREQ Hz at exactly those media times, silence elsewhere,
same duration. With the relay in --measure-tracks mode the TV plays the video's beep and
the kitchen plays a click in another band at the same media time, so one room recording
gives offset = t(kitchen click) - t(TV beep) per beep.
Also writes assets/measure/VID.json (onsets, beep band) for the analyzer.
"""
import argparse
import json
import os
import subprocess
import sys
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFMPEG = os.path.join(ROOT, "bin", "ffmpeg")
SR = 48000


def decode(path):
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def onsets(x):
    env = np.convolve(np.abs(x), np.ones(48) / 48, "same")
    thr = 0.3 * env.max()
    on = np.where((env[1:] > thr) & (env[:-1] <= thr))[0] / SR
    return on[np.insert(np.diff(on) > 0.3, 0, True)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vid")
    ap.add_argument("--freq", type=float, default=2310.0)
    ap.add_argument("--click-ms", type=float, default=30.0)
    a = ap.parse_args()
    x = decode(os.path.join(ROOT, "assets", "yt", a.vid + ".m4a"))
    on = onsets(x)
    seg = x[: SR * 60]
    spec = np.abs(np.fft.rfft(seg))
    beep_hz = float(np.fft.rfftfreq(len(seg), 1 / SR)[np.argmax(spec)])
    out = np.zeros_like(x)
    n = int(SR * a.click_ms / 1000)
    i = np.arange(n)
    shape = 0.8 * np.sin(np.pi * i / n) ** 2 * np.sin(2 * np.pi * a.freq * i / SR)
    for t in on:
        k = int(round(t * SR))
        out[k:k + n] += shape[: max(0, min(n, len(out) - k))]
    os.makedirs(os.path.join(ROOT, "assets", "measure"), exist_ok=True)
    wav = f"/tmp/measure-{a.vid}.wav"
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(out, -1, 1) * 32767).astype("<i2").tobytes())
    dst = os.path.join(ROOT, "assets", "measure", a.vid + ".m4a")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", wav, "-c:a", "aac", "-b:a", "96k",
                    "-movflags", "+faststart", dst], check=True)
    # Verify the encoded track: its clicks must sit where the beeps are (AAC priming etc.)
    enc_on = onsets(decode(dst))
    shift = [float(np.min(np.abs(on - t))) * 1000 for t in enc_on[:50]]
    meta = {"vid": a.vid, "beep_hz": round(beep_hz), "click_hz": a.freq, "n": len(on),
            "onsets": [round(float(t), 4) for t in on],
            "encoded_click_vs_beep_ms_max": round(max(shift), 2),
            "duration_s": round(len(x) / SR, 3)}
    json.dump(meta, open(os.path.join(ROOT, "assets", "measure", a.vid + ".json"), "w"))
    print(json.dumps({k: v for k, v in meta.items() if k != "onsets"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
