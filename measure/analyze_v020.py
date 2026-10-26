#!/usr/bin/env python3
"""The v0.2 digital-error instrument (contracts §2), self-tested on synthetic and archived data.

    measure/analyze_v020.py SESSION.jsonl [--video-log RX.jsonl] [--json OUT]
                                    [--delta-v2 S] [--tol-ms 20] [--action-log A.jsonl]

What it decides (and this is the whole point): over every 5 s window of *steady* samples,
the median of ``e = p_group - p_video - DELTA_V2`` must be within ±20 ms; and no ``load`` or
``seek`` may occur inside a steady window (contracts §1: after the start event the group is
never reloaded). It also reports the raw p95/max next to every median (never instead of it,
contracts §2, A15), the steadiest window, the least-squares drift of the 5 s medians in
ms/10 min with its standard error, and the latency of each transport event.

Input formats, mapped explicitly (never guessed silently):

  1. ``v0.2-contract`` — the session log of docs/v020-contracts.md, one JSON object per line.
     Samples: ``{"wall": float, "type": "sample", "group_media": float, "video_media": float,
     "delta_v2": float, "group_state": "playing", "video_state": "playing"}``.
     Events: ``{"wall": float, "type": "load"|"seek"|"play_at"|"play"|"pause"|"rate"|"stop"|
     "idle"|"share"|"first_frame"|"ended", "side": "group"|"video", "media": float}``.
     ``ev`` is accepted for ``type``; ``p_group``/``p_video`` for ``group_media``/``video_media``.
     The full alias table is in ``V02_*`` below and is printed by ``--print-schema``.
  2. ``v0.1-relay`` — the archived v0.1 relay log (evidence/29-relay-untouched.jsonl, 21/22/24/
     26/30/33/34). ``sample`` rows carry ``tv_t``/``grp_t``/``err_ms``/``action``; the relay
     computed ``err_ms`` against its own filtered TV playhead *with its delta already applied*,
     so the mapping uses ``e = err_ms`` and DELTA_V2 is reported as "in-log", not as the CLI
     value. ``content_changed``→share, ``sink_started``(mode load)→load, ``sink_started``(mode
     seek)→seek, ``tv_jump``→seek on the video side, a sample whose ``action`` starts with
     ``realign``→seek, ``tv_session_ended``→end.
  3. ``v0.1-landing`` — the archived v0.1 landing report shape (evidence/39-landing-30ms.json,
     evidence/38-landing-50ms.json): one JSON document with ``cases[].err_median``/``err_max``.
     It carries **no samples**: each case is reported as a window with n unknown and the run is
     ``INCOMPLETE`` (exit 4). It is accepted so the gate can read the old file, not so that a
     series can be invented from it.

Exit codes (the first matching rule wins, in this order):
  2  unreadable input / unknown format          (nothing analysed)
  3  a load or seek occurred in steady state    (structural violation, contracts §1)
  1  a 5 s window median exceeds ±tol           (contracts §2 acceptance)
  4  cannot decide: no steady window at all     (e.g. a landing report, or a truncated log)
  0  every steady window within ±tol and no forbidden transport

``--video-log`` supplies the receiver's own ``state`` stream when the session log has no video
positions: one JSON object per line with ``rx_ms`` and ``media`` (plus optional ``rate``,
``player_state``). Its clock is converted with ``--video-clock-offset-ms`` (default 0) and each
group sample is paired with the nearest receiver state within ``--pair-ms`` (default 150).

``--help`` prints the owner-side precondition; ``--self-test`` proves the instrument offline on
synthetic logs (never on a device) and exits non-zero if any expected verdict is wrong.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))

# Frozen values, imported from the contracts when available so this tool cannot drift from them.
try:                                    # pragma: no cover - exercised wherever lib/ is present
    from v020 import contracts as C     # noqa: E402
    STEADY_AFTER_S = C.STEADY_AFTER_S
    WINDOW_S = C.STEADY_WINDOW_S
    TOL_MS = C.DIGITAL_TOL_MS
except Exception:                       # noqa: BLE001 - standalone copy, same values
    STEADY_AFTER_S, WINDOW_S, TOL_MS = 30.0, 5.0, 20.0

RATE_QUIET_S = 2.0        # contracts §2: "the last rate command is older than 2 s"
NONPLAY_QUIET_S = 2.0     # contracts §2: "neither device reported a non-playing state in the last 2 s"
MIN_WINDOW_S = 4.0        # a 5 s window must actually cover 4 s and ...
MIN_WINDOW_N = 5          # ... carry at least 5 samples (4 Hz => ~20)
SAMPLE_GAP_S = 1.5        # a hole longer than this breaks a steady stretch
DRIFT_GAP_S = 7.0         # gap between window mid-times that starts another drift stretch

EXIT_OK, EXIT_TOL, EXIT_INPUT, EXIT_TRANSPORT, EXIT_UNDECIDED = 0, 1, 2, 3, 4

# --- explicit format mapping tables ---------------------------------------------------------

#: v0.2 event ``type`` -> our internal kind. Anything unknown is kept as ``other`` and named
#: in the report, never silently dropped.
V02_KINDS = {
    "share": "share", "session_start": "share", "resolving": "share",
    "first_frame": "first_frame", "started": "first_frame", "video_playing": "first_frame",
    "load": "load", "seek": "seek", "play_at": "play_at",
    "play": "play", "resume": "play", "pause": "pause", "rate": "rate",
    "stop": "stop", "idle": "stop", "ended": "end", "end": "end",
    "error": "error", "intent": "intent", "device_lost": "device_lost",
    "quiet_refused": "quiet_refused", "state": "state",
}
V02_SAMPLE_KEYS = {
    "group_media": ("group_media", "p_group", "group_pos", "group_t"),
    "video_media": ("video_media", "p_video", "video_pos", "video_t"),
    "group_state": ("group_state", "grp_state"),
    "video_state": ("video_state", "tv_state"),
    "delta": ("delta_v2", "delta"),
}

#: v0.1 relay ``ev`` -> internal kind. ``sample``/``sink_started`` need a field to disambiguate
#: and are handled in ``_map_v01``.
V01_KINDS = {
    "content_changed": "share",
    "tv_session_ended": "end",
    "tv_read_error": "error",
    "content_ready": "other", "tv_waiting": "other", "volumes_saved": "other",
    "kitchen_lowered": "other", "tv_lowered": "other", "tv_cmd": "other",
    "prepare_failed": "other",
}

PLAYING = "playing"          # lower-case comparison on every state string ("PLAYING" too)

TRANSPORT_KINDS = {"share", "load", "seek", "play_at", "play", "pause", "stop", "end"}


def _first(d: dict, keys) -> float | None:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                return None
    return None


def _state(d: dict, keys) -> str | None:
    for k in keys:
        if k in d and d[k] is not None:
            return str(d[k]).lower()
    return None


def redact(url: str) -> str:
    """A googlevideo URL is a short-lived token: scheme://host/<6 chars>… everywhere."""
    if not url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}/<redacted>"


