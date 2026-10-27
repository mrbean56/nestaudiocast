#!/usr/bin/env python3
"""A long, silent playback through the deployed service: does the video keep playing?

Review 2026-10-27 found that the MSE reader never refilled after the start, so no session had
played more than ~25 s. This tool is the device-side proof of the fix:

1. records the Chromecast's volume and **mutes** it (the level is never touched; restored at the
   end, whatever happens) — the TV may be on that input, and real YouTube media has sound;
2. refuses to run unless the service is in ``video_only`` (the group is never loaded);
3. shares ``--url``, then reads ``GET /api/status`` once a second for ``--seconds``;
4. reports how far the video advanced against the wall clock, every stall (no advance for
   more than 2 s while ``playing``), and the final state; then stops the session.

    tools/long_play.py --seconds 600 --json evidence/52-long-play.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from castv2 import CastLink  # noqa: E402

SERVICE = "http://192.168.2.21:7640"
CHROMECAST = "192.168.2.80"
LONG_VIDEO = "https://www.youtube.com/watch?v=ai_gI0Jbr_E"  # 2 h 23, the fixture's source


def call(path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(SERVICE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        return {"http_error": exc.code, "body": exc.read().decode(errors="replace")[:300]}


def device_volume() -> dict:
    link = CastLink(CHROMECAST)
    try:
        link.connect_platform()
        return dict(link.receiver_status().get("volume") or {})
    finally:
        link.close()


def set_muted(muted: bool) -> dict:
    link = CastLink(CHROMECAST)
    try:
        link.connect_platform()
        link.set_receiver_volume(muted=muted)
        return dict(link.receiver_status().get("volume") or {})
    finally:
        link.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default=LONG_VIDEO)
    ap.add_argument("--seconds", type=float, default=600.0)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)

    status = call("/api/status")
    quiet = status.get("quiet") or {}
    if quiet.get("decision") != "video_only":
        print(f"refusing: the service would load the group (quiet decision {quiet.get('decision')!r})")
        return 2

    before = device_volume()
    if "muted" not in before:
        print(f"refusing: cannot read the Chromecast's volume ({before!r}), so it could not be restored")
        return 2
    out: dict = {
        "tool": "tools/long_play.py",
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "url": a.url,
        "seconds": a.seconds,
        "volume_before": before,
        "group": "never loaded (video_only)",
    }
    rows: list[dict] = []
    try:
        out["volume_muted"] = set_muted(True)
        share = call("/api/share", {"url": a.url})
        out["share"] = share
        t0 = time.monotonic()
        first_playing = None
        while time.monotonic() - t0 < a.seconds + 20:
            s = call("/api/status")
            rows.append(
                {
                    "t": round(time.monotonic() - t0, 2),
                    "state": s.get("state"),
                    "pos": s.get("position_video"),
                    "err": s.get("last_error"),
                }
            )
            if first_playing is None and s.get("state") == "playing" and s.get("position_video"):
                first_playing = rows[-1]["t"]
            if first_playing is not None and rows[-1]["t"] - first_playing >= a.seconds:
                break
            if s.get("state") in ("ended", "idle") and rows[-1]["t"] > 30:
                break
            time.sleep(1.0)
    finally:
        out["stop"] = call("/api/control", {"action": "stop"})
        out["volume_after"] = set_muted(bool(before["muted"]))

    playing = [r for r in rows if r["state"] == "playing" and r["pos"] is not None]
    stalls, last_move = [], None
    for prev, cur in zip(playing, playing[1:], strict=False):
        if cur["pos"] > prev["pos"] + 0.05:
            if last_move is not None and cur["t"] - last_move > 2.5:
                stalls.append({"from_t": last_move, "to_t": cur["t"], "pos": prev["pos"]})
            last_move = cur["t"]
        elif last_move is None:
            last_move = prev["t"]
    if playing and last_move is not None and playing[-1]["t"] - last_move > 2.5:
        stalls.append(
            {"from_t": last_move, "to_t": playing[-1]["t"], "pos": playing[-1]["pos"], "open": True}
        )
    span = (playing[-1]["t"] - playing[0]["t"]) if len(playing) > 1 else 0.0
    advance = (playing[-1]["pos"] - playing[0]["pos"]) if len(playing) > 1 else 0.0
    out["summary"] = {
        "first_playing_s": first_playing,
        "played_wall_s": round(span, 1),
        "advanced_media_s": round(advance, 1),
        "ratio": round(advance / span, 5) if span else None,
        "stalls": stalls,
        "final_state": rows[-1]["state"] if rows else None,
        "errors": sorted({r["err"] for r in rows if r["err"]}),
        "ok": bool(span >= a.seconds - 2 and not stalls and abs(advance - span) < 2.0),
    }
    out["rows"] = rows
    print(json.dumps(out["summary"], indent=1))
    print("volume before", before, "after", out.get("volume_after"))
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(out, fh, indent=1)
            fh.write("\n")
    return 0 if out["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
