#!/usr/bin/env python3
"""group_probe_v020.py — measure the kitchen group's actuator and its real drift.

    *** NEEDS AN AUDIO WINDOW. THIS SCRIPT MAKES SOUND. TASK E WROTE IT AND LEFT IT UNRUN. ***

This is the measurement the v0.2 hardware gate runs later (contracts §1-§2, task E). Everything it
does beyond the dry run is a packet to the Cuisine group, so it refuses to do anything at all
unless the operator says twice that the room may make sound:

    --mute                      the speakers stay muted for the whole run (a mute is silent;
                                that is the whole point of the protocol)
    --i-have-an-audio-window    the owner opened an audio window

and it refuses, before sending anything, any group that fails ``lib/groupguard`` (a group that
contains a video device takes the TV over — A6).

Without ``--run`` it prints the plan and touches nothing: no mDNS, no socket, no transport object.
The ``transport_constructed`` counter in its output is the proof of that (a dry run that built a
transport would say so).

What a real run measures, in this order:

1. 8 landings: park the group in PAUSE at a position, then PLAY, and compare where the playhead
   actually is with where the release asked it to be. That is the dispersion of the
   PAUSE→SEEK→PLAY actuator (evidence/27: σ 30 ms, against 202 ms for a LOAD).
2. ``--minutes`` (default 10) of playhead reads at 4 Hz with no transport event at all: the
   estimator's residual (A15), the 5 s medians, and the group's real drift in ms / 10 min with
   its standard error and the 2σ test of contracts §2.
3. stop (only the session this run created), restore the volume, unmute what it muted (A37).

Output: one JSON file in ``evidence/`` (arguments and every number), URLs redacted.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(ROOT, "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from v020 import group as groupmod  # noqa: E402
from v020.contracts import PLAYER_PLAYING, GroupSample  # noqa: E402

CASTV2_IMPORTED = False
TRANSPORT_CONSTRUCTED = 0

DEFAULT_CONTENT_TYPE = "audio/mp4"
DRY_RUN = "dry-run"
HERE = os.path.dirname(os.path.abspath(__file__))


def redact(url: str | None) -> str:
    """``scheme://host/<6 chars>…`` — the only form allowed in a log or an evidence file (§4)."""
    if not url:
        return "<none>"
    import re

    m = re.match(r"^(?P<scheme>[a-z0-9+.-]+)://(?P<host>[^/?#]+)(?P<path>[^?#]*)", url)
    if not m:
        return "<unparsable-url>"
    return f"{m.group('scheme')}://{m.group('host')}/{m.group('path').lstrip('/')[:6]}…"


def counting_link(host: str, port: int):
    """The transport factory, instrumented: a dry run must leave the counter at 0."""
    global CASTV2_IMPORTED, TRANSPORT_CONSTRUCTED
    from castv2 import CastLink

    CASTV2_IMPORTED = True
    TRANSPORT_CONSTRUCTED += 1
    return CastLink(host, port)


# --------------------------------------------------------------------------------- plan
def plan(args) -> dict:
    """Exactly what a real run would do, in order. Printed by the default (dry) mode."""
    return {
        "mode": DRY_RUN,
        "needs_audio_window": True,
        "sends": "nothing (no mDNS, no socket, no transport object)",
        "group": args.group,
        "media": {"url": redact(args.url) if args.url else "<--url required for a real run>",
                  "content_type": args.content_type, "start_s": args.start},
        "steps": [
            {"step": 1, "what": "resolve the group by name over mDNS, then refuse it unless "
                                "lib/groupguard.require_safe_group accepts every member",
             "packets": "read-only (mDNS + MULTIZONE_STATUS)"},
            {"step": 2, "what": "save volume/mute, then mute the group (a mute is silent)",
             "packets": "SET_VOLUME muted=true"},
            {"step": 3, "what": f"{args.landings} landings: PAUSE -> SEEK -> PLAY at "
                                f"{args.start:.1f}s + n*{args.stride:.1f}s, measure where the "
                                f"playhead lands",
             "packets": "1 LOAD, then per landing 1 PAUSE, 1 SEEK, 1 PLAY"},
            {"step": 4, "what": f"park and release once more, then {args.minutes:.1f} min of "
                                f"playhead reads at {args.hz:.1f} Hz with no transport event: "
                                f"estimator residual, 5 s medians, drift",
             "packets": "GET_STATUS only"},
            {"step": 5, "what": "stop the session this run created, restore the volume, undo the "
                                "mute we set",
             "packets": "STOP + SET_VOLUME"},
        ],
        "output": args.out or "evidence/group-probe-<timestamp>.json",
        "refuses": ["without --mute", "without --i-have-an-audio-window",
                    "any group that fails groupguard"],
        "transport_constructed": TRANSPORT_CONSTRUCTED,
        "castv2_imported": CASTV2_IMPORTED,
    }


