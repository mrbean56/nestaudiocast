#!/usr/bin/env python3
"""Group membership + drift-control levers.

For each existing Google Cast group: start a muted DMR session, then ask *every*
Cast endpoint on the LAN which app it is running. Members show the session, so
membership is revealed without a single audible sample.

Then, on the group: correct SEEK accuracy (referenced to the instant the seek was
issued, not to the first sample after it) and whether SET_PLAYBACK_RATE works —
the lever we need to kill drift without audible seek gaps.
"""
import json
import sys
import time

sys.path.insert(0, "lib")
from castv2 import CastLink, receiver_app, CastError, probe_endpoint  # noqa: E402
from asset_server import AssetServer  # noqa: E402
from silence import silence, KITCHEN, GROUPS  # noqa: E402

DMR = "CC1AD845"
ASSET = "http://192.168.2.18:8099/yt/aqz-KE-bpKQ.m4a"

ALL_DEVICES = {
    "Cuisine Fenetre Vert": "192.168.2.61",
    "Cuisine Frigo Orange": "192.168.2.62",
    "Cuisine Frigo Jaune": "192.168.2.63",
    "Cuisine Meuble Bleu": "192.168.2.64",
    "Cuisine Meuble Rose": "192.168.2.65",
    "Salon Orange (Nest Hub)": "192.168.2.66",
    "Salon Jaune (Nest Hub)": "192.168.2.67",
    "Chromecast Salon (TV)": "192.168.2.80",
    "Chromecast Sous-Sol Ultra": "192.168.2.81",
    "Chromecast Chambre SS (Hub)": "192.168.2.119",
    "Chambre Maitre Orange (Hub)": "192.168.2.120",
    "Basement TV": "192.168.2.128",
    "TV Chambre principale (HD)": "192.168.2.130",
}

ev = {"asset": ASSET, "groups": {}, "seek": [], "rate": {}}


def who_runs(transport):
    """Ask every endpoint if it is currently running `transport`."""
    out = {}
    for name, host in ALL_DEVICES.items():
        try:
            l = CastLink(host, 8009, timeout=6)
            try:
                l.connect_platform()
                st = l.receiver_status()
                a_id, a_name, a_tr = receiver_app(st)
                out[name] = {"app": a_name, "app_id": a_id, "member": a_tr == transport}
            finally:
                l.close()
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": str(exc)[:60]}
        time.sleep(0.05)
    return out


def main():
    srv = AssetServer("assets", log_path="logs/asset-server.log").start()
    try:
        with silence():
            # ---------- membership per group ----------
            for gname, (host, port) in GROUPS.items():
                rec = {"endpoint": f"{host}:{port}"}
                try:
                    grp = CastLink(host, port)
                    grp.connect_platform()
                    grp.launch(DMR, timeout=25)
                    st = grp.receiver_status()
                    a_id, a_name, tr = receiver_app(st)
                    rec["transport"] = tr
                    grp.connect_transport(tr)
                    time.sleep(0.3)
                    grp.load(tr, {"appId": DMR, "sessionId": tr,
                                  "media": {"contentId": ASSET, "contentType": "audio/mp4",
                                            "streamType": "BUFFERED"},
                                  "autoplay": True, "currentTime": 30.0}, timeout=25)
                    time.sleep(1.5)
                    members = who_runs(tr)
                    rec["members"] = [k for k, v in members.items() if v.get("member")]
                    rec["detail"] = members
                    print(f"\n[{gname}] members = {rec['members']}")
                    grp.stop_app(tr, timeout=10)
                    grp.close()
                except Exception as exc:  # noqa: BLE001
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                    print(f"\n[{gname}] FAILED: {rec['error']}")
                ev["groups"][gname] = rec
                time.sleep(1.0)

            # ---------- seek accuracy, properly referenced ----------
            grp = CastLink(*GROUPS["Groupe Tous"])
            grp.connect_platform()
            grp.launch(DMR, timeout=25)
            st = grp.receiver_status()
            _, _, tr = receiver_app(st)
            grp.connect_transport(tr)
            time.sleep(0.3)
            lr = grp.load(tr, {"appId": DMR, "sessionId": tr,
                               "media": {"contentId": ASSET, "contentType": "audio/mp4",
                                         "streamType": "BUFFERED"},
                               "autoplay": True, "currentTime": 30.0}, timeout=25)
            sid = (lr.get("status") or [{}])[0].get("mediaSessionId")
            time.sleep(1.5)
            print(f"\nseek tests on mediaSessionId={sid}")
            for dest in [120.0, 300.0, 45.5, 600.0, 90.0, 200.0, 30.0, 45.0]:
                t_seek = time.time()
                grp.media_command(tr, "SEEK", sid, {"currentTime": dest}, timeout=8)
                time.sleep(1.0)
                ms = grp.media_status(tr, timeout=5)
                s = (ms.get("status") or [{}])[0]
                t_now, wall = s.get("currentTime"), time.time()
                expected = dest + (wall - t_seek)
                err = (t_now - expected) * 1000
                ev["seek"].append({"dest": dest, "t": t_now, "expected": round(expected, 3),
                                   "err_ms": round(err, 1), "state": s.get("playerState")})
                print(f"  SEEK {dest:7.1f} -> t={t_now:9.3f} expected={expected:9.3f} "
                      f"err={err:+8.1f} ms")

            # ---------- playback rate ----------
            print("\nplaybackRate tests")
            for rate in [1.005, 0.995, 1.0]:
                try:
                    r = grp.media_command(tr, "SET_PLAYBACK_RATE", sid, {"playbackRate": rate},
                                          timeout=6)
                    s = (r.get("status") or [{}])[0]
                    ev["rate"][str(rate)] = {"ok": True, "rate": s.get("playbackRate"),
                                             "state": s.get("playerState")}
                    print(f"  SET_PLAYBACK_RATE {rate} -> accepted, "
                          f"reported playbackRate={s.get('playbackRate')}")
                except CastError as exc:
                    ev["rate"][str(rate)] = {"ok": False, "error": str(exc)[:80]}
                    print(f"  SET_PLAYBACK_RATE {rate} -> REFUSED ({str(exc)[:60]})")
                time.sleep(0.4)
            # does the playhead actually move faster?
            if ev["rate"].get("1.005", {}).get("ok"):
                grp.media_command(tr, "SET_PLAYBACK_RATE", sid, {"playbackRate": 1.005})
                p = []
                for _ in range(12):
                    ms = grp.media_status(tr, timeout=5)
                    s = (ms.get("status") or [{}])[0]
                    p.append((time.time(), s.get("currentTime")))
                    time.sleep(0.4)
                d_wall = p[-1][0] - p[0][0]
                d_med = p[-1][1] - p[0][1]
                ev["rate"]["measured_ratio_at_1.005"] = round(d_med / d_wall, 6)
                print(f"  measured media/wall ratio at 1.005 = {d_med/d_wall:.6f}")
                grp.media_command(tr, "SET_PLAYBACK_RATE", sid, {"playbackRate": 1.0})
            grp.stop_app(tr, timeout=10)
            grp.close()
    finally:
        srv.stop()
        with open("evidence/08-groups-seek-rate.json", "w") as fh:
            json.dump(ev, fh, indent=2)
        print("\nsaved evidence/08-groups-seek-rate.json")


if __name__ == "__main__":
    main()
