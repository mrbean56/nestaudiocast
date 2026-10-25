#!/usr/bin/env python3
"""Regenerate the group-membership evidence (evidence/09).

This script exists because the first version of that evidence was produced by an inline
command that was never committed (cross-review §6.2): the most safety-critical file in the
project was not reproducible.

It is read-only: `MULTIZONE_STATUS` returns the member list of a group without starting any
session, so nothing is played and nobody is disturbed.

    .venv/bin/python probe/group_membership.py
"""
import json
import sys
import time

sys.path.insert(0, "lib")
from castv2 import CastLink, NS_MULTIZONE  # noqa: E402
import groupguard  # noqa: E402

SPEAKER_CAPS = 196612


def main():
    groups = groupguard.discover_groups()
    out = {}
    print(f"{len(groups)} group(s) found\n")
    for label, (host, port) in groups.items():
        l = CastLink(host, port)
        try:
            l.connect_platform()
            st = l.request("receiver-0", NS_MULTIZONE, {"type": "GET_STATUS"},
                           timeout=5)["payload"]["status"]
            devices = [{"name": d["name"].strip(), "id": d["deviceId"],
                        "caps": d["capabilities"],
                        "kind": "speaker" if d["capabilities"] == SPEAKER_CAPS else "VIDEO/other"}
                       for d in st.get("devices", [])]
            out[label] = {"endpoint": f"{host}:{port}",
                          "isVideoGroup": st.get("isVideoGroup"),
                          "safe_audio_only": bool(devices) and all(
                              d["caps"] == SPEAKER_CAPS for d in devices),
                          "devices": devices}
            print(f"=== {label}  ({host}:{port})  n={len(devices)}  "
                  f"safe={out[label]['safe_audio_only']}")
            for d in devices:
                print(f"   {d['kind']:12s} {d['name']:28s} {d['id']}  caps={d['caps']}")
        except Exception as exc:  # noqa: BLE001
            out[label] = {"endpoint": f"{host}:{port}", "error": f"{type(exc).__name__}: {exc}"}
            print(f"=== {label}: ERROR {out[label]['error']}")
        finally:
            l.close()
        time.sleep(0.2)
    with open("evidence/09-group-membership-multizone.json", "w") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print("\nsaved evidence/09-group-membership-multizone.json")


if __name__ == "__main__":
    main()
