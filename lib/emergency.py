#!/usr/bin/env python3
"""Panic stop — the one command to run when something is wrong.

Order matters: silence first, then stop our own processes, then clean sessions.
It never stops an app on a group that contains the TV (that would cut into
someone else's session); on such groups it only mutes.

    .venv/bin/python lib/emergency.py            # silence + stop
    .venv/bin/python lib/emergency.py --restore  # put back saved volumes
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from castv2 import CastLink, receiver_app  # noqa: E402
import groupguard  # noqa: E402
from silence import KITCHEN, GROUPS, STATE_FILE  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIDFILES = ["logs/relay.pid", "logs/virtual-receiver.pid"]


def stop_our_processes() -> list[str]:
    stopped = []
    for pidf in PIDFILES:
        p = os.path.join(ROOT, pidf)
        if not os.path.exists(p):
            continue
        try:
            pid = int(open(p).read().strip())
        except ValueError:
            os.unlink(p)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(f"{os.path.basename(pidf)} pid={pid}")
        except ProcessLookupError:
            stopped.append(f"{os.path.basename(pidf)} stale")
        finally:
            try:
                os.unlink(p)
            except OSError:
                pass
    return stopped


def mute_everything() -> dict:
    """Silence individual speakers only.

    Deliberately does NOT touch cast groups: every group here contains the TV, and
    SET_VOLUME on a group propagates to its members — i.e. it would change the living-room
    TV's volume. (This was a real flaw in the first version of this script.)
    """
    out = {}
    # Save the current levels first, so that --restore puts back what was there just now
    # (it used to restore whatever an older silence() had recorded).
    saved = {}
    for name, (host, port) in KITCHEN.items():
        try:
            l = CastLink(host, port)
            try:
                l.connect_platform()
                v = l.receiver_status().get("volume") or {}
                saved[name] = {"host": host, "port": port, "level": v.get("level"),
                               "muted": v.get("muted")}
            finally:
                l.close()
        except Exception:  # noqa: BLE001
            pass
    if saved and not any(x["level"] == 0.0 and x["muted"] for x in saved.values()):
        json.dump({"saved": saved, "t": time.time(), "by": "emergency"}, open(STATE_FILE, "w"))
    for name, (host, port) in KITCHEN.items():
        try:
            l = CastLink(host, port)
            try:
                l.connect_platform()
                # Mute only: keep the level so --restore and the volume mirror still work.
                # (A zeroed level made the kitchen silent while the group played at 0.4.)
                l.set_receiver_volume(muted=True)
                out[name] = "muted"
            finally:
                l.close()
        except Exception as exc:  # noqa: BLE001
            out[name] = f"error: {type(exc).__name__}"
        time.sleep(0.05)
    out["_groups"] = "not touched: they contain the TV (see notes/00-labo.md A6)"
    return out


def clean_sessions() -> dict:
    """Stop a running receiver app, but only on groups that pass the safety guard."""
    out = {}
    try:
        groups = groupguard.discover_groups(timeout=4) or dict(GROUPS)
    except Exception:  # noqa: BLE001
        groups = dict(GROUPS)
    for name, (host, port) in groups.items():
        try:
            ok, members = groupguard.is_safe(host, port)
            l = CastLink(host, port)
            try:
                l.connect_platform()
                st = l.receiver_status()
                app_id, app_name, tr = receiver_app(st)
                if not tr:
                    out[name] = "idle"
                elif not ok:
                    out[name] = f"left alone (contains TV): {app_name}"
                else:
                    l.stop_app(tr)
                    out[name] = f"stopped {app_name}"
            finally:
                l.close()
        except Exception as exc:  # noqa: BLE001
            out[name] = f"error: {type(exc).__name__}"
        time.sleep(0.05)
    return out


def restore() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"error": "no saved state"}
    saved = json.load(open(STATE_FILE))["saved"]
    out = {}
    for name, info in saved.items():
        try:
            l = CastLink(info["host"], info["port"])
            try:
                l.connect_platform()
                l.set_receiver_volume(level=info["level"], muted=info["muted"])
                out[name] = {"level": info["level"], "muted": info["muted"]}
            finally:
                l.close()
        except Exception as exc:  # noqa: BLE001
            out[name] = f"error: {type(exc).__name__}"
        time.sleep(0.05)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args()
    if a.restore:
        print(json.dumps({"restored": restore()}, indent=2, ensure_ascii=False))
        sys.exit(0)
    res = {"processes": stop_our_processes(), "muted": mute_everything(),
           "sessions": clean_sessions()}
    print(json.dumps(res, indent=2, ensure_ascii=False))
