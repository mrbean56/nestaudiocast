#!/usr/bin/env python3
"""Generate test assets with pure stdlib (no ffmpeg needed).

sync_click.wav: 48 kHz stereo. A 1 kHz 40 ms click every 1.000 s (tick), and every
10 s a distinctive 3-click "sync word" (clicks at +0, +0.25, +0.75) so an acoustic
recording can be aligned to the stream timeline unambiguously.
Also writes sync_click.mp4-less variants? No: WAV + AAC-in-MP4 handled by ffmpeg when
available; WAV alone is accepted by Cast.
"""
import math
import struct
import sys
import wave

SR = 48000


def click(n_samples, freq=1000.0, dur_ms=40.0, amp=0.6):
    n = int(SR * dur_ms / 1000)
    out = [0.0] * n_samples
    for i in range(min(n, n_samples)):
        env = math.sin(math.pi * i / n) ** 2  # smooth window, no clicks
        out[i] = amp * env * math.sin(2 * math.pi * freq * i / SR)
    return out


def build(seconds: int, path: str):
    total = SR * seconds
    buf = [0.0] * total
    t = 0.0
    while t < seconds:
        idx = int(t * SR)
        pattern = [0.0, 0.25, 0.75] if int(round(t)) % 10 == 0 else [0.0]
        for off in pattern:
            c = click(total - idx - int(off * SR))
            for j, v in enumerate(c):
                buf[idx + int(off * SR) + j] += v
        t += 1.0
    peak = max(1e-9, max(abs(v) for v in buf))
    scale = 0.85 / peak
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = bytearray()
        for v in buf:
            s = int(max(-1.0, min(1.0, v * scale)) * 32767)
            frames += struct.pack("<hh", s, s)
        w.writeframes(bytes(frames))
    print(f"wrote {path}: {seconds}s, {len(frames)} bytes")


def click_train(seconds: int, freq: float, path: str, phase: float = 0.0,
                period: float = 1.0, dur_ms: float = 30.0, amp: float = 0.7):
    """Click train at `freq` Hz on every `period` seconds, offset by `phase`.

    This is the kitchen-side measurement signal: a different band from the TV beep so
    one room recording can separate the two sources.
    """
    total = int(SR * seconds)
    buf = [0.0] * total
    n = int(SR * dur_ms / 1000)
    shape = [amp * (math.sin(math.pi * i / n) ** 2) * math.sin(2 * math.pi * freq * i / SR)
             for i in range(n)]
    t = phase
    count = 0
    while t < seconds:
        idx = int(t * SR)
        for j, v in enumerate(shape):
            if idx + j < total:
                buf[idx + j] += v
        t += period
        count += 1
    peak = max(1e-9, max(abs(v) for v in buf))
    scale = 0.9 / peak
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, v * scale)) * 32767)) for v in buf))
    print(f"wrote {path}: {seconds}s, {count} clicks at {freq:.0f} Hz, phase {phase:.3f}s")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--click-train":
        secs = int(sys.argv[2]) if len(sys.argv) > 2 else 720
        freq = float(sys.argv[3]) if len(sys.argv) > 3 else 2000.0
        out = sys.argv[4] if len(sys.argv) > 4 else "assets/click_train_2k.wav"
        phase = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0
        click_train(secs, freq, out, phase)
    else:
        secs = int(sys.argv[1]) if len(sys.argv) > 1 else 30
        build(secs, sys.argv[2] if len(sys.argv) > 2 else "assets/sync_click.wav")
