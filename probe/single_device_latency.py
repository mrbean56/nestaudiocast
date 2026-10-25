#!/usr/bin/env python3
"""Latency on ONE Nest Audio, muted: is it device-level or group-level, and does it
depend on the container format?

Why it matters:
  * the measurement protocol replaces the video's audio with our own 2 kHz click track.
    That is only legitimate if the pipeline latency does not depend on the file type;
  * `delta` (TV latency - kitchen latency) is treated as a constant. If latency differs
    a lot between one speaker and the group, or between formats, `delta` is not one
    number and the whole calibration story changes.

Everything is muted; the media clock is unaffected by muting.
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
ASSETS = [
    ("wav", "http://192.168.2.18:8099/click_train_2k.wav", "audio/wav"),
    ("m4a", "http://192.168.2.18:8099/yt/aqz-KE-bpKQ.m4a", "audio/mp4"),
]
ev = {"host": HOST, "trials": []}
START_AT = 60.0


def wait_playing(link, tr, timeout=8.0):
    """Time from LOAD to the first PLAYING report, and the playhead it reports."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ms = link.media_status(tr, timeout=3)
            s = (ms.get("status") or [{}])[0]
            if s.get("playerState") == "PLAYING":
                return time.time() - t0, s
        except CastError:
            pass
        time.sleep(0.05)
    return None, None


def main():
    srv = AssetServer("assets", log_path="logs/asset-server.log").start()
    try:
        with silence(devices=["Cuisine Fenetre Vert"]):
            link = CastLink(HOST)
            link.connect_platform()
            link.launch(DMR, timeout=25)
            st = link.receiver_status()
            _, _, tr = receiver_app(st)
            link.connect_transport(tr)
            time.sleep(0.3)

            for label, url, ctype in ASSETS:
                # --- start latency
                t0 = time.time()
                lr = link.load(tr, {"appId": DMR, "sessionId": tr,
                                    "media": {"contentId": url, "contentType": ctype,
                                              "streamType": "BUFFERED"},
                                    "autoplay": True, "currentTime": START_AT}, timeout=25)
                s0 = (lr.get("status") or [{}])[0]
                sid = s0.get("mediaSessionId")
                t_resp = time.time() - t0
                # poll for the playhead to actually leave the requested position
                first_move, moved = None, None
                while time.time() - t0 < 8:
                    ms = link.media_status(tr, timeout=3)
                    s = (ms.get("status") or [{}])[0]
                    t = s.get("currentTime") or 0
                    if s.get("playerState") == "PLAYING" and t > START_AT + 0.05:
                        first_move = time.time() - t0
                        moved = t
                        break
                    time.sleep(0.05)
                rec = {"format": label, "load_response_ms": round(t_resp * 1000, 1),
                       "state_at_load": s0.get("playerState"),
                       "reported_t_at_load": s0.get("currentTime"),
                       "time_to_playing_s": None if first_move is None else round(first_move, 3),
                       "t_when_first_seen_moving": None if moved is None else round(moved, 3)}

                # --- seek precision (reference = instant of the command)
                seeks = []
                for dest in (120.0, 45.0, 300.0, 90.0):
                    ts = time.time()
                    link.media_command(tr, "SEEK", sid, {"currentTime": dest}, timeout=8)
                    time.sleep(0.9)
                    ms = link.media_status(tr, timeout=4)
                    s = (ms.get("status") or [{}])[0]
                    tn, wall = s.get("currentTime"), time.time()
                    seeks.append(round((tn - (dest + (wall - ts))) * 1000, 1))
                rec["seek_err_ms"] = seeks
                rec["seek_err_std_ms"] = round(
                    (sum((x - sum(seeks) / len(seeks)) ** 2 for x in seeks) / max(1, len(seeks) - 1)) ** 0.5, 1)
                ev["trials"].append(rec)
                print(json.dumps(rec, ensure_ascii=False))
                # keep the same receiver app: a second LOAD simply replaces the media,
                # which avoids re-launching (and losing the transport id) between trials
                time.sleep(0.8)
            link.close()
    finally:
        srv.stop()
        with open("evidence/16-single-device-latency.json", "w") as fh:
            json.dump(ev, fh, indent=2)
        print("\nsaved evidence/16-single-device-latency.json")


if __name__ == "__main__":
    main()