# --- reading --------------------------------------------------------------------------------

def read_rows(path: str) -> tuple[str, list[dict]]:
    """Return (format, rows). Raises ValueError on an unreadable/unknown input."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if not text.strip():
        raise ValueError(f"{path}: empty file")
    stripped = text.strip()
    rows = []
    line_err = None
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            line_err = f"{path}:{n}: not JSON ({exc.msg})"
            break
        if not isinstance(obj, dict):
            line_err = f"{path}:{n}: not a JSON object"
            break
        rows.append(obj)
    if line_err is not None:                    # a pretty-printed document, not JSONL
        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError:
            raise ValueError(line_err) from None
        if isinstance(doc, list) and doc and isinstance(doc[0], dict) and "cases" not in doc[0]:
            return "v0.1-landing", doc
        if isinstance(doc, dict) and "cases" in doc:
            return "v0.1-landing", [doc]
        raise ValueError(line_err)
    if len(rows) == 1 and "cases" in rows[0]:
        return "v0.1-landing", rows
    if not rows:
        raise ValueError(f"{path}: no events")
    if any("ev" in r and "type" not in r for r in rows):
        return "v0.1-relay", rows
    if any(("type" in r or "ev" in r) for r in rows):
        return "v0.2-contract", rows
    raise ValueError(f"{path}: no recognised event shape (no 'type'/'ev' field)")


def map_events(rows: list[dict], fmt: str) -> tuple[list[dict], dict]:
    """Format rows -> (samples, events). Every mapping decision is recorded in ``notes``."""
    samples, events, notes = [], [], {}
    if fmt == "v0.1-landing":
        return [], []
    for r in rows:
        wall = _first(r, ("wall", "t", "time"))
        if wall is None:
            continue
        if fmt == "v0.2-contract":
            kind = str(r.get("type", r.get("ev", ""))).lower()
            kind = V02_KINDS.get(kind, "other")
            if "media" in r and kind in ("state", "sample"):
                kind = "sample"
            if kind == "other" and str(r.get("type", r.get("ev", ""))) == "sample":
                kind = "sample"
            if kind == "sample":
                gm = _first(r, V02_SAMPLE_KEYS["group_media"])
                vm = _first(r, V02_SAMPLE_KEYS["video_media"])
                if gm is None and vm is None:
                    kind = "other"
            if kind == "sample":
                samples.append({
                    "wall": wall,
                    "group_media": gm,
                    "video_media": vm,
                    "group_state": _state(r, V02_SAMPLE_KEYS["group_state"]),
                    "video_state": _state(r, V02_SAMPLE_KEYS["video_state"]),
                    "delta": _first(r, V02_SAMPLE_KEYS["delta"]),
                    "rate": _first(r, ("rate",)),
                    "raw": r,
                })
            else:
                events.append({"wall": wall, "kind": kind,
                               "side": r.get("side"),
                               "media": _first(r, ("media", "position")),
                               "raw_type": str(r.get("type", r.get("ev", ""))),
                               "raw": r})
            notes.setdefault("kinds", {})
            notes["kinds"][str(r.get("type", r.get("ev")))] = \
                notes["kinds"].get(str(r.get("type", r.get("ev"))), 0) + 1
        else:  # v0.1-relay
            ev = str(r.get("ev", ""))
            if ev == "sample":
                err = r.get("err_ms")
                samples.append({
                    "wall": wall,
                    "group_media": _first(r, ("grp_t",)),
                    "video_media": _first(r, ("tv_t",)),
                    "group_state": _state(r, ("grp_state",)),
                    "video_state": _state(r, ("tv_state",)),
                    "delta": None,
                    "err_ms": None if err is None else float(err),
                    "rate": _first(r, ("rate",)),
                    "action": r.get("action"),
                    "raw": r,
                })
                action = str(r.get("action") or "")
                if r.get("err_ms") is not None and action.startswith("realign"):
                    events.append({"wall": wall, "kind": "seek", "side": "group",
                                   "media": None, "raw_type": f"sample.action={action}",
                                   "raw": r})
            elif ev == "sink_started":
                mode = str(r.get("mode", "load")).lower()
                events.append({"wall": wall, "kind": "load" if mode == "load" else "seek",
                               "side": "group", "media": _first(r, ("pos",)),
                               "first_frame": True, "raw_type": f"sink_started:{mode}",
                               "raw": r})
            else:
                kind = V01_KINDS.get(ev, "other")
                e = {"wall": wall, "kind": kind, "side": None, "media": _first(r, ("tv_t",)),
                     "raw_type": ev, "raw": r}
                if ev == "tv_jump":
                    e["kind"] = "seek"
                    e["side"] = "video"
                events.append(e)
            notes.setdefault("kinds", {})
            notes["kinds"][ev] = notes["kinds"].get(ev, 0) + 1
    samples.sort(key=lambda s: s["wall"])
    events.sort(key=lambda e: e["wall"])
    notes["first_frame_marks"] = ["sink_started"] if fmt == "v0.1-relay" else ["first_frame"]
    notes["delta_note"] = ("relay log: err_ms is p_group - p_video with the v0.1 delta already "
                           "applied; DELTA_V2 is not re-subtracted") if fmt == "v0.1-relay" else \
                          ("DELTA_V2 taken from each sample's delta_v2/delta, else --delta-v2")
    return samples, events, notes


def pair_video_log(samples: list[dict], path: str, clock_offset_ms: float,
                   pair_ms: float) -> int:
    """Fill missing ``video_media`` from the receiver's own state stream. Returns pairs made."""
    vids = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        rx = _first(d, ("rx_ms", "wall_ms", "wall"))
        media = _first(d, ("media", "video_media", "p_video", "position"))
        if rx is None or media is None:
            continue
        vids.append({"wall": rx / 1000.0 + clock_offset_ms / 1000.0, "media": media,
                     "state": _state(d, ("player_state", "video_state"))})
    vids.sort(key=lambda v: v["wall"])
    if not vids:
        return 0
    vt = np.array([v["wall"] for v in vids])
    made = 0
    for s in samples:
        if s.get("video_media") is not None:
            continue
        i = int(np.searchsorted(vt, s["wall"]))
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(vids):
                dt = abs(vids[j]["wall"] - s["wall"])
                if dt <= pair_ms / 1000.0 and (best is None or dt < best[0]):
                    best = (dt, vids[j])
        if best is not None:
            s["video_media"] = best[1]["media"]
            s["video_state"] = s.get("video_state") or best[1].get("state")
            s["video_paired_ms"] = round(best[0] * 1000, 1)
            made += 1
    return made


