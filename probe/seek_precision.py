#!/usr/bin/env python3
"""Measure what the Cast group actually does with time: start latency, seek accuracy,
seek settle time, and whether the playhead advances at 1.000x.

Runs fully muted (silence guard): muting does not stop the media clock, so every
number below is real pipeline behaviour with no sound in the house.
"""
import json
import os
import sys
import time

sys.path.insert(0, "lib")
from castv2 import CastLink, receiver_app, CastError  # noqa: E402
from asset_server import AssetServer  # noqa: E402
from silence import silence, GROUPS  # noqa: E402

DMR = "CC1AD845"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
GROUP = GROUPS["Groupe Tous"]
ASSET = "http://192.168.2.18:8099/yt/aqz-KE-bpKQ.m4a"
OUT = "evidence/07-seek-precision.json"
ev = {"asset": ASSET, "group": list(GROUP), "trials": []}


def sample(link, transport, n=1, gap=0.0):
    out = []
    for _ in range(n):
        t0 = time.time()
        try:
            ms = link.media_status(transport, timeout=5)
            t1 = time.time()
            s = (ms.get("status") or [{}])[0]
            out.append({"wall": (t0 + t1) / 2, "rtt_ms": round((t1 - t0) * 1000, 1),
                        "t": s.get("currentTime"), "state": s.get("playerState")})
        except CastError as exc:
            out.append({"wall": time.time(), "error": str(exc)[:50]})
        if gap:
            time.sleep(gap)
    return out


def main():
    srv = AssetServer("assets", log_path="logs/asset-server.log").start()
    print(f"asset server up: {ASSET}")
    try:
        with silence() as saved:
            ev["muted_state"] = saved
            grp = CastLink(*GROUP)
            try:
                grp.connect_platform()
                r = grp.launch(DMR, timeout=25)
                st = grp.receiver_status()
                app_id, app_name, tr = receiver_app(st)
                print(f"launched {app_name} transport={tr}")
                grp.connect_transport(tr)
                time.sleep(0.3)

                # ---------- trial 1: LOAD at a non-zero playhead ----------
                target = 60.0
                t_load = time.time()
                lr = grp.load(tr, {
                    "appId": DMR, "sessionId": tr,
                    "media": {"contentId": ASSET, "contentType": "audio/mp4",
                              "streamType": "BUFFERED"},
                    "autoplay": True, "currentTime": target,
                }, timeout=25)
                s0 = (lr.get("status") or [{}])[0]
                print(f"LOAD@60 -> state={s0.get('playerState')} t={s0.get('currentTime')} "
                      f"cmds={s0.get('supportedMediaCommands')}")
                ev["supported_media_commands"] = s0.get("supportedMediaCommands")

                ss = sample(grp, tr, n=24, gap=0.25)
                ev["load_trial"] = {"target": target, "load_wall": t_load, "samples": ss}
                good = [x for x in ss if x.get("t") is not None and x.get("state") == "PLAYING"]
                if good:
                    first = good[0]
                    print(f"first PLAYING sample: t={first['t']:.3f} at "
                          f"{(first['wall'] - t_load) * 1000:.0f} ms after LOAD "
                          f"(target {target})")

                # ---------- trial 2: SEEK accuracy, 6 seeks ----------
                seeks = []
                for k, dest in enumerate([120.0, 300.0, 45.5, 600.0, 90.0, 200.0]):
                    t_seek = time.time()
                    try:
                        grp.media_command(tr, "SEEK", s0["mediaSessionId"],
                                          {"currentTime": dest}, timeout=8)
                    except CastError as exc:
                        seeks.append({"dest": dest, "error": str(exc)[:60]})
                        continue
                    time.sleep(1.2)  # settle
                    post = sample(grp, tr, n=4, gap=0.2)
                    ok = [x for x in post if x.get("t") is not None]
                    if ok:
                        errs = [ (x["t"] - dest - (x["wall"] - ok[0]["wall"])) * 1000 for x in ok ]
                        seeks.append({"dest": dest, "settle_ms": 1200,
                                      "post": ok,
                                      "err_ms": [round(e, 1) for e in errs]})
                        print(f"SEEK {dest:7.1f} -> t={ok[0]['t']:.3f} "
                              f"err={errs[0]:+.1f} ms  (spread {max(errs)-min(errs):.1f} ms)")
                    else:
                        seeks.append({"dest": dest, "post": post})
                        print(f"SEEK {dest:7.1f} -> no playhead")
                ev["seeks"] = seeks

                # ---------- trial 3: does the clock run at 1.000x? 20 s ----------
                t_start = time.time()
                drift = sample(grp, tr, n=40, gap=0.5)
                ev["drift_trial"] = {"start_wall": t_start, "samples": drift}
                pts = [(x["wall"], x["t"]) for x in drift
                       if x.get("t") is not None and x.get("state") == "PLAYING"]
                if len(pts) > 5:
                    n = len(pts)
                    mx = sum(p[0] for p in pts) / n
                    my = sum(p[1] for p in pts) / n
                    a = sum((p[0] - mx) * (p[1] - my) for p in pts) / sum((p[0] - mx) ** 2 for p in pts)
                    print(f"clock rate over {pts[-1][0]-pts[0][0]:.1f}s = {a:.6f} "
                          f"({(a-1)*600*1000:+.1f} ms per 10 min)")

                grp.stop_app(tr, timeout=10)
            finally:
                grp.close()
    finally:
        srv.stop()
        with open(OUT, "w") as fh:
            json.dump(ev, fh, indent=2)
        print(f"\nsaved {OUT}")


if __name__ == "__main__":
    main()
