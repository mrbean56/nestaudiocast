#!/usr/bin/env python3
"""Settle two open questions from the cross-review, on ONE muted Nest Audio.

1. **Is SET_PLAYBACK_RATE a real lever or an echo?** The group's advertised
   `supportedMediaCommands` does not clearly include it, and the earlier gain estimate came
   from two samples 4.4 s apart (±0.23 %, i.e. useless). Here the rate is measured by least
   squares over 60 s of playhead samples, which gives a slope error ~1e-4 — enough to say
   whether 1.005 is honoured and by how much.

2. **What is the clock drift over a real 10 minutes?** The earlier 20 s measurement gave
   1.000091 ±114 ms/10 min at 1σ, i.e. indistinguishable from zero. The acceptance criterion
   is "no drift over ≥10 min", so it needs a 600 s baseline, not 20 s.

Everything runs muted (`lib/silence.py` restores volumes AND stops the session afterwards).
"""
import json
import sys
import time

sys.path.insert(0, "lib")
from castv2 import CastLink, receiver_app, CastError  # noqa: E402
from asset_server import AssetServer  # noqa: E402
from silence import silence  # noqa: E402

DMR = "CC1AD845"
HOST = "192.168.2.61"
ASSET = "http://192.168.2.18:8099/yt/aqz-KE-bpKQ.m4a"
OUT = "evidence/17-rate-gain-and-drift.json"


def fit(pts):
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    a = sxy / sxx
    b = my - a * mx
    resid = [p[1] - (a * p[0] + b) for p in pts]
    s2 = sum(r * r for r in resid) / max(1, n - 2)
    se = (s2 / sxx) ** 0.5
    return a, se, max(abs(r) for r in resid)


def measure(link, tr, sid, seconds, label):
    pts = []
    t_end = time.time() + seconds
    while time.time() < t_end:
        t0 = time.time()
        try:
            ms = link.media_status(tr, timeout=4)
            t1 = time.time()
            s = (ms.get("status") or [{}])[0]
            if s.get("playerState") == "PLAYING" and s.get("currentTime") is not None:
                pts.append(((t0 + t1) / 2, s["currentTime"]))
        except CastError:
            pass
        time.sleep(0.5)
    if len(pts) < 10:
        return {"label": label, "error": "not enough samples", "n": len(pts)}
    a, se, resid = fit(pts)
    return {"label": label, "n": len(pts),
            "span_s": round(pts[-1][0] - pts[0][0], 1),
            "ratio": round(a, 6),
            "ratio_sigma": round(se, 6),
            "gain_vs_1": round(a - 1, 6),
            "gain_sigma": round(se, 6),
            "residual_max_ms": round(resid * 1000, 1),
            "drift_ms_per_10min": round((a - 1) * 600 * 1000, 1),
            "drift_sigma_ms_per_10min": round(se * 600 * 1000, 1),
            "samples": [[round(p[0], 3), round(p[1], 3)] for p in pts]}


def main():
    srv = AssetServer("assets", log_path="logs/asset-server.log").start()
    out = {"host": HOST, "asset": ASSET, "runs": []}
    try:
        with silence(devices=["Cuisine Fenetre Vert"]):
            link = CastLink(HOST)
            link.connect_platform()
            link.launch(DMR, timeout=25)
            st = link.receiver_status()
            _, _, tr = receiver_app(st)
            link.connect_transport(tr)
            time.sleep(0.3)
            lr = link.load(tr, {"appId": DMR, "sessionId": tr,
                                "media": {"contentId": ASSET, "contentType": "audio/mp4",
                                          "streamType": "BUFFERED"},
                                "autoplay": True, "currentTime": 0.0}, timeout=25)
            s = (lr.get("status") or [{}])[0]
            sid = s.get("mediaSessionId")
            cmds = s.get("supportedMediaCommands")
            out["supported_media_commands"] = cmds
            out["supports_playback_rate_bit"] = bool(cmds and cmds & 4096)
            print(f"supportedMediaCommands={cmds} (bit 4096 = {bool(cmds and cmds & 4096)})")

            for rate, secs, label in [(1.005, 60, "rate_1.005"),
                                      (0.995, 60, "rate_0.995"),
                                      (1.0, 600, "rate_1.000_over_10min")]:
                link.media_command(tr, "SET_PLAYBACK_RATE", sid, {"playbackRate": rate}, timeout=8)
                time.sleep(0.5)
                r = measure(link, tr, sid, secs, label)
                r["requested_rate"] = rate
                out["runs"].append(r)
                print(json.dumps({k: v for k, v in r.items() if k != "samples"}, ensure_ascii=False))
                # jump back to 0 so the 10-minute run always has room ahead of it
                link.media_command(tr, "SEEK", sid, {"currentTime": 0.0}, timeout=8)
                time.sleep(1.0)
            link.media_command(tr, "SET_PLAYBACK_RATE", sid, {"playbackRate": 1.0}, timeout=8)
            link.close()
    finally:
        srv.stop()
        with open(OUT, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nsaved {OUT}")


if __name__ == "__main__":
    main()