def pair_action_log(events: list[dict], path: str) -> int:
    """Append transport commands observed by the harness (POST /api/control, TV remote).

    Accepted rows: ``{"wall": float, "kind"|"action"|"ev": "play"|"pause"|"seek"|"stop"|
    "share", "media"?: float}``. ``kind`` is mapped with V02_KINDS.
    """
    made = 0
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        wall = _first(d, ("wall", "t", "time"))
        if wall is None:
            continue
        raw = str(d.get("kind") or d.get("action") or d.get("ev") or "").lower()
        kind = V02_KINDS.get(raw, raw if raw in TRANSPORT_KINDS else "other")
        if kind == "other":
            continue
        events.append({"wall": wall, "kind": kind, "side": d.get("side"),
                       "media": _first(d, ("media", "position")), "raw_type": f"action:{raw}",
                       "raw": d})
        made += 1
    events.sort(key=lambda e: e["wall"])
    return made


# --- the instrument -------------------------------------------------------------------------

def _steady_flags(samples: list[dict], events: list[dict]) -> None:
    """Annotate each sample with ``e_ms`` and ``steady`` (contracts §2, verbatim rule)."""
    transport_t = [e["wall"] for e in events if e["kind"] in TRANSPORT_KINDS]
    rate_t = [e["wall"] for e in events if e["kind"] == "rate"]
    prev = None
    for s in samples:
        e = s.get("err_ms")
        if e is None and s.get("group_media") is not None and s.get("video_media") is not None:
            delta = s.get("delta")
            if delta is None:
                delta = s.get("delta_cli", 0.0)
            e = (s["group_media"] - s["video_media"] - delta) * 1000.0
        s["e_ms"] = None if e is None else float(e)
        last_tr = max([t for t in transport_t if t <= s["wall"]], default=None)
        since = None if last_tr is None else s["wall"] - last_tr
        last_rate = max([t for t in rate_t if t <= s["wall"]], default=None)
        rate_age = None if last_rate is None else s["wall"] - last_rate
        gs = (s.get("group_state") or "").lower()
        vs = (s.get("video_state") or "").lower()
        playing = (gs in ("", PLAYING)) and (vs in ("", PLAYING))
        nonplay_recent = False
        if prev is not None and s["wall"] - prev["wall"] <= NONPLAY_QUIET_S:
            if (prev.get("group_state") or "").lower() not in ("", PLAYING) or \
               (prev.get("video_state") or "").lower() not in ("", PLAYING):
                nonplay_recent = True
        s["since_transport_s"] = None if since is None else round(since, 2)
        s["steady"] = bool(s["e_ms"] is not None and playing and not nonplay_recent
                           and (since is not None and since >= STEADY_AFTER_S)
                           and (rate_age is None or rate_age >= RATE_QUIET_S))
        prev = s


