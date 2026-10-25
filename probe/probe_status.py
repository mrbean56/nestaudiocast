#!/usr/bin/env python3
"""Probe CASTv2 state of the devices that matter, without changing anything.

Connects by explicit host:port so we hit exactly the device we mean.
"""
import json
import sys
import time

import pychromecast
from pychromecast.models import CastInfo

TARGETS = [
    ("tv", "192.168.2.80", 8009),
    ("nest_cuisine_fenetre_vert", "192.168.2.61", 8009),
    ("group_tous", "192.168.2.61", 32012),
    ("group_en_haut", "192.168.2.61", 32132),
    ("group_dehors", "192.168.2.81", 32049),
]


def connect(host, port):
    ci = CastInfo(
        services=set(),
        uuid=None,
        model_name=None,
        friendly_name=None,
        host=host,
        port=port,
        cast_type=None,
        manufacturer=None,
    )
    return pychromecast.get_chromecast_from_cast_info(ci, None)


def probe(label, host, port):
    print(f"\n=== {label}  ({host}:{port})")
    out = {"label": label, "host": host, "port": port}
    try:
        cc = connect(host, port)
        cc.wait(timeout=15)
        print(f"  name       : {cc.name}")
        print(f"  model      : {cc.model_name}   uuid={cc.uuid}")
        out.update(name=cc.name, model=cc.model_name, uuid=str(cc.uuid))
        cs = cc.status
        if cs:
            print(f"  receiver   : app={cs.display_name!r} app_id={cs.app_id} text={cs.status_text!r}")
            print(f"  volume     : level={cs.volume_level} muted={cs.volume_muted}")
            out.update(app_id=cs.app_id, app_name=cs.display_name,
                       volume=cs.volume_level, status_text=cs.status_text)
        ms = cc.media_controller.status
        print(f"  media      : state={ms.player_state} idle={ms.idle_reason} "
              f"time={ms.current_time} dur={ms.duration}")
        print(f"  content_id : {(ms.content_id or '')[:100]}")
        # media_info attribute name varies across pychromecast versions
        mi = getattr(ms, "media_info", None) or getattr(ms, "media_information", None)
        ct = getattr(mi, "content_type", None) if mi else None
        mt = getattr(mi, "media_type", None) if mi else None
        print(f"  content_type: {ct}  media_type: {mt}  session={ms.media_session_id}")
        out["media"] = {
            "player_state": ms.player_state,
            "current_time": ms.current_time,
            "duration": ms.duration,
            "content_id": ms.content_id,
            "content_type": ct,
            "media_type": mt,
            "media_session_id": ms.media_session_id,
            "supported_media_commands": ms.supported_media_commands,
        }
        try:
            cc.disconnect(timeout=5)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"  !! {type(exc).__name__}: {exc}")
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


if __name__ == "__main__":
    res = [probe(*t) for t in TARGETS]
    with open("evidence/03-cast-state-by-host.json", "w") as fh:
        json.dump(res, fh, indent=2)
    print("\nsaved evidence/03-cast-state-by-host.json")
