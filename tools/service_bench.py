#!/usr/bin/env python3
"""End-to-end timings and transport, through the running service (silent bench).

Measures what the owner's acceptance talks about, on the **video side only** while the group is
deliberately never loaded (`video_only`): first frame after a share, pause, resume, seek, stop,
new share. The group half of each row needs an audible window and is reported as such — this tool
never touches the group.

    tools/service_bench.py --base http://192.168.2.21:7640 --json evidence/50-service-bench.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request

SHORT = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # 19 s
LONG = "https://www.youtube.com/watch?v=ai_gI0Jbr_E"  # 2 h 23


def call(base: str, path: str, body=None, timeout: float = 15.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path,
        data=data,
        method="POST" if data else "GET",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": (e.read() or b"").decode()[:200]}


def status(base: str) -> dict:
    return call(base, "/api/status")


def wait_for(base: str, predicate, timeout: float, step: float = 0.2) -> tuple[float, dict]:
    t0 = time.monotonic()
    last = {}
    while time.monotonic() - t0 < timeout:
        last = status(base)
        if predicate(last):
            return time.monotonic() - t0, last
        time.sleep(step)
    return -1.0, last


def playing(s: dict) -> bool:
    return s.get("state") == "playing" and (s.get("position_video") or 0) > 0.05


def bench_first_frame(base: str, url: str, runs: int = 3) -> dict:
    out = []
    for _ in range(runs):
        call(base, "/api/control", {"action": "stop"})
        time.sleep(1.5)
        reply = call(base, "/api/share", {"url": url})
        if "error" in reply:
            out.append({"ok": False, "why": reply})
            continue
        dt, s = wait_for(base, playing, timeout=60.0)
        out.append(
            {
                "ok": dt >= 0,
                "seconds": round(dt, 2) if dt >= 0 else None,
                "state": s.get("state"),
                "position": s.get("position_video"),
                "last_event": s.get("last_event"),
                "last_error": s.get("last_error"),
                "share_reply": reply.get("state"),
            }
        )
        call(base, "/api/control", {"action": "stop"})
    good = [r["seconds"] for r in out if r.get("ok")]
    return {
        "runs": out,
        "median_s": round(statistics.median(good), 2) if good else None,
        "max_s": max(good) if good else None,
        "criterion_s": 15.0,
        "verdict": ("PASS" if good and max(good) <= 15.0 else "FAIL" if good else "no run"),
    }


def bench_transport(base: str) -> dict:
    steps = []

    def record(name: str, ok: bool, dt: float | None, s: dict, note: str = "") -> None:
        steps.append(
            {
                "step": name,
                "ok": bool(ok),
                "seconds": None if dt is None else round(dt, 2),
                "state": s.get("state"),
                "position": s.get("position_video"),
                "note": note,
            }
        )

    call(base, "/api/control", {"action": "stop"})
    time.sleep(1.0)
    call(base, "/api/share", {"url": LONG})
    dt, s = wait_for(base, playing, 60.0)
    record("start", dt >= 0, dt, s)

    time.sleep(3.0)
    pause_reply = call(base, "/api/control", {"action": "pause"})
    dt, s = wait_for(base, lambda x: x.get("state") == "paused", 10.0, step=0.1)
    time.sleep(1.0)
    frozen = status(base).get("position_video") or 0.0
    time.sleep(1.0)
    still = status(base).get("position_video") or 0.0
    record(
        "pause",
        dt >= 0 and abs(still - frozen) < 0.2,
        dt,
        s,
        f"position {frozen:.2f} -> {still:.2f} while paused; reply {pause_reply}",
    )

    play_reply = call(base, "/api/control", {"action": "play"})
    dt, s = wait_for(base, playing, 20.0, step=0.1)
    record("resume", dt >= 0, dt, s, f"reply {play_reply}")

    # let it play a little, then seek forward
    wait_for(base, lambda x: (x.get("position_video") or 0) > 2.0, 30.0, step=0.2)
    seek_reply = call(base, "/api/control", {"action": "seek", "position": 600.0})
    dt, s = wait_for(
        base,
        lambda x: (
            (x.get("position_video") or 0) >= 599.5
            or (x.get("state") == "playing" and (x.get("position_video") or 0) > 599.0)
        ),
        40.0,
        step=0.2,
    )
    record("seek +600 s", dt >= 0, dt, s, f"reply {seek_reply}")

    stop_reply = call(base, "/api/control", {"action": "stop"})
    dt, s = wait_for(base, lambda x: x.get("state") in ("idle", "ended"), 12.0, step=0.2)
    record("stop", dt >= 0, dt, s, f"reply {stop_reply}")

    time.sleep(1.0)
    reply = call(base, "/api/share", {"url": SHORT})
    dt, s = wait_for(base, playing, 45.0)
    record("new share", dt >= 0 and "error" not in reply, dt, s)
    call(base, "/api/control", {"action": "stop"})

    return {
        "steps": steps,
        "passed": sum(1 for x in steps if x["ok"]),
        "total": len(steps),
        "group_side": "not measured: the bench runs video_only, the group is never loaded",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--base", default="http://192.168.2.21:7640")
    ap.add_argument("--json", default=None)
    ap.add_argument("--url", default=LONG)
    a = ap.parse_args(argv)

    out = {
        "task": "v0.2.0 service bench (silent: video_only, group untouched)",
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base": a.base,
        "status_before": status(a.base),
        "first_frame": bench_first_frame(a.base, a.url),
        "transport": bench_transport(a.base),
    }
    out["status_after"] = status(a.base)
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(out, fh, indent=1)
        print(f"saved {a.json}")
    print(json.dumps({k: out[k] for k in ("first_frame", "transport")}, indent=1))
    ok = (
        out["first_frame"]["verdict"] != "FAIL"
        and out["transport"]["passed"] == out["transport"]["total"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