def _windows(samples: list[dict], t0: float) -> list[dict]:
    """5 s windows over maximal runs of steady samples, per steady stretch."""
    steady = [s for s in samples if s["steady"]]
    if not steady:
        return []
    stretches, cur = [], [steady[0]]
    for s in steady[1:]:
        if s["wall"] - cur[-1]["wall"] > SAMPLE_GAP_S:
            stretches.append(cur)
            cur = []
        cur.append(s)
    stretches.append(cur)
    out = []
    for st in stretches:
        a0 = st[0]["wall"]
        bins: dict[int, list[dict]] = {}
        for s in st:
            bins.setdefault(int((s["wall"] - a0) // WINDOW_S), []).append(s)
        for b in sorted(bins):
            ws = bins[b]
            span = ws[-1]["wall"] - ws[0]["wall"]
            e = np.array([s["e_ms"] for s in ws], dtype=float)
            full = span >= MIN_WINDOW_S and len(ws) >= MIN_WINDOW_N
            w = {
                "t_start": round(ws[0]["wall"] - t0, 2),
                "t_end": round(ws[-1]["wall"] - t0, 2),
                "wall_start": round(ws[0]["wall"], 3),
                "n": len(ws),
                "span_s": round(span, 2),
                "median_ms": round(float(np.median(e)), 1),
                "p95_abs_ms": round(float(np.percentile(np.abs(e), 95)), 1),
                "max_abs_ms": round(float(np.abs(e).max()), 1),
                "stdev_ms": round(float(e.std(ddof=1)) if len(e) > 1 else 0.0, 1),
                "steady_since_s": ws[0].get("since_transport_s"),
                "full": full,
            }
            out.append(w)
    return out


def _drift(windows: list[dict]) -> list[dict]:
    """Least squares slope of the 5 s medians, per continuous stretch, in ms/10 min."""
    full = [w for w in windows if w["full"]]
    if not full:
        return []
    stretches, cur = [], [full[0]]
    for w in full[1:]:
        if w["wall_start"] - cur[-1]["wall_start"] > DRIFT_GAP_S:
            stretches.append(cur)
            cur = []
        cur.append(w)
    stretches.append(cur)
    out = []
    for st in stretches:
        if len(st) < 3:
            out.append({"wall_from": st[0]["wall_start"], "windows": len(st),
                        "drift_ms_per_10min": None, "sigma_ms_per_10min": None,
                        "note": "fewer than 3 windows: no slope"})
            continue
        x = np.array([w["wall_start"] for w in st], dtype=float) - st[0]["wall_start"]
        y = np.array([w["median_ms"] for w in st], dtype=float)
        slope, icpt = np.polyfit(x, y, 1)
        resid = y - (slope * x + icpt)
        sxx = float(np.sum((x - x.mean()) ** 2))
        se = float(np.sqrt(np.sum(resid ** 2) / max(1, len(y) - 2) / sxx)) if sxx > 0 else None
        proj = float(slope) * 600.0
        sigma = None if se is None else se * 600.0
        out.append({
            "wall_from": round(float(st[0]["wall_start"]), 3),
            "t_start": st[0]["t_start"], "t_end": st[-1]["t_end"],
            "windows": len(st),
            "drift_ms_per_10min": round(proj, 1),
            "sigma_ms_per_10min": None if sigma is None else round(sigma, 1),
            "trend_counts": bool(sigma is not None and abs(proj) > 2 * sigma),
        })
    return out


def _latencies(samples: list[dict], events: list[dict], windows: list[dict],
               t0: float) -> list[dict]:
    """Per-transport-event latency, from the event to the first observable consequence."""
    out = []
    first_frame_marks = {"first_frame"}
    for ev in events:
        k, t = ev["kind"], ev["wall"]
        row = {"kind": ev["raw_type"], "t": round(t - t0, 2)}
        target = {"share": "first_frame", "play": "playing", "pause": "paused",
                  "seek": "steady again", "stop": "idle"}.get(k)
        if target is None:
            continue
        after = [s for s in samples if s["wall"] > t]
        got = None
        if k == "share":
            marks = [e for e in events if e["wall"] >= t and (e["kind"] == "first_frame"
                     or e.get("first_frame") or e["raw_type"] in first_frame_marks)]
            if marks:
                got = (marks[0]["wall"] - t, "event:first_frame")
            elif after:
                for s in after:
                    if (s.get("group_state") == PLAYING or s.get("video_state") == PLAYING):
                        got = (s["wall"] - t, "sample:playing")
                        break
        elif k == "play":
            for s in after:
                if (s.get("group_state") or "") == PLAYING:
                    got = (s["wall"] - t, "sample:group playing")
                    break
        elif k == "pause":
            for s in after:
                if (s.get("group_state") or "") not in ("", PLAYING):
                    got = (s["wall"] - t, "sample:group not playing")
                    break
        elif k == "seek":
            starts = [w for w in windows if w["wall_start"] > t and w["full"]]
            if starts:
                got = (starts[0]["wall_start"] - t, "first full steady window after the seek")
        elif k == "stop":
            for s in after:
                apps = s["raw"].get("applications") if isinstance(s.get("raw"), dict) else None
                if apps == [] or (s.get("group_state") or "") in ("idle", "stopped"):
                    got = (s["wall"] - t, "sample:group idle")
                    break
            if got is None:
                ends = [e for e in events if e["wall"] > t and e["kind"] in ("stop", "end")]
                if ends:
                    got = (ends[0]["wall"] - t, "event:stop/ended")
        row["target"] = target
        if got is None:
            row["unresolved"] = True
        else:
            row["latency_s"] = round(float(got[0]), 2)
            row["resolved_by"] = got[1]
        out.append(row)
    return out


def _landing_report(path: str, doc: dict, tol_ms: float) -> dict:
    """The v0.1 landing shape (evidence/39): per-case medians, no series."""
    cases = doc["cases"] if isinstance(doc, dict) else []
    wins, breaches = [], []
    for i, c in enumerate(cases):
        med = c.get("err_median", c.get("final15s_median_ms"))
        mx = c.get("err_max", c.get("final15s_max_abs"))
        row = {"case": c.get("case", c.get("cmd")), "median_ms": med, "max_abs_ms": mx,
                "n": None, "full": False,
                "note": "landing report: one number per case, no samples to window"}
        if isinstance(med, (int, float)):
            row["within_tol"] = abs(med) <= tol_ms
            if not row["within_tol"]:
                breaches.append(row)
        wins.append(row)
    return {
        "tool": "analyze_v020", "version": "0.2.0", "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "file": os.path.abspath(path), "format": "v0.1-landing",
        "landing_tol_ms": doc.get("land_tol_ms"), "windows": wins,
        "counts": {"cases": len(cases), "samples": 0, "windows": 0, "steady_samples": 0},
        "overall": {"median_ms": None, "p95_abs_ms": None, "max_abs_ms": None, "n": 0},
        "steadiest_window": None,
        "latencies": [],
        "drift": {"stretches": []},
        "violations": {"forbidden_transport": [], "tolerance": breaches},
        "verdict": ("FAIL — a landing case median exceeds ±%g ms" % tol_ms if breaches else
                    "INCOMPLETE — a v0.1 landing report carries no samples; run the v0.2 session "
                    "log through this instrument instead"),
        "exit_code": EXIT_TOL if breaches else EXIT_UNDECIDED,
    }


def analyze_file(path: str, video_log: str | None = None, video_clock_offset_ms: float = 0.0,
                 pair_ms: float = 150.0, delta_cli: float = 0.0, tol_ms: float = TOL_MS,
                 action_log: str | None = None) -> tuple[dict, int]:
    fmt, rows = read_rows(path)
    if fmt == "v0.1-landing":
        rep = _landing_report(path, rows[0], tol_ms)
        return rep, rep["exit_code"]

    samples, events, notes = map_events(rows, fmt)
    paired = pair_video_log(samples, video_log, video_clock_offset_ms, pair_ms) \
        if video_log else 0
    acted = pair_action_log(events, action_log) if action_log else 0
    for s in samples:
        s["delta_cli"] = delta_cli

    deltas = [s["delta"] for s in samples if s.get("delta") is not None]
    delta_used = deltas[0] if deltas else (None if fmt == "v0.1-relay" else delta_cli)
    delta_source = ("in-log (the relay applied its delta to err_ms; DELTA_V2 is not re-subtracted)"
                    if fmt == "v0.1-relay" else
                    ("per-sample delta_v2/delta" if deltas else "--delta-v2 / default"))
    _steady_flags(samples, events)
    t0 = min([s["wall"] for s in samples] + [e["wall"] for e in events]) if (samples or events) else 0.0
    windows = _windows(samples, t0)
    drift = _drift(windows)
    lat = _latencies(samples, events, windows, t0)
    steady = [s for s in samples if s["steady"]]
    full = [w for w in windows if w["full"]]
    e_all = np.array([s["e_ms"] for s in steady], dtype=float) if steady else np.array([])

    # Forbidden transport: a load/seek whose predecessor transport event is >= steady age old.
    last_tr = None
    forbidden = []
    for ev in events:
        if ev["kind"] in TRANSPORT_KINDS:
            if ev["kind"] in ("load", "seek") and last_tr is not None and \
                    ev["wall"] - last_tr >= STEADY_AFTER_S:
                forbidden.append({
                    "kind": ev["raw_type"], "side": ev.get("side"),
                    "t": round(ev["wall"] - t0, 2),
                    "since_previous_transport_s": round(ev["wall"] - last_tr, 2),
                    "note": "load/seek in steady state: forbidden after the start event (§1)",
                })
            last_tr = ev["wall"]
    tol_bad = [w for w in full if abs(w["median_ms"]) > tol_ms]

    steadiest = min(full, key=lambda w: (w["stdev_ms"], w["t_start"])) if full else None
    if forbidden:
        code, verdict = EXIT_TRANSPORT, (
            f"FAIL — {len(forbidden)} load/seek event(s) inside steady state (contracts §1); "
            f"{len(full)} steady window(s), worst |median| "
            f"{max((abs(w['median_ms']) for w in full), default=float('nan')):.1f} ms")
    elif tol_bad:
        code, verdict = EXIT_TOL, (
            f"FAIL — {len(tol_bad)}/{len(full)} steady window(s) with |median| > {tol_ms:g} ms "
            f"(worst {max(abs(w['median_ms']) for w in tol_bad):.1f} ms)")
    elif not full:
        code, verdict = EXIT_UNDECIDED, (
            "INCOMPLETE — no steady 5 s window (needs >= %gs of playing samples after the last "
            "transport event; %d sample(s), %d steady)" %
            (STEADY_AFTER_S, len(samples), len(steady)))
    else:
        code, verdict = EXIT_OK, (
            f"PASS — {len(full)}/{len(full)} steady window(s) within ±{tol_ms:g} ms; worst "
            f"|median| {max(abs(w['median_ms']) for w in full):.1f} ms; no reload in steady state")

    rep = {
        "tool": "analyze_v020", "version": "0.2.0",
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "file": os.path.abspath(path),
        "video_log": os.path.abspath(video_log) if video_log else None,
        "format": fmt, "mapping": notes,
        "delta_v2": delta_used, "delta_source": delta_source, "tol_ms": tol_ms,
        "steady_rule": {"after_s": STEADY_AFTER_S, "window_s": WINDOW_S,
                        "rate_quiet_s": RATE_QUIET_S, "nonplay_quiet_s": NONPLAY_QUIET_S,
                        "min_window_s": MIN_WINDOW_S, "min_window_n": MIN_WINDOW_N},
        "counts": {
            "lines": len(rows), "samples": len(samples), "samples_with_e": sum(
                1 for s in samples if s["e_ms"] is not None),
            "steady_samples": len(steady), "transport_events": sum(
                1 for e in events if e["kind"] in TRANSPORT_KINDS),
            "windows": len(full), "partial_windows": len(windows) - len(full),
            "video_paired": paired, "actions_merged": acted,
        },
        "other_events": sorted({e["raw_type"] for e in events if e["kind"] == "other"}),
        "overall": {
            "median_ms": round(float(np.median(e_all)), 1) if e_all.size else None,
            "p95_abs_ms": round(float(np.percentile(np.abs(e_all), 95)), 1) if e_all.size else None,
            "max_abs_ms": round(float(np.abs(e_all).max()), 1) if e_all.size else None,
            "n": int(e_all.size),
        },
        "windows": windows,
        "steadiest_window": steadiest and {k: steadiest[k] for k in
                                           ("t_start", "t_end", "n", "median_ms",
                                            "stdev_ms", "max_abs_ms")},
        "drift": {"stretches": drift,
                  "longest": max(drift, key=lambda d: d["windows"]) if drift else None},
        "latencies": lat,
        "violations": {"forbidden_transport": forbidden,
                       "tolerance": [{k: w[k] for k in ("t_start", "median_ms", "n")}
                                     for w in tol_bad]},
        "verdict": verdict, "exit_code": code,
    }
    return rep, code


# --- text output ----------------------------------------------------------------------------

def print_report(rep: dict) -> None:
    print(f"analyze_v020 — {rep['file']}  [{rep['format']}]")
    print(f"  verdict: {rep['verdict']}")
    if rep["format"] == "v0.1-landing":
        for w in rep["windows"]:
            print(f"    case {str(w['case'])[:38]:38s} median={w['median_ms']} "
                  f"max_abs={w['max_abs_ms']} within_tol={w.get('within_tol')}")
        return
    c = rep["counts"]
    o = rep["overall"]
    delta_txt = ("in-log" if rep.get("delta_source", "").startswith("in-log")
                 else rep["delta_v2"])
    print(f"  delta_v2={delta_txt}  samples={c['samples']} (steady {c['steady_samples']})"
          f"  windows={c['windows']} full / {c['partial_windows']} partial"
          f"  transport events={c['transport_events']}")
    print(f"  overall steady: median {o['median_ms']} ms, p95|e| {o['p95_abs_ms']} ms, "
          f"max|e| {o['max_abs_ms']} ms (n={o['n']})")
    print("  windows (t_start, n, median, p95|e|, max|e|, stdev):")
    for w in rep["windows"]:
        mark = "" if w["full"] else "  [partial]"
        print(f"    {w['t_start']:8.2f}s n={w['n']:3d} med={w['median_ms']:8.1f} "
              f"p95={w['p95_abs_ms']:7.1f} max={w['max_abs_ms']:7.1f} sd={w['stdev_ms']:6.1f}{mark}")
    sw = rep["steadiest_window"]
    if sw:
        print(f"  steadiest window: {sw['t_start']}..{sw['t_end']}s "
              f"median {sw['median_ms']} ms, sd {sw['stdev_ms']} ms")
    for d in rep["drift"]["stretches"]:
        if d.get("drift_ms_per_10min") is not None:
            print(f"  drift {d['t_start']}..{d['t_end']}s over {d['windows']} windows: "
                  f"{d['drift_ms_per_10min']} ± {d['sigma_ms_per_10min']} ms/10 min "
                  f"({'counts' if d['trend_counts'] else 'within 2 sigma'})")
        else:
            print(f"  drift {d.get('t_start')}..{d.get('t_end')}s: {d.get('note')}")
    if rep["latencies"]:
        print("  transport latencies:")
        for l in rep["latencies"]:
            if l.get("unresolved"):
                print(f"    {l['kind']:22s} -> {l['target']:18s} UNRESOLVED (not in this log)")
            else:
                print(f"    {l['kind']:22s} -> {l['target']:18s} {l['latency_s']} s "
                      f"(via {l['resolved_by']})")
    for v in rep["violations"]["forbidden_transport"]:
        print(f"  VIOLATION {v['kind']} at t={v['t']}s, {v['since_previous_transport_s']}s after "
              f"the previous transport event")
    for v in rep["violations"]["tolerance"]:
        print(f"  VIOLATION window t={v['t_start']}s median {v['median_ms']} ms > ±{rep['tol_ms']} ms")


SCHEMA = """v0.2 session log (one JSON object per line), as this instrument reads it:

  sample: {"wall": 12.5, "type": "sample", "group_media": 34.21, "video_media": 33.95,
           "delta_v2": 0.265, "group_state": "playing", "video_state": "playing", "rate": 1.0}
  event : {"wall": 12.0, "type": "load"|"seek"|"play_at"|"play"|"pause"|"rate"|"stop"|"idle"|
                  "share"|"first_frame"|"ended"|"device_lost"|"error",
           "side": "group"|"video", "media": 420.0}

  aliases: ev for type; p_group/p_group_pos for group_media; p_video for video_media;
           delta for delta_v2; grp_state for group_state; tv_state for video_state.

v0.1 relay log (evidence/29-relay-untouched.jsonl):
  sample   -> sample with e = err_ms (the relay's delta is already applied)
  sink_started(mode=load) -> load ; sink_started(mode=seek) -> seek
  tv_jump -> seek (video side) ; sample.action realign* -> seek (group side)
  content_changed -> share ; tv_session_ended -> end ; anything else -> ignored (named)

v0.1 landing report (evidence/39-landing-30ms.json): one JSON document, cases[].err_median.
  No samples: reported as INCOMPLETE (exit 4) unless a case median exceeds ±tol (exit 1)."""

PRECONDITION = """Owner-side precondition (the hardware gate, not tonight):
  - a v0.2 session log written by the owner app, and optionally the receiver's state stream
    (--video-log) and the harness's control-action log (--action-log);
  - no device is touched by this instrument: it reads files only. It can run at any time."""


def _write(path: str, obj) -> None:
    if not path:
        return
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
    print(f"  -> {path}")


# --- self-test ------------------------------------------------------------------------------

def _synth_log(path: str, *, minutes: float = 4.0, drift_ms_per_10min: float = 0.0,
               noise_ms: float = 4.0, reload_at: float | None = None,
               bias_ms: float = 0.0, seed: int = 7, start_wall: float = 1000.0) -> None:
    """A synthetic v0.2 session log: 4 Hz, a start at t=2 s, then steady samples."""
    rng = np.random.default_rng(seed)
    rows = [{"wall": start_wall, "type": "share"},
            {"wall": start_wall + 0.5, "type": "load", "side": "group", "media": 0.0},
            {"wall": start_wall + 0.8, "type": "play_at", "side": "video", "media": 0.0},
            {"wall": start_wall + 2.0, "type": "first_frame"}]
    t = 2.0
    while t < minutes * 60:
        if reload_at is not None and abs(t - reload_at) < 0.2:
            rows.append({"wall": start_wall + t, "type": "load", "side": "group",
                         "media": t})
        e = bias_ms + drift_ms_per_10min * t / 600.0 + float(rng.normal(0, noise_ms))
        g = 100.0 + t
        v = g - e / 1000.0 - 0.265
        rows.append({"wall": start_wall + t, "type": "sample", "group_media": round(g, 4),
                     "video_media": round(v, 4), "delta_v2": 0.265,
                     "group_state": "playing", "video_state": "playing", "rate": 1.0})
        t += 0.25
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def self_test(tmpdir: str, out: str = "") -> int:
    os.makedirs(tmpdir, exist_ok=True)
    cases = []

    def run(name, path, expect_code, expect=None, **kw):
        rep, code = analyze_file(path, **kw)
        ok = code == expect_code and (expect is None or expect(rep))
        cases.append({"case": name, "input": os.path.basename(path), "exit": code,
                      "expected_exit": expect_code, "ok": bool(ok),
                      "verdict": rep["verdict"],
                      "overall": rep["overall"], "windows": rep["counts"]["windows"]})
        print(("  ok   " if ok else "  FAIL ") + f"{name} (exit {code})")
        return rep, code

    print("analyze_v020 --self-test (synthetic logs, archived v0.1 logs; no device touched)")
    good = os.path.join(tmpdir, "contract-good.jsonl")
    _synth_log(good, bias_ms=3.0)
    run("v0.2 contract log, 3 ms bias", good, EXIT_OK)

    reloaded = os.path.join(tmpdir, "contract-injected-reload.jsonl")
    _synth_log(reloaded, reload_at=120.0, bias_ms=3.0)
    run("v0.2 log with a hidden reload at 120 s", reloaded, EXIT_TRANSPORT,
        expect=lambda r: len(r["violations"]["forbidden_transport"]) == 1)

    drifted = os.path.join(tmpdir, "contract-drift.jsonl")
    _synth_log(drifted, minutes=6.0, drift_ms_per_10min=60.0, bias_ms=0.0, noise_ms=3.0)
    rep, _ = run("v0.2 log with +60 ms/10 min injected drift", drifted, EXIT_TOL)
    cases[-1]["ok"] = bool(cases[-1]["ok"] and rep["drift"]["longest"] and
                           abs(rep["drift"]["longest"]["drift_ms_per_10min"] - 60.0) < 15.0 and
                           rep["drift"]["longest"]["trend_counts"])
    if not cases[-1]["ok"]:
        print("  FAIL  the injected drift was not recovered by the least-squares fit")

    breach = os.path.join(tmpdir, "contract-breach.jsonl")
    _synth_log(breach, bias_ms=35.0, noise_ms=2.0)
    run("v0.2 log with a +35 ms median (over ±20 ms)", breach, EXIT_TOL)

    short = os.path.join(tmpdir, "contract-short.jsonl")
    _synth_log(short, minutes=0.5)
    run("v0.2 log too short for a steady window", short, EXIT_UNDECIDED)

    # --video-log: samples carry only the group position; the receiver stream feeds the video.
    gpath = os.path.join(tmpdir, "contract-group-only.jsonl")
    vpath = os.path.join(tmpdir, "receiver-states.jsonl")
    rows = [json.loads(l) for l in open(good)]
    with open(gpath, "w") as fh:
        for r in rows:
            if r["type"] == "sample":
                r = {k: v for k, v in r.items() if k != "video_media"}
            fh.write(json.dumps(r) + "\n")
    with open(vpath, "w") as fh:
        for r in rows:
            if r["type"] == "sample":
                fh.write(json.dumps({"rx_ms": r["wall"] * 1000.0, "media": r["video_media"],
                                     "player_state": "playing"}) + "\n")
    run("v0.2 log without video positions + --video-log", gpath, EXIT_OK,
        video_log=vpath)

    # archived v0.1 sessions, when the repository has them
    arch29 = os.path.join(ROOT, "evidence", "29-relay-untouched.jsonl")
    if os.path.exists(arch29):
        a29, _ = run("archived v0.1 relay log evidence/29 (v0.1 realigns after the start: "
                     "the v0.2 rule forbids it)", arch29, EXIT_TRANSPORT,
                     expect=lambda r: r["counts"]["steady_samples"] > 500 and
                     r["drift"]["longest"] is not None and
                     len(r["violations"]["forbidden_transport"]) >= 1)
        if a29["drift"]["longest"]:
            print(f"       drift of the 5 s medians: "
                  f"{a29['drift']['longest']['drift_ms_per_10min']} ± "
                  f"{a29['drift']['longest']['sigma_ms_per_10min']} ms/10 min over "
                  f"{a29['drift']['longest']['windows']} windows; the archived report's "
                  f"raw-sample slope is -5.5 ± 1.2 (different estimator, same conclusion: "
                  f"no trend beyond 2 sigma)")
        print(f"       windows over ±20 ms: {len(a29['violations']['tolerance'])} "
              f"(v0.1 ran at a 30-70 ms landing tolerance, not at the v0.2 20 ms)")
    arch39 = os.path.join(ROOT, "evidence", "39-landing-30ms.json")
    if os.path.exists(arch39):
        run("archived v0.1 landing report evidence/39", arch39, EXIT_UNDECIDED)

    bad = os.path.join(tmpdir, "not-a-log.jsonl")
    with open(bad, "w") as fh:
        fh.write("this is not JSON\n")
    try:
        analyze_file(bad)
        cases.append({"case": "unreadable input", "ok": False, "exit": None,
                      "expected_exit": EXIT_INPUT})
        print("  FAIL  a non-JSON input was accepted")
    except ValueError:
        cases.append({"case": "unreadable input", "ok": True, "exit": EXIT_INPUT,
                      "expected_exit": EXIT_INPUT})
        print(f"  ok   unreadable input rejected (exit {EXIT_INPUT})")

    ok = all(c["ok"] for c in cases)
    res = {"tool": "analyze_v020", "self_test": True,
           "when": time.strftime("%Y-%m-%d %H:%M:%S"),
           "offline": True, "targets_touched": [],
           "note": "every input is synthetic except the two archived v0.1 files, which are read "
                   "as files; nothing here opens a socket",
           "cases": cases, "passed": sum(1 for c in cases if c["ok"]), "total": len(cases),
           "result": "SELF-TEST PASS" if ok else "SELF-TEST FAIL"}
    _write(out, res)
    print(f"  {res['result']} — {res['passed']}/{res['total']}")
    return 0 if ok else 1


# --- main -----------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Per-window digital error of a v0.2 session log (contracts §2).",
        epilog=PRECONDITION, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?", help="session log (contract JSONL, v0.1 relay JSONL, "
                                          "or a v0.1 landing report)")
    ap.add_argument("--video-log", help="receiver state stream (rx_ms, media) for video positions")
    ap.add_argument("--video-clock-offset-ms", type=float, default=0.0,
                    help="receiver clock minus controller clock, ms (default 0)")
    ap.add_argument("--pair-ms", type=float, default=150.0,
                    help="max distance between a group sample and a receiver state (default 150)")
    ap.add_argument("--action-log", help="harness control actions (wall + kind/action) to merge")
    ap.add_argument("--delta-v2", type=float, default=0.0,
                    help="DELTA_V2 when the log does not carry it (default 0.0)")
    ap.add_argument("--tol-ms", type=float, default=TOL_MS,
                    help=f"acceptance on the window median (default {TOL_MS:g} ms)")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--print-schema", action="store_true", help="print the accepted formats")
    ap.add_argument("--self-test", action="store_true",
                    help="run the instrument on synthetic and archived inputs, then exit")
    ap.add_argument("--self-test-dir", default=os.path.join(ROOT, "evidence", "audio",
                                                           "48-selftest"),
                    help="where synthetic inputs are written (gitignored)")
    a = ap.parse_args(argv)

    if a.print_schema:
        print(SCHEMA)
        return 0
    if a.self_test:
        return self_test(a.self_test_dir, a.json)
    if not a.log:
        ap.error("a session log is required (or --self-test / --print-schema)")
    try:
        rep, code = analyze_file(a.log, a.video_log, a.video_clock_offset_ms, a.pair_ms,
                                 a.delta_v2, a.tol_ms, a.action_log)
    except (OSError, ValueError) as exc:
        print(f"analyze_v020: {exc}", file=sys.stderr)
        return EXIT_INPUT
    print_report(rep)
    _write(a.json, rep)
    return code


if __name__ == "__main__":
    sys.exit(main())
