"""Does a Cast audio group render VIDEO on its Chromecast member?

Loads a silent-audio H.264 test pattern (assets/group_video_test.mp4) on the
"En Haut" group (5 kitchen Nest Audio + Chromecast Salon), logs what the group
and the TV report, holds for HOLD_S so a human can look at the TV, then stops.
Owner authorised this run on 2026-10-25 10:40 (TV on, nobody watching).
Usage: probe/group_video_test.py HOST PORT [HOLD_S]
"""
import json
import sys
import time

sys.path.insert(0, "lib")
from castv2 import CastLink, receiver_app  # noqa: E402
from asset_server import AssetServer  # noqa: E402

DMR = "CC1AD845"
TV = "192.168.2.80"
URL = "http://192.168.2.18:8099/group_video_test.mp4"
OUT = "evidence/18-group-video-test.json"

host, port = sys.argv[1], int(sys.argv[2])
hold = float(sys.argv[3]) if len(sys.argv) > 3 else 120
log = []


def rec(ev, **kw):
    row = {"t": round(time.time(), 3), "ev": ev, **kw}
    log.append(row)
    print(json.dumps(row)[:600], flush=True)


def tv_status():
    tv = CastLink(TV)
    tv.connect_platform()
    try:
        st = tv.receiver_status()
        app_id, name, tid = receiver_app(st)
        out = {"app_id": app_id, "app": name, "volume": st.get("volume")}
        if tid and app_id == DMR:
            tv.connect_transport(tid)
            ms = tv.media_status(tid).get("status") or []
            out["media"] = ms[0] if ms else None
        return out
    finally:
        tv.close()


srv = AssetServer("assets", log_path="logs/asset-server.log").start()
g = CastLink(host, port)
g.connect_platform()
sess = None
try:
    rec("tv_before", **tv_status())
    st = g.receiver_status()
    rec("group_before", app=receiver_app(st), volume=st.get("volume"))
    r = g.launch(DMR)
    app_id, name, tid = receiver_app(r.get("status", {}))
    sess = (r.get("status", {}).get("applications") or [{}])[0].get("sessionId")
    rec("launched", app=name, transport=tid)
    g.connect_transport(tid)
    r = g.load(tid, {"media": {"contentId": URL, "contentType": "video/mp4",
                               "streamType": "BUFFERED"},
                     "autoplay": True, "currentTime": 0})
    rec("load_reply", payload=r)
    t_end = time.time() + hold
    n = 0
    while time.time() < t_end:
        time.sleep(5)
        n += 1
        ms = (g.media_status(tid).get("status") or [{}])[0]
        rec("group_media", state=ms.get("playerState"), t=ms.get("currentTime"),
            media=(ms.get("media") or {}) if n == 1 else None,
            idle=ms.get("idleReason"))
        if n in (2, 6):
            rec("tv_during", **tv_status())
finally:
    try:
        if sess:
            g.stop_app(sess)
            rec("stopped", session=sess)
    except Exception as e:  # noqa: BLE001
        rec("stop_error", error=repr(e))
    g.close()
    srv.stop()
    time.sleep(2)
    try:
        rec("tv_after", **tv_status())
    except Exception as e:  # noqa: BLE001
        rec("tv_after_error", error=repr(e))
    json.dump(log, open(OUT, "w"), indent=1)
    print("saved", OUT)