# --------------------------------------------------------------------------- measurement
def landing_stats(errors_ms: list[float]) -> dict:
    """Landing dispersion of the PAUSE→SEEK→PLAY actuator (evidence/27's number, re-measured)."""
    if not errors_ms:
        return {"n": 0}
    return {
        "n": len(errors_ms),
        "mean_ms": statistics.fmean(errors_ms),
        "sigma_ms": statistics.stdev(errors_ms) if len(errors_ms) > 1 else 0.0,
        "median_ms": statistics.median(errors_ms),
        "min_ms": min(errors_ms),
        "max_ms": max(errors_ms),
        "p95_ms": sorted(abs(e) for e in errors_ms)[max(0, int(round(0.95 * (len(errors_ms) - 1))))],
        "errors_ms": [round(e, 2) for e in errors_ms],
    }


def measure(sink, args, log) -> dict:
    """The whole protocol. Only called from a real run (all the gates have passed).

    Whatever happens — an exception, a dead link, a park that never settles — the session this
    run created is stopped and the volume/mute we changed is put back (A37, contracts §8).
    """
    saved = {"volume": sink.volume, "muted": sink.muted}
    log(f"saved volumes: level={saved['volume']} muted={saved['muted']}")
    out: dict = {"restored": saved, "stopped": None, "volume_restored": None}
    sink.set_volume(0.0)                       # A37: mutes, never zeroes a level
    log("group muted for the whole run (a mute is silent)")
    try:
        out["landings"], out["playhead"] = _run_protocol(sink, args, log)
        return out
    finally:
        log("stopping the session this run created")
        try:
            sink.stop()
            out["stopped"] = True
        except Exception as exc:               # noqa: BLE001 - still restore the volume
            out["stopped"] = f"{type(exc).__name__}: {exc}"
            log(f"stop failed: {out['stopped']}")
        try:
            restore_volume(sink, saved)
            out["volume_restored"] = True
        except Exception as exc:               # noqa: BLE001
            out["volume_restored"] = f"{type(exc).__name__}: {exc}"
            log(f"volume restore failed: {out['volume_restored']}")


def _run_protocol(sink, args, log) -> tuple[dict, dict]:
    errors: list[float] = []
    failures = 0
    for i in range(args.landings):
        target = args.start + i * args.stride
        if not sink.prepare(args.url, args.content_type, target):
            failures += 1
            log(f"landing {i + 1}/{args.landings}: park failed at {target:.1f}s")
            continue
        released = sink.now()
        sink.play()
        sink.sleep(args.settle)
        sample = None
        for _ in range(20):
            sample = sink.sample()
            if sample is not None:
                break
            sink.sleep(0.05)
        if sample is None:
            failures += 1
            log(f"landing {i + 1}/{args.landings}: no playhead after PLAY")
            continue
        # where the release asked the playhead to be, at the wall time of the read
        expected = target + (sample.wall - released)
        errors.append((sample.media - expected) * 1000.0)
        log(f"landing {i + 1}/{args.landings}: target={target:.1f}s "
            f"error={(sample.media - expected) * 1000.0:+.1f} ms")
        if i < args.landings - 1:
            sink.pause()                     # re-park: the next landing is PAUSE->SEEK->PLAY

    landings = landing_stats(errors)
    landings["failed"] = failures

    # One more park and release: from here on there is no transport event at all, which is what
    # contracts §2 calls steady (and what makes a drift measurement mean anything).
    if not sink.prepare(args.url, args.content_type, args.start + args.landings * args.stride):
        raise RuntimeError("could not park the group for the continuous-play phase")
    sink.play()
    last_play = sink.now()
    log("continuous play: no transport event from now on")

    # -- the measurement window: playhead reads, no transport event ---------------------------
    samples: list[GroupSample] = []
    residuals: list[float] = []
    rejected = 0
    started = sink.now()
    deadline = started + args.minutes * 60.0
    progress = started
    while sink.now() < deadline:
        sample = sink.sample()
        if sample is None:
            rejected += 1
        else:
            samples.append(sample)
            res = sink.estimator.residual_ms()
            if res is not None:
                residuals.append(res)
        sink.sleep(1.0 / args.hz)
        if sink.now() - progress >= 30.0:
            progress = sink.now()
            log(f"playhead: {len(samples)} reads, {rejected} rejected, "
                f"{sink.now() - started:.0f}/{args.minutes * 60:.0f}s")

    # contracts §2: steady = 30 s after the last transport event, and only while playing
    steady_from = last_play + 30.0
    steady = [s for s in samples if s.wall >= steady_from and s.player_state == PLAYER_PLAYING]
    medians = groupmod.window_medians(steady, 5.0)
    drift = groupmod.drift(medians)
    raw = groupmod.least_squares([(s.wall, s.media) for s in steady])

    playhead = {
        "minutes": args.minutes,
        "samples": len(samples),
        "rejected": rejected,
        "steady_samples": len(steady),
        "windows": len(medians),
        "estimator_residual_ms_median": statistics.median(residuals) if residuals else None,
        "estimator_residual_ms_max": max(residuals) if residuals else None,
        "series_sigma_ms": raw.get("sigma_ms"),
        "drift_ms_per_10min": drift.get("drift_ms_per_10min"),
        "drift_se_ms_per_10min": drift.get("se_ms_per_10min"),
        "drift_significant_2sigma": drift.get("significant"),
        "window_medians": [[round(w, 3), round(m, 6)] for w, m in medians],
    }
    return landings, playhead


