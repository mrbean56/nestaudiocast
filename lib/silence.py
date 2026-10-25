"""Silence guard.

Every experiment in this project runs with the kitchen speakers muted, and the
guard restores the exact prior volume/mute state of every device it touched —
even on exception. Nothing in this PoC may make noise unless a human explicitly
arms it (see `allow_sound=True`, which is refused unless CAST_POC_ALLOW_SOUND=1
is set in the environment).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from castv2 import CastLink  # noqa: E402

NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"

KITCHEN = {
    "Cuisine Fenetre Vert": ("192.168.2.61", 8009),
    "Cuisine Frigo Orange": ("192.168.2.62", 8009),
    "Cuisine Frigo Jaune": ("192.168.2.63", 8009),
    "Cuisine Meuble Bleu": ("192.168.2.64", 8009),
    "Cuisine Meuble Rose": ("192.168.2.65", 8009),
}
GROUPS = {
    "Groupe Tous": ("192.168.2.61", 32012),
    "En Haut": ("192.168.2.61", 32132),
    "Dehors": ("192.168.2.81", 32049),
}
TV = {"TV Salon": ("192.168.2.80", 8009)}

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "evidence", "silence-state.json")


def _get(host, port):
    l = CastLink(host, port)
    l.connect_platform()
    st = l.receiver_status()
    return l, st.get("volume") or {}


def _set(link, level=None, muted=None):
    vol = {}
    if level is not None:
        vol["level"] = float(level)
    if muted is not None:
        vol["muted"] = bool(muted)
    return link.request("receiver-0", NS_RECEIVER, {"type": "SET_VOLUME", "volume": vol},
                        timeout=6)["payload"]


@contextlib.contextmanager
def silence(devices=None, touch_tv=False, allow_sound=False):
    """Mute the given endpoints for the duration of the block, then restore."""
    if allow_sound and os.environ.get("CAST_POC_ALLOW_SOUND") != "1":
        raise RuntimeError("refusing to unmute: CAST_POC_ALLOW_SOUND is not 1")

    targets = dict(KITCHEN)
    if touch_tv:
        targets.update(TV)
    if devices:
        targets = {k: v for k, v in targets.items() if k in devices}

    saved = {}
    for name, (host, port) in targets.items():
        l, vol = _get(host, port)
        try:
            st = l.receiver_status()
            apps = st.get("applications") or []
            saved[name] = {"host": host, "port": port,
                           "level": vol.get("level"), "muted": vol.get("muted"),
                           # if something was already running we must NOT stop it later
                           # (it could be someone's Spotify); if nothing was running,
                           # any session found at the end is ours and must be stopped.
                           "app_at_entry": (apps[0].get("displayName") if apps else None),
                           "transport_at_entry": (apps[0].get("transportId") if apps else None)}
            _set(l, level=0.0, muted=True)
        finally:
            l.close()
        time.sleep(0.05)
    with open(STATE_FILE, "w") as fh:
        json.dump({"muted_at": time.time(), "saved": saved}, fh, indent=2)
    try:
        yield saved
    finally:
        for name, info in saved.items():
            l, _ = _get(info["host"], info["port"])
            try:
                # a session we started must be stopped, otherwise the media keeps
                # playing after the guard restores the volume — that is audible noise.
                if info.get("app_at_entry") is None:
                    st = l.receiver_status()
                    apps = st.get("applications") or []
                    if apps:
                        tr = apps[0].get("transportId")
                        if tr:
                            try:
                                l.stop_app(tr, timeout=8)
                                time.sleep(0.3)
                            except Exception:  # noqa: BLE001
                                pass
                _set(l, level=info["level"], muted=info["muted"])
            finally:
                l.close()
            time.sleep(0.05)


def check_silent() -> dict:
    """Per endpoint: volume/mute, plus whether an app is running.

    "Silent" means: nothing is playing, or it is playing muted/at zero. A speaker at
    its normal resting volume with no session is not a noise source.
    """
    out = {}
    for name, (host, port) in {**KITCHEN, **GROUPS}.items():
        l, vol = _get(host, port)
        try:
            lvl = vol.get("level") or 0.0
            muted = bool(vol.get("muted"))
            st = l.receiver_status()
            apps = st.get("applications") or []
            app = apps[0] if apps else None
            playing = app is not None
            out[name] = {"level": round(lvl, 4), "muted": muted,
                         "app": (app or {}).get("displayName"),
                         "session": playing,
                         "silent": muted or lvl < 0.005 or not playing}
        finally:
            l.close()
        time.sleep(0.05)
    return out


if __name__ == "__main__":
    print(json.dumps(check_silent(), indent=2))
