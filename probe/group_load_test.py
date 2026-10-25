#!/usr/bin/env python3
"""Does a Google Cast *group* accept a stream we push ourselves?

Silent by construction: every kitchen speaker is muted (and set to level 0) before
anything is loaded, then restored to its exact prior state afterwards.

Records: launch result, LOAD result, MEDIA_STATUS timeline (currentTime vs wall
clock), and — the real prize — which physical devices show the session running,
which reveals the group's membership without listening to it.
"""
import json
import sys
import time

sys.path.insert(0, "lib")
from castv2 import CastLink, receiver_app, CastError  # noqa: E402

KITCHEN = {
    "Cuisine Fenetre Vert": "192.168.2.61",
    "Cuisine Frigo Orange": "192.168.2.62",
    "Cuisine Frigo Jaune": "192.168.2.63",
    "Cuisine Meuble Bleu": "192.168.2.64",
    "Cuisine Meuble Rose": "192.168.2.65",
}
GROUP = ("192.168.2.61", 32012)  # "Groupe Tous"
DMR = "CC1AD845"
ASSET = "http://192.168.2.18:8099/sync_click.wav"

ev = {"group": f"{GROUP[0]}:{GROUP[1]}", "asset": ASSET, "steps": []}


def log(step, **kw):
    rec = {"t": time.time(), "step": step, **kw}
    ev["steps"].append(rec)
    print(f"[{time.strftime('%H:%M:%S')}] {step}: {json.dumps(kw, ensure_ascii=False)[:400]}")


def set_vol_mute(link, level=None, muted=None):
    payload = {}
    if level is not None:
        payload["level"] = level
    if muted is not None:
        payload["muted"] = muted
    return link.request("receiver-0", "urn:x-cast:com.google.cast.receiver",
                        {"type": "SET_VOLUME", "volume": payload}, timeout=6)["payload"]


def main():
    # ---- 1. snapshot + silence
    saved = {}
    for name, host in KITCHEN.items():
        l = CastLink(host)
        try:
            l.connect_platform()
            st = l.receiver_status()
            v = st.get("volume") or {}
            saved[name] = {"host": host, "level": v.get("level"), "muted": v.get("muted")}
            set_vol_mute(l, level=0.0, muted=True)
        finally:
            l.close()
    log("muted_kitchen", saved=saved)
    ev["saved_volumes"] = saved

    grp = None
    try:
        # ---- 2. connect to the group endpoint
        grp = CastLink(GROUP[0], GROUP[1])
        grp.connect_platform()
        st = grp.receiver_status()
        log("group_receiver_status", app=receiver_app(st), volume=st.get("volume"))

        # ---- 3. LAUNCH Default Media Receiver on the group
        t0 = time.time()
        try:
            r = grp.launch(DMR, timeout=25)
            log("launch_dmr_ok", ms=round((time.time() - t0) * 1000), payload=r.get("type"),
                app=(r.get("status") or {}).get("applications"))
        except CastError as e:
            log("launch_dmr_failed", ms=round((time.time() - t0) * 1000), error=str(e))
            r = None

        # ---- 4. find transport, LOAD
        st = grp.receiver_status()
        app_id, app_name, tr = receiver_app(st)
        log("after_launch", app_id=app_id, app=app_name, transport=tr)
        if not tr:
            log("abort", why="no transport id after launch")
            return

        grp.connect_transport(tr)
        time.sleep(0.3)
        load_req = {
            "appId": DMR,
            "sessionId": tr,
            "media": {
                "contentId": ASSET,
                "contentType": "audio/wav",
                "streamType": "BUFFERED",
                "metadata": {"metadataType": 0, "title": "sync_click"},
            },
            "autoplay": True,
            "currentTime": 0,
        }
        t0 = time.time()
        try:
            lr = grp.load(tr, load_req, timeout=25)
            log("load_ok", ms=round((time.time() - t0) * 1000),
                status=(lr.get("status") or [{}])[0] if lr.get("status") else None)
        except CastError as e:
            log("load_failed", ms=round((time.time() - t0) * 1000), error=str(e))

        # ---- 5. sample the group playhead against the wall clock
        samples = []
        for _ in range(30):
            try:
                ms = grp.media_status(tr, timeout=5)
                s = (ms.get("status") or [{}])[0]
                samples.append({"wall": round(time.time(), 4),
                                "currentTime": s.get("currentTime"),
                                "playerState": s.get("playerState")})
            except CastError as e:
                samples.append({"wall": round(time.time(), 4), "error": str(e)[:60]})
            time.sleep(0.5)
        ev["group_samples"] = samples
        log("group_samples", n=len(samples), first=samples[0], last=samples[-1])

        # ---- 6. membership: who is running the session now?
        members = {}
        for name, host in KITCHEN.items():
            l = CastLink(host)
            try:
                l.connect_platform()
                s2 = l.receiver_status()
                a_id, a_name, a_tr = receiver_app(s2)
                members[name] = {"app": a_name, "app_id": a_id, "transport": a_tr,
                                 "same_session": a_tr == tr}
            except Exception as exc:  # noqa: BLE001
                members[name] = {"error": str(exc)[:80]}
            finally:
                l.close()
        ev["membership_after_load"] = members
        log("membership", **{k: v.get("app") for k, v in members.items()})

        # ---- 7. stop
        try:
            grp.stop_app(tr, timeout=10)
            log("stopped", ok=True)
        except CastError as e:
            log("stop_failed", error=str(e))
        time.sleep(1.0)
        st = grp.receiver_status()
        log("group_after_stop", app=receiver_app(st))
    finally:
        if grp:
            grp.close()
        # ---- 8. restore volumes
        for name, info in saved.items():
            l = CastLink(info["host"])
            try:
                l.connect_platform()
                set_vol_mute(l, level=info["level"], muted=info["muted"])
            finally:
                l.close()
        log("volumes_restored", n=len(saved))

    with open("evidence/06-group-load-test.json", "w") as fh:
        json.dump(ev, fh, indent=2)
    print("\nsaved evidence/06-group-load-test.json")


if __name__ == "__main__":
    main()
