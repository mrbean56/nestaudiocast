#!/usr/bin/env python3
"""Which group actuator lands most repeatably? (silent: kitchen speakers muted, restored)

For each trial we know the media position we asked for and the wall time of the command,
and we measure where the playhead's real-time line actually sits once it runs:
    origin = median(t - wall) over 2 s of motion;  landing = origin - (target - t_cmd)
Bias does not matter (it can be learned); the spread (std) of `landing` is the actuator's
repeatability, i.e. the floor of any start/realign accuracy.

  A: LOAD at position          (what the relay does now)
  B: SEEK while playing
  C: PAUSE -> SEEK (paused, buffered) -> PLAY at a chosen instant
"""
import json
import statistics as st
import sys
import time

sys.path.insert(0, "lib")
from castv2 import CastLink, receiver_app  # noqa: E402
from asset_server import AssetServer  # noqa: E402
import groupguard  # noqa: E402

DMR = "CC1AD845"
URL = "http://192.168.2.18:8099/yt/K0uxjdcZWyk.m4a"
SPK = ["192.168.2.61", "192.168.2.62", "192.168.2.63", "192.168.2.64", "192.168.2.65"]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 8


def mute_all(flag):
    for h in SPK:
        l = CastLink(h); l.connect_platform(); l.set_receiver_volume(muted=flag); l.close()


def sample(g, tr, secs):
    out = []
    end = time.time() + secs
    while time.time() < end:
        g.keepalive()
        t0 = time.time(); ms = g.media_status(tr, timeout=3); t1 = time.time()
        s = (ms.get("status") or [{}])[0]
        out.append(((t0 + t1) / 2, s.get("currentTime"), s.get("playerState")))
        time.sleep(0.12)
    return out


def landing(g, tr, target, t_cmd):
    # wait for motion, then fit the real-time line
    deadline = time.time() + 8
    prev = None
    while time.time() < deadline:
        w, t, stt = sample(g, tr, 0.15)[-1]
        if prev and t is not None and prev[1] is not None and stt == "PLAYING" and \
                abs((t - prev[1]) - (w - prev[0])) < 0.03 and w - prev[0] > 0.1:
            break
        prev = (w, t)
    pts = [(w, t) for w, t, s in sample(g, tr, 2.0) if t is not None and s == "PLAYING"]
    origin = st.median(t - w for w, t in pts)
    return (origin - (target - t_cmd)) * 1000


def main():
    groups = groupguard.discover_groups()
    host, port = groups["Cuisine"]
    groupguard.require_safe_group(host, port, "kitchen")
    srv = AssetServer("assets", log_path="logs/asset-server.log").start()
    g = CastLink(host, port); g.connect_platform()
    mute_all(True)
    res = {"A_load": [], "B_seek": [], "C_pause_seek_play": []}
    sess = None
    try:
        r = g.launch(DMR)
        app = (r.get("status", {}).get("applications") or [{}])[0]
        sess, tr = app.get("sessionId"), app.get("transportId")
        g.connect_transport(tr)
        pos = 100.0
        for k in range(N):
            # A
            pos += 20; t_cmd = time.time()
            ms = g.load(tr, {"media": {"contentId": URL, "contentType": "audio/mp4",
                                       "streamType": "BUFFERED"}, "autoplay": True,
                             "currentTime": pos}, timeout=25)
            msid = (ms.get("status") or [{}])[0].get("mediaSessionId")
            res["A_load"].append(landing(g, tr, pos, t_cmd))
            # B
            pos += 20; t_cmd = time.time()
            g.media_command(tr, "SEEK", msid, {"currentTime": pos})
            res["B_seek"].append(landing(g, tr, pos, t_cmd))
            # C
            pos += 20
            g.media_command(tr, "PAUSE", msid)
            time.sleep(0.4)
            g.media_command(tr, "SEEK", msid, {"currentTime": pos, "resumeState": "PLAYBACK_PAUSE"})
            time.sleep(1.5)
            t_cmd = time.time()
            g.media_command(tr, "PLAY", msid)
            res["C_pause_seek_play"].append(landing(g, tr, pos, t_cmd))
            print(k, {m: round(v[-1], 1) for m, v in res.items()}, flush=True)
    finally:
        try:
            if sess:
                g.stop_app(sess)
        finally:
            mute_all(False)
            g.close(); srv.stop()
    summary = {m: {"n": len(v), "mean_ms": round(st.mean(v), 1),
                   "std_ms": round(st.stdev(v), 1) if len(v) > 1 else None,
                   "values": [round(x, 1) for x in v]} for m, v in res.items()}
    print(json.dumps({m: {k: x for k, x in d.items() if k != "values"} for m, d in summary.items()}))
    json.dump(summary, open("evidence/27-actuator-repeatability.json", "w"), indent=1)


if __name__ == "__main__":
    main()
