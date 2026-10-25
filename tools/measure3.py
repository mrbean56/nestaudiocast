#!/usr/bin/env python3
"""Three acoustic points inside one >=10 min session, at the CURRENT deployed settings.

Records at +60 s, +240 s and +450 s of a 600 s test card (the last window ends at 510 s, so it
never straddles the end), analyses each, prints the offsets and the drift fit, then puts the
service back. Unbuffered output so it can be watched live.

  tools/measure3.py [--delta 0.265] [--tag v015b]
"""
import argparse
import json
import statistics as st
import subprocess
import sys
import time

ROOT = "/home/mrbean/opencode/nestaudiocast"
PY = ROOT + "/.venv/bin/python"
CT = "root@192.168.2.21"
VIDEO = "QzomK1fdSUg"
POINTS = (60, 240, 450)      # seconds after the video starts
REC_S = 60


def sh(cmd, timeout=180):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delta", type=float, default=0.265)
    ap.add_argument("--tag", default="v015b")
    ap.add_argument("--phone", default="192.168.2.148:39509")
    a = ap.parse_args()
    log = f"evidence/23-relay-acoustic-{a.tag}.jsonl"
    print(sh(f"ssh {CT} 'systemctl stop cast-relay'").stdout.strip()[:80], flush=True)
    time.sleep(2)
    sh(f"ssh {CT} 'cd /opt/castrelay && (setsid nohup .venv/bin/python -u relay/cast_relay.py "
       f"--group Cuisine --measure-tracks --delta {a.delta} --duration 620 --log {log} "
       f"> logs/relay-m3.out 2>&1 < /dev/null &)'")
    time.sleep(6)
    print("play:", sh(f"{PY} tools/yt_tv.py play {VIDEO}").stdout.strip()[-90:], flush=True)
    t0 = time.time()
    outs = []
    for n, at in enumerate(POINTS, 1):
        wait = t0 + at - time.time()
        if wait > 0:
            time.sleep(wait)
        wav = f"{ROOT}/evidence/audio/23-acoustic-{a.tag}-{n}.wav"
        r = sh(f"cd {ROOT} && tools/phone_record.sh {a.phone} {REC_S} {wav}", timeout=REC_S + 60)
        out = f"{ROOT}/evidence/23-acoustic-{a.tag}-{n}.json"
        an = sh(f"cd {ROOT} && {PY} measure/analyze_offset.py {wav} --tv-freq 350 --grp-freq 2310 "
                f"--period 1.0 --expect-ms 0 --json {out}", timeout=300)
        d = {}
        try:
            d = json.load(open(out))
        except Exception:  # noqa: BLE001
            d = {"verdict": an.stdout[-120:]}
        rec = {"n": n, "at_s": at, "offset_ms": d.get("offset_median_ms"),
               "sd_ms": d.get("offset_stdev_ms"), "kept": d.get("pairs_kept"),
               "rejected": d.get("pairs_rejected"), "verdict": (d.get("verdict") or "")[:70],
               "rec_rc": r.returncode}
        outs.append(rec)
        print(json.dumps(rec), flush=True)
    sh(f"{PY} tools/yt_tv.py stop")
    sh(f"ssh {CT} 'kill $(pgrep -f \"^.venv/bin/python -u relay/cast_relay.py --group Cuisine --measure-tracks\") 2>/dev/null; systemctl start cast-relay'")
    ys = [o["offset_ms"] for o in outs if o["offset_ms"] is not None]
    res = {"when": time.strftime("%Y-%m-%d %H:%M"), "delta": a.delta,
           "land_tol_ms": 30, "points": outs}
    if len(ys) >= 3:
        ts = [o["at_s"] / 60 for o in outs if o["offset_ms"] is not None]
        mt, my = st.mean(ts), st.mean(ys)
        sxx = sum((t - mt) ** 2 for t in ts)
        sl = sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / sxx
        ic = my - sl * mt
        resid = [y - (sl * t + ic) for t, y in zip(ts, ys)]
        s2 = sum(r * r for r in resid) / (len(ts) - 2)
        se = (s2 / sxx) ** 0.5
        res.update({"max_abs_ms": max(map(abs, ys)), "trend_ms_per_10min": round(sl * 10, 1),
                    "trend_sigma_ms_per_10min": round(se * 10, 1)})
        print(f"\noffsets {ys} ms -> max |offset| {max(map(abs, ys)):.0f} ms, "
              f"trend {sl * 10:+.1f} +- {se * 10:.1f} ms/10 min", flush=True)
    json.dump(res, open(f"{ROOT}/evidence/23-acoustic-{a.tag}-summary.json", "w"), indent=1)
    print("saved summary", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
