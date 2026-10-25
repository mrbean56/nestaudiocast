#!/usr/bin/env python3
"""Read-only snapshot of every kitchen Nest Audio + TV: what app, playing or not."""
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
OTHER = {"TV Salon (Chromecast)": "192.168.2.80"}


def snap(host):
    r = {}
    l = CastLink(host)
    try:
        l.connect_platform()
        st = l.receiver_status()
        app_id, name, tr = receiver_app(st)
        r.update(app=name, app_id=app_id, volume=(st.get("volume") or {}).get("level"),
                 muted=(st.get("volume") or {}).get("muted"))
        if tr:
            try:
                l.connect_transport(tr)
                time.sleep(0.2)
                ms = l.media_status(tr, timeout=5)
                s = (ms.get("status") or [{}])[0]
                r.update(state=s.get("playerState"), t=s.get("currentTime"),
                         content=(s.get("media") or {}).get("contentId", "")[:60])
            except CastError as e:
                r["media_err"] = str(e)[:60]
    finally:
        l.close()
    return r


if __name__ == "__main__":
    out = {}
    for name, host in {**KITCHEN, **OTHER}.items():
        out[name] = snap(host)
        print(f"{name:24s} {host:14s} {json.dumps(out[name])}")
    with open("evidence/05-device-snapshot.json", "w") as fh:
        json.dump(out, fh, indent=2)
