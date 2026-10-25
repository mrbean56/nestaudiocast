#!/usr/bin/env python3
"""Acoustic acceptance session (needs the phone's wireless-debugging IP:PORT).

  acoustic_session.py PHONE_IP:PORT [--delta 0.0] [--minutes 11]

1. starts the relay on CT 121 in measurement mode (--measure-tracks): the kitchen plays
   2310 Hz clicks at the media times of the video's own 350 Hz beeps;
2. plays QzomK1fdSUg (600 s) then K0uxjdcZWyk on the TV (one continuous YouTube session);
3. records 60 s through the phone microphone at minutes 1, 5 and 10;
4. analyses each recording (offset = kitchen click - TV beep, per beep) and writes
   evidence/23-acoustic-<n>.json plus a summary.
The phone must lie where it hears both the TV and the kitchen. Volumes are not changed:
the measurement needs real levels.
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")
CT = "root@192.168.2.21"


MINUTES = [1.5, 5.0, 8.5]


def stable_since(relay_log, secs):
    """True if the relay log (on the CT) shows no restart / content change in `secs`."""
    r = sh(f"ssh {CT} 'tail -40 /opt/castrelay/{relay_log} 2>/dev/null | "
           f"grep -E \"sink_started|content_changed\" | tail -1'")
    line = (r.stdout or "").strip()
    if not line:
        return False
    try:
        w = float(json.loads(line)["wall"])
    except Exception:  # noqa: BLE001
        return False
    return (time.time() - w) > secs


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phone")
    ap.add_argument("--delta", type=float, default=0.0)
    ap.add_argument("--minutes", type=float, default=11)
    ap.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M"))
    ap.add_argument("--minutes-at", default="1.5,5,8.5",
                    help="recording start times in minutes (default avoids video boundaries)")
    a = ap.parse_args()
    global MINUTES
    MINUTES = [float(x) for x in a.minutes_at.split(",")]
    dur = int(max(a.minutes, MINUTES[-1] + 2) * 60) + 60
    relay_log = f"evidence/23-relay-acoustic-{a.tag}.jsonl"
    sh(f"ssh {CT} 'cd /opt/castrelay && (setsid nohup .venv/bin/python -u relay/cast_relay.py "
       f"--group Cuisine --measure-tracks --delta {a.delta} --duration {dur} "
       f"--log {relay_log} > logs/relay-acoustic.out 2>&1 < /dev/null &)'", timeout=30)
    time.sleep(8)
    t0 = time.time()
    sh(f"{PY} tools/yt_tv.py play QzomK1fdSUg K0uxjdcZWyk", timeout=60)
    results = []
    for n, minute in enumerate(MINUTES, 1):
        # Only record on a settled relay: a re-lock or a content change inside the 60 s
        # window mixes two alignments and the dispersion explodes (2026-10-25: minute 1
        # straddled a re-lock, minute 10 straddled the video boundary).
        while True:
            wait = t0 + minute * 60 - time.time()
            if wait > 0:
                time.sleep(wait)
            if stable_since(relay_log, 12.0):
                break
            print(f"minute {minute}: relay not settled, waiting 20 s", flush=True)
            time.sleep(20)
            minute += 1 / 3
        wav = f"evidence/audio/23-acoustic-{a.tag}-{n}.wav"
        r = sh(f"tools/phone_record.sh {a.phone} 60 {wav}", timeout=120)
        out = f"evidence/23-acoustic-{a.tag}-{n}.json"
        an = sh(f"{PY} measure/analyze_offset.py {wav} --tv-freq 350 --grp-freq 2310 "
                f"--period 1.0 --json {out}", timeout=300)
        res = {"n": n, "minute": minute, "wav": wav, "record_rc": r.returncode,
               "analysis": an.stdout.strip().splitlines()[-3:]}
        results.append(res)
        print(json.dumps(res), flush=True)
    sh(f"{PY} tools/yt_tv.py stop", timeout=60)
    sh(f"scp -q {CT}:/opt/castrelay/{relay_log} evidence/", timeout=60)
    json.dump(results, open(f"evidence/23-acoustic-{a.tag}-summary.json", "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
