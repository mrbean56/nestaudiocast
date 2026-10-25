#!/usr/bin/env python3
"""Rollback test with archived values (notes/rollback.md, levels 0 and 1).

before  : volumes/mute of the 5 kitchen speakers, groups, TV; sessions
play    : relay on CT 121 + YouTube on the TV for ~15 s (real levels: it is the real case)
level 0 : stop relay on CT, lib/emergency.py, stop the test video on the TV
check   : nothing plays anywhere
level 1 : lib/emergency.py --restore
after   : same snapshot; PASS iff every volume/mute equals `before`
-> evidence/28-rollback-test.json
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
from castv2 import CastLink, receiver_app  # noqa: E402

PY = os.path.join(ROOT, ".venv", "bin", "python")
POINTS = {"Cuisine Fenetre Vert": ("192.168.2.61", 8009), "Cuisine Frigo Orange": ("192.168.2.62", 8009),
          "Cuisine Frigo Jaune": ("192.168.2.63", 8009), "Cuisine Meuble Bleu": ("192.168.2.64", 8009),
          "Cuisine Meuble Rose": ("192.168.2.65", 8009), "TV Salon": ("192.168.2.80", 8009)}


def snap():
    out = {}
    for name, (h, p) in POINTS.items():
        l = CastLink(h, p)
        try:
            l.connect_platform()
            st = l.receiver_status()
            v = st.get("volume") or {}
            out[name] = {"level": round(v.get("level") or 0, 4), "muted": v.get("muted"),
                         "app": receiver_app(st)[1]}
        finally:
            l.close()
    import groupguard
    g = groupguard.discover_groups(timeout=4)
    if "Cuisine" in g:
        l = CastLink(*g["Cuisine"])
        try:
            l.connect_platform()
            out["group Cuisine"] = {"app": receiver_app(l.receiver_status())[1]}
        finally:
            l.close()
    return out


def run(cmd, timeout=60):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    return (r.stdout + r.stderr).strip()[-600:]


def main():
    rep = {"t": time.strftime("%Y-%m-%d %H:%M:%S")}
    rep["before"] = snap()
    run("ssh root@192.168.2.21 'cd /opt/castrelay && (setsid nohup .venv/bin/python -u "
        "relay/cast_relay.py --group Cuisine --duration 300 --log evidence/28-relay-rollback.jsonl "
        "> logs/relay-rb.out 2>&1 < /dev/null &)'")
    time.sleep(6)
    run(f"{PY} tools/yt_tv.py play QzomK1fdSUg")
    time.sleep(22)
    rep["during"] = snap()
    t0 = time.time()
    rep["level0"] = {
        "ct_relay": run("ssh root@192.168.2.21 'systemctl stop cast-relay; "
                        "pkill -f \"^.venv/bin/python -u relay\"; sleep 1; "
                        "pgrep -af \"^.venv/bin/python -u relay\" || echo stopped'"),
        "emergency": run(f"{PY} lib/emergency.py"),
        "tv": run(f"{PY} tools/yt_tv.py stop"),
    }
    rep["level0_s"] = round(time.time() - t0, 1)
    time.sleep(2)
    rep["after_level0"] = snap()
    rep["silent_after_level0"] = all(
        (v.get("app") in (None, "Backdrop")) or (v.get("muted") and k != "TV Salon")
        for k, v in rep["after_level0"].items())
    rep["level1"] = run(f"{PY} lib/emergency.py --restore")
    time.sleep(1)
    rep["after"] = snap()
    diff = {k: (rep["before"][k], rep["after"][k]) for k in POINTS
            if (rep["before"][k]["level"], rep["before"][k]["muted"]) !=
            (rep["after"][k]["level"], rep["after"][k]["muted"])}
    rep["volume_diffs"] = diff
    rep["PASS"] = rep["silent_after_level0"] and not diff
    json.dump(rep, open(os.path.join(ROOT, "evidence", "28-rollback-test.json"), "w"),
              indent=1, ensure_ascii=False)
    print(json.dumps({k: rep[k] for k in ("PASS", "silent_after_level0", "volume_diffs",
                                          "level0_s")}, ensure_ascii=False))
    print(json.dumps({"before": rep["before"], "during": rep["during"],
                      "after_level0": rep["after_level0"], "after": rep["after"]},
                     ensure_ascii=False)[:1500])


if __name__ == "__main__":
    main()
