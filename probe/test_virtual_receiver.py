#!/usr/bin/env python3
"""Track 1 proof: does our virtual receiver speak CASTv2 well enough for a sender to
launch an app, load media and read a running playhead?

Uses our own CASTv2 client as the sender (it performs no certificate validation, so
this isolates protocol correctness from the Google-signed-certificate question).
"""
import json
import sys
import time

sys.path.insert(0, "lib")
from castv2 import CastLink, receiver_app, CastError  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8009
HOST = "127.0.0.1"
DMR = "CC1AD845"
out = {"host": HOST, "port": PORT, "steps": []}


def step(name, **kw):
    rec = {"t": round(time.time(), 3), "step": name, **kw}
    out["steps"].append(rec)
    print(f"{name}: {json.dumps(kw, ensure_ascii=False)[:300]}")


def main():
    link = CastLink(HOST, PORT)
    try:
        link.connect_platform()
        step("connected")

        st = link.receiver_status()
        step("receiver_status", apps=len(st.get("applications") or []),
             volume=st.get("volume"))

        r = link.launch(DMR, timeout=8)
        app_id, app_name, tr = receiver_app(r.get("status") or {})
        step("launched", app_id=app_id, app=app_name, transport=tr)
        if not tr:
            step("abort", why="no transport")
            return

        link.connect_transport(tr)
        time.sleep(0.2)
        ms = link.media_status(tr, timeout=5)
        step("media_status_empty", status=ms.get("status"))

        lr = link.load(tr, {
            "appId": DMR, "sessionId": tr,
            "media": {"contentId": "http://192.168.2.18:8099/yt/aqz-KE-bpKQ.m4a",
                      "contentType": "audio/mp4", "streamType": "BUFFERED"},
            "autoplay": True, "currentTime": 42.0}, timeout=8)
        s = (lr.get("status") or [{}])[0]
        step("loaded", state=s.get("playerState"), t=s.get("currentTime"))

        # does the playhead advance in real time?
        t0 = time.time()
        first = (s.get("currentTime"), t0)
        time.sleep(2.0)
        ms = link.media_status(tr, timeout=5)
        s2 = (ms.get("status") or [{}])[0]
        t1 = time.time()
        adv = (s2.get("currentTime") - first[0])
        wall = t1 - first[1]
        step("playhead_advances", media_delta=round(adv, 3), wall_delta=round(wall, 3),
             ratio=round(adv / wall, 4))

        link.media_command(tr, "SEEK", s2.get("mediaSessionId"), {"currentTime": 300.0})
        time.sleep(0.3)
        ms = link.media_status(tr, timeout=5)
        s3 = (ms.get("status") or [{}])[0]
        step("seeked", t=s3.get("currentTime"), state=s3.get("playerState"))

        link.media_command(tr, "PAUSE", s3.get("mediaSessionId"))
        time.sleep(0.4)
        ms = link.media_status(tr, timeout=5)
        s4 = (ms.get("status") or [{}])[0]
        p1 = s4.get("currentTime")
        time.sleep(1.0)
        ms = link.media_status(tr, timeout=5)
        s5 = (ms.get("status") or [{}])[0]
        step("paused_frozen", state=s5.get("playerState"),
             t_before=p1, t_after=s5.get("currentTime"),
             frozen=abs((s5.get("currentTime") or 0) - (p1 or 0)) < 0.01)

        link.stop_app(tr)
        st = link.receiver_status()
        step("stopped", apps=len(st.get("applications") or []))
    except CastError as exc:
        step("error", error=str(exc))
    finally:
        link.close()
        with open("evidence/12-virtual-receiver-protocol.json", "w") as fh:
            json.dump(out, fh, indent=2)
        print("\nsaved evidence/12-virtual-receiver-protocol.json")


if __name__ == "__main__":
    main()
