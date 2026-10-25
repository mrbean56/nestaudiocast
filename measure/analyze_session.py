#!/usr/bin/env python3
"""Digital sync report for a relay session log (+ optional scenario event log).

  analyze_session.py RELAY.jsonl [EVENTS.jsonl] [--json OUT]

Digital error = group playhead - filtered TV playhead (- delta), from the relay's samples.
This is NOT the acoustic offset; it is the part the relay controls. The acoustic offset is
digital error + (group output latency - TV output latency), a constant measured separately
(notes/measurement-protocol.md).

Reports: per-minute error stats, linear drift over the longest uninterrupted stretch,
every relay event, and for each scenario action the time until the kitchen is back in sync.
"""
import argparse
import json

import numpy as np


def load(path):
    return [json.loads(line) for line in open(path) if line.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("relay")
    ap.add_argument("events", nargs="?")
    ap.add_argument("--json")
    a = ap.parse_args()
    rows = load(a.relay)
    smp = [r for r in rows if r.get("ev") == "sample" and r.get("err_ms") is not None]
    ok = [r for r in smp if r["action"] in ("in_sync", "hold")]
    out = {"samples": len(smp), "in_sync": len(ok)}
    print(f"samples with error: {len(smp)}, in_sync: {len(ok)}")
    acts = {}
    for r in rows:
        if r.get("ev") == "sample":
            k = r["action"].split(" ")[0]
            acts[k] = acts.get(k, 0) + 1
    print("actions:", acts)
    out["actions"] = acts

    # stretches: consecutive in_sync samples on one content without a gap > 5 s
    stretches, cur = [], []
    for r in ok:
        if cur and (r["wall"] - cur[-1]["wall"] > 5 or r["content"] != cur[-1]["content"]):
            stretches.append(cur)
            cur = []
        cur.append(r)
    if cur:
        stretches.append(cur)
    out["stretches"] = []
    for st in stretches:
        w = np.array([r["wall"] for r in st])
        e = np.array([r["err_ms"] for r in st])
        if w[-1] - w[0] < 30:
            continue
        slope, icpt = np.polyfit(w - w[0], e, 1)
        res = e - (slope * (w - w[0]) + icpt)
        n = len(e)
        se = res.std() / (np.sqrt(n) * (w - w[0]).std()) if n > 2 else float("nan")
        info = {"content": st[0]["content"], "start": w[0], "dur_s": round(w[-1] - w[0], 1),
                "n": n, "mean_ms": round(e.mean(), 1), "std_ms": round(e.std(), 1),
                "p95_abs_ms": round(np.percentile(np.abs(e), 95), 1),
                "max_abs_ms": round(np.abs(e).max(), 1),
                "drift_ms_per_10min": round(slope * 600, 1),
                "drift_sigma_ms_per_10min": round(se * 600, 1)}
        mins = []
        for k in range(int((w[-1] - w[0]) // 60) + 1):
            m = (w - w[0] >= 60 * k) & (w - w[0] < 60 * (k + 1))
            if m.sum() >= 5:
                mins.append((k, round(float(e[m].mean()), 1), round(float(e[m].std()), 1)))
        info["per_minute"] = mins
        out["stretches"].append(info)
        print(f"\nstretch {info['content']} {info['dur_s']} s n={n}: mean {info['mean_ms']} "
              f"std {info['std_ms']} p95|e| {info['p95_abs_ms']} max|e| {info['max_abs_ms']} ms; "
              f"drift {info['drift_ms_per_10min']} +- {info['drift_sigma_ms_per_10min']} ms/10min")
        print("  per minute (min, mean, std):", mins)

    print("\nrelay events:")
    evs = [r for r in rows if r.get("ev") not in ("sample",)]
    for r in evs:
        print("  ", json.dumps(r)[:170])

    if a.events:
        print("\nscenario actions -> back in sync:")
        out["recovery"] = []
        for e in load(a.events):
            if e["ev"] not in ("tv_cmd", "tv_volume", "kitchen_volume"):
                continue
            after = [r for r in ok if r["wall"] > e["wall"]]
            first = None
            for i in range(len(after) - 4):
                if all(abs(x["err_ms"]) < 50 for x in after[i:i + 5]):
                    first = after[i]
                    break
            rec = {"rel": e["rel"], "action": e.get("args") or e["ev"],
                   "back_in_sync_s": None if first is None else round(first["wall"] - e["wall"], 1)}
            out["recovery"].append(rec)
            print("  ", rec)
    if a.json:
        json.dump(out, open(a.json, "w"), indent=1)
        print("saved", a.json)


if __name__ == "__main__":
    main()
