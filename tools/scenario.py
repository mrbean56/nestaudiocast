#!/usr/bin/env python3
"""End-to-end scenario: the relay runs on CT 121, this script plays the phone's role.

Timeline (seconds from start), every action logged to evidence/21-scenario-events.jsonl:
  play video 1 + queue video 2 (autoplay), pause/resume, seek, TV volume, kitchen volume,
  video 1 ends -> autoplay to video 2, video 2 plays 10 min untouched (drift),
  stop (session end), new session, stop.
Volumes are lowered for the run (digital test) and restored exactly at the end.
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
from castv2 import CastLink  # noqa: E402

PY = os.path.join(ROOT, ".venv", "bin", "python")
EV = os.path.join(ROOT, "evidence", "21-scenario-events.jsonl" if (sys.argv[1:] or ["a"])[0] == "a"
                  else "%s-scenario-%s-events.jsonl" % ({"b": "22", "c": "24", "d": "26", "e": "29"}[sys.argv[1]], sys.argv[1]))
V1, V2 = "QzomK1fdSUg", "K0uxjdcZWyk"
POP = "dQw4w9WgXcQ"   # ordinary popular music video: ads possible, audio not cached
TV = "192.168.2.80"
KITCHEN = {"Cuisine Fenetre Vert": "192.168.2.61", "Cuisine Frigo Orange": "192.168.2.62",
           "Cuisine Frigo Jaune": "192.168.2.63", "Cuisine Meuble Bleu": "192.168.2.64",
           "Cuisine Meuble Rose": "192.168.2.65"}
T0 = time.time()
SCEN = sys.argv[1] if len(sys.argv) > 1 else "a"


def log(ev, **kw):
    row = {"wall": round(time.time(), 3), "rel": round(time.time() - T0, 1), "ev": ev, **kw}
    with open(EV, "a") as f:
        f.write(json.dumps(row) + "\n")
    print(json.dumps(row)[:200], flush=True)


def tv(*args):
    r = subprocess.run([PY, os.path.join(ROOT, "tools", "yt_tv.py"), *args],
                       capture_output=True, text=True, timeout=60)
    out = (r.stdout.strip().splitlines() or [""])[-1]
    log("tv_cmd", args=list(args), out=out[:200], rc=r.returncode)
    return out


def vol(host, level=None):
    link = CastLink(host)
    link.connect_platform()
    try:
        if level is not None:
            link.set_receiver_volume(level=level)
            time.sleep(0.3)
        return (link.receiver_status().get("volume") or {}).get("level")
    finally:
        link.close()


def at(rel):
    d = T0 + rel - time.time()
    if d > 0:
        time.sleep(d)


def main():
    saved = {n: vol(h) for n, h in KITCHEN.items()}
    saved_tv = vol(TV)
    log("volumes_saved", kitchen=saved, tv=saved_tv)
    try:
        for h in KITCHEN.values():
            vol(h, 0.05)
        log("kitchen_lowered", level=0.05)
        if SCEN == "e":     # untouched session: 11 min of one test card, no seeking at all
            at(8); tv("play", V2)
            vol(TV, 0.15); log("tv_lowered", level=0.15)
            at(680); tv("status")
            at(700); tv("stop")
            at(710)
            return
        if SCEN == "d":     # landing test: every TV seek forces a fresh landing
            at(8); tv("play", V2)
            vol(TV, 0.15); log("tv_lowered", level=0.15)
            for k, pos in enumerate((100, 300, 200, 450, 50, 350)):
                at(40 + 50 * k); tv("seek", str(pos))
            at(350); tv("stop")
            at(360)
            return
        if SCEN == "c":
            at(8); tv("play", POP)
            time.sleep(4); tv("queue", V1)
            vol(TV, 0.15); log("tv_lowered", level=0.15)
            at(300); tv("status")
            at(600); tv("status")
            at(900); tv("status")
            at(990); tv("stop")
            at(1010)
            return
        if SCEN == "b":
            at(8); tv("play", V1)
            time.sleep(4); tv("queue", V2)
            vol(TV, 0.15); log("tv_lowered", level=0.15)
            at(40); tv("pause")
            at(55); tv("resume")
            at(85); tv("seek", "570")      # video 1 ends ~30 s later -> autoplay to video 2
            # video 2: 600 s untouched (drift), then YouTube autoplays a recommendation
            at(800); tv("status")
            at(830); tv("status"); tv("stop")
            at(860); tv("play", V1)
            at(920); tv("status"); tv("stop")
            at(945)
            return
        at(8)
        tv("play", V1)
        time.sleep(4)
        tv("queue", V2)
        vol(TV, 0.15)
        log("tv_lowered", level=0.15)
        at(70); tv("pause")
        at(85); tv("resume")
        at(130); tv("seek", "250")
        at(170); vol(TV, 0.10); log("tv_volume", level=0.10)
        at(185); vol(TV, 0.15); log("tv_volume", level=0.15)
        at(200)
        for h in KITCHEN.values():
            vol(h, 0.03)
        log("kitchen_volume", level=0.03)
        at(215)
        for h in KITCHEN.values():
            vol(h, 0.05)
        log("kitchen_volume", level=0.05)
        # video 1 runs out (~t=250+), autoplay should bring video 2; then 10 min untouched
        at(1150); tv("status")
        tv("stop")
        at(1180); tv("play", V1)
        at(1240); tv("status")
        tv("stop")
        at(1260)
    finally:
        for n, h in KITCHEN.items():
            vol(h, saved[n])
        vol(TV, saved_tv)
        log("volumes_restored", kitchen={n: round(vol(h), 4) for n, h in KITCHEN.items()},
            tv=vol(TV))


if __name__ == "__main__":
    main()
