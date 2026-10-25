#!/usr/bin/env python3
"""Transport matrix against the *deployed service* (not a manual relay run).

Verifies, one by one, what the objective requires: play, pause, resume, seek forward,
seek back, TV volume (mirrored to the group), stop, and a brand-new session — reading the
TV side (YouTube media status), the Nest side (the group's receiver state) and the relay's
own digital error from the service log.

  tools/transport_matrix.py [--tv-vol 0.15]

Writes evidence/35-transport-matrix.json. Does not start or stop the relay: it must already
be running (systemd), which is the point.
"""
import argparse
import glob
import json
import subprocess
import sys
import time

ROOT = "/home/mrbean/opencode/nestaudiocast"
sys.path.insert(0, ROOT + "/lib")
sys.path.insert(0, ROOT + "/tools")
from castv2 import CastLink, receiver_app  # noqa: E402
import groupguard  # noqa: E402

PY = ROOT + "/.venv/bin/python"
CT = "root@192.168.2.21"
V1, V2 = "K0uxjdcZWyk", "QzomK1fdSUg"
results = []


def sh(cmd, timeout=90):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def tv_status():
    r = sh(f"{PY} tools/yt_tv.py status")
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {"raw": r.stdout[-200:]}


def group_state():
    g = groupguard.discover_groups().get("Cuisine")
    if not g:
        return {"app": None, "error": "group not found"}
    l = CastLink(*g)
    try:
        l.connect_platform()
        st = l.receiver_status()
        app = (st.get("applications") or [{}])[0]
        out = {"app": app.get("displayName"), "volume": round((st.get("volume") or {}).get("level") or 0, 3)}
        tr = app.get("transportId")
        if tr:
            l.connect_transport(tr)
            time.sleep(0.15)
            ms = (l.media_status(tr, timeout=3).get("status") or [{}])[0]
            out["state"] = ms.get("playerState")
            out["t"] = ms.get("currentTime")
        return out
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:80]}
    finally:
        l.close()


def service_log():
    r = sh(f"ssh {CT} 'ls -t /opt/castrelay/evidence/relay-*.jsonl | head -1'")
    return r.stdout.strip().splitlines()[-1]


def wait_for(pred, timeout, step=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = group_state()
        if pred(s):
            return round(time.time() - t0, 1), s
        time.sleep(step)
    return None, group_state()


def digital_error(log, since):
    r = sh(f"ssh {CT} \"awk -v s={since} '{{}}' /dev/null; grep '\\\"ev\\\": \\\"sample\\\"' {log} | "
           f"tail -200\"")
    vals = []
    for line in r.stdout.strip().splitlines():
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if d.get("err_ms") is not None and d.get("wall", 0) >= since:
            vals.append(d["err_ms"])
    if not vals:
        return None
    vals.sort()
    return round(vals[len(vals) // 2], 1)


def record(name, ok, detail):
    results.append({"check": name, "pass": bool(ok), **detail})
    print(("PASS " if ok else "FAIL ") + name + "  " + json.dumps(detail)[:220], flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tv-vol", type=float, default=0.15)
    a = ap.parse_args()
    log = service_log()
    print("service log:", log, flush=True)
    tv0 = tv_status()
    vol0 = tv0.get("volume")
    print("TV before:", json.dumps(tv0)[:160], flush=True)
    try:
        # 1. play
        sh(f"{PY} tools/yt_tv.py volume {a.tv_vol}")
        sh(f"{PY} tools/yt_tv.py play {V1}")
        t, s = wait_for(lambda s: s.get("state") in ("PLAYING", "BUFFERING"), 45)
        record("play: kitchen joins", t is not None, {"seconds": t, "group": s})
        time.sleep(6)
        st = tv_status()
        record("play: TV plays the same video", st.get("content") == V1 and st.get("state") == "PLAYING",
               {"tv": {k: st.get(k) for k in ("state", "content", "t")}})

        # 2. pause
        sh(f"{PY} tools/yt_tv.py pause")
        t, s = wait_for(lambda s: s.get("state") == "PAUSED", 12)
        record("pause: kitchen pauses", t is not None, {"seconds": t, "group": s})

        # 3. resume
        t0 = time.time()
        sh(f"{PY} tools/yt_tv.py resume")
        t, s = wait_for(lambda s: s.get("state") == "PLAYING", 25)
        time.sleep(12)
        record("resume: kitchen resumes", t is not None, {"seconds": t, "group": s,
                                                          "digital_err_ms": digital_error(log, t0)})

        # 4. seek forward
        t0 = time.time()
        sh(f"{PY} tools/yt_tv.py seek 420")
        t, s = wait_for(lambda s: s.get("state") == "PLAYING" and (s.get("t") or 0) > 400, 40)
        time.sleep(10)
        record("seek +: kitchen follows", t is not None, {"seconds": t, "group": s,
                                                          "digital_err_ms": digital_error(log, t0)})

        # 5. seek back
        t0 = time.time()
        sh(f"{PY} tools/yt_tv.py seek 60")
        t, s = wait_for(lambda s: s.get("state") == "PLAYING" and 40 < (s.get("t") or 0) < 120, 40)
        time.sleep(10)
        record("seek -: kitchen follows", t is not None, {"seconds": t, "group": s,
                                                          "digital_err_ms": digital_error(log, t0)})

        # 6/7. volume mirroring while playing
        sh(f"{PY} tools/yt_tv.py volume 0.22")
        time.sleep(2.5)
        s = group_state()
        record("volume: group mirrors TV down", abs((s.get("volume") or 0) - 0.22) < 0.02, {"group": s})
        sh(f"{PY} tools/yt_tv.py volume 0.35")
        time.sleep(2.5)
        s = group_state()
        record("volume: group mirrors TV up", abs((s.get("volume") or 0) - 0.35) < 0.02, {"group": s})

        # 8. stop
        sh(f"{PY} tools/yt_tv.py stop")
        t, s = wait_for(lambda s: s.get("app") is None, 20)
        record("stop: kitchen stops with the TV", t is not None, {"seconds": t, "group": s})

        # 9. brand-new session
        sh(f"{PY} tools/yt_tv.py play {V2}")
        t, s = wait_for(lambda s: s.get("state") in ("PLAYING", "BUFFERING"), 45)
        record("new session: kitchen joins the new video", t is not None, {"seconds": t, "group": s})
        st = tv_status()
        record("new session: TV on the new video", st.get("content") == V2, {"tv": st.get("content")})
    finally:
        sh(f"{PY} tools/yt_tv.py stop")
        if vol0 is not None:
            sh(f"{PY} tools/yt_tv.py volume {vol0}")
        out = ROOT + "/evidence/35-transport-matrix.json"
        json.dump({"when": time.strftime("%Y-%m-%d %H:%M:%S"), "log": log,
                   "checks": results,
                   "passed": sum(1 for r in results if r["pass"]), "total": len(results)},
                  open(out, "w"), indent=1)
        print(f"\n{sum(1 for r in results if r['pass'])}/{len(results)} passed -> {out}")
    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