def restore_volume(sink, saved: dict) -> None:
    """Put back exactly what we found: a level, a mute flag — and never a level of 0 (A37)."""
    level, muted = saved.get("volume"), saved.get("muted")
    if level is None:
        # Nothing is known about the level: touch the mute flag only, never a level.
        sink._set_receiver_volume(level=None, muted=muted)
        return
    # A saved level of 0 comes back as a mute, not as a level: set_volume refuses to send 0.
    sink.set_volume(level, muted=muted)


# ---------------------------------------------------------------------------------- cli
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Measure the kitchen group's actuator and its real drift (needs an audio "
                    "window; default mode is a dry run that sends nothing).")
    p.add_argument("--group", default="Cuisine", help="Cast group name, resolved over mDNS")
    p.add_argument("--url", default=None, help="audio URL the group's DMR must play (required "
                                               "for a real run)")
    p.add_argument("--content-type", default=DEFAULT_CONTENT_TYPE)
    p.add_argument("--start", type=float, default=0.0, help="first parked position, seconds")
    p.add_argument("--stride", type=float, default=20.0, help="media seconds between landings")
    p.add_argument("--landings", type=int, default=8)
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--hz", type=float, default=4.0)
    p.add_argument("--settle", type=float, default=0.8,
                   help="seconds between PLAY and the playhead read of a landing")
    p.add_argument("--out", default=None, help="evidence file (default evidence/group-probe-<ts>.json)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and send nothing (this is the default)")
    p.add_argument("--run", action="store_true", help="actually measure: MAKES SOUND unless muted")
    p.add_argument("--mute", action="store_true", help="keep the speakers muted for the whole run")
    p.add_argument("--i-have-an-audio-window", action="store_true",
                   help="the owner opened an audio window")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log = lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)  # noqa: E731

    if args.dry_run and args.run:
        print("refusing: --dry-run and --run are mutually exclusive", file=sys.stderr)
        return 2
    if not args.run:
        print(json.dumps(plan(args), indent=2, ensure_ascii=False))
        print()
        print("MODE: dry run — the plan above was printed and nothing was sent.")
        print(f"transport_constructed: {TRANSPORT_CONSTRUCTED}")
        print("NEEDS AN AUDIO WINDOW: re-run with --run --mute --i-have-an-audio-window "
              "to measure, and only then.")
        if TRANSPORT_CONSTRUCTED != 0:            # can never happen: nothing calls the factory
            print("FAIL: a transport was constructed during a dry run", file=sys.stderr)
            return 3
        return 0

    if not args.mute:
        print("refusing to run without --mute: the speakers must stay muted for the whole run "
              "(a mute is silent; that is the protocol)", file=sys.stderr)
        return 2
    if not args.i_have_an_audio_window:
        print("refusing to run without --i-have-an-audio-window: this measurement parks, plays "
              "and seeks the kitchen group and is audible only if the mute fails. The owner must "
              "say the window is open.", file=sys.stderr)
        return 2
    if not args.url:
        print("refusing to run without --url: the group's DMR has to be given something to play",
              file=sys.stderr)
        return 2

    groupmod.make_link = counting_link            # instrumented: the gate's audit trail
    sink = groupmod.CastGroup(name=args.group, log=log)
    started_at = time.time()
    result: dict = {"mode": "run", "group": args.group, "started_at": started_at,
                    "args": {"url": redact(args.url), "content_type": args.content_type,
                             "start_s": args.start, "stride_s": args.stride,
                             "landings": args.landings, "minutes": args.minutes, "hz": args.hz,
                             "mute": args.mute}}
    try:
        sink.connect()                            # mDNS + groupguard; refuses a video group
    except groupmod.UnsafeGroup as exc:
        print(f"REFUSED by groupguard: {exc}", file=sys.stderr)
        return 4
    except groupmod.GroupNotFound as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:                      # noqa: BLE001
        print(f"REFUSED: cannot reach the group: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    result["endpoint"] = f"{sink.host}:{sink.port}"
    log(f"group {args.group!r} at {result['endpoint']} passed groupguard")
    try:
        result.update(measure(sink, args, log))   # stops and restores in its own finally
    except Exception as exc:                      # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        log(f"measurement failed: {result['error']}")
        return 5
    finally:
        sink.close()
        result["transport_constructed"] = TRANSPORT_CONSTRUCTED
        result["finished_at"] = time.time()
        result["duration_s"] = round(time.time() - started_at, 1)
        out = args.out or os.path.join(
            ROOT, "evidence",
            f"group-probe-{time.strftime('%Y%m%d-%H%M%S', time.localtime(started_at))}.json")
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
        print(json.dumps({k: v for k, v in result.items() if k != "playhead"},
                         indent=2, ensure_ascii=False))
        print(f"evidence: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
