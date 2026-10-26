#!/usr/bin/env python3
"""The acoustic series of the v0.2 gate (notes/measurement-protocol.md §3-§4).

    tools/acoustic_v020.py --phone 192.168.2.148:39509 [--url http://192.168.2.21:7640]
                           [--minutes-at 1.5,5,8.5] [--hold-to 11] [--linearity]
                           [--bias-ms 6.0] [--json OUT]

What it does, in order, on ONE continuous session (>= 10 min):

  1. POST /api/share {"url": <share>, "measure": true} — the v0.2 equivalent of the v0.1
     ``--measure-tracks``: the kitchen plays the 2310 Hz click train while the video plays its
     own 350 Hz beep (contracts §5).
  2. Waits for a *settled* session: GET /api/status must say ``playing`` and ``steady_since``
     must be older than 12 s (the v0.1 rule that saved two windows out of three, A28).
  3. Records 60 s through the phone at each minute mark (default 1.5 / 5 / 8.5 min, the spacing
     A28 settled), with ``tools/phone_record.sh`` — the same recorder the v0.1 series used.
  4. Calls ``measure/analyze_offset.py`` **unchanged** on each recording, then applies the known
     instrument bias of +6 ms (evidence/23-acoustic-chain-selftest.json) by subtraction.
  5. Discards any window whose ``steady_since`` moved during the recording: a reload or a seek
     inside the 60 s mixes two alignments and must be thrown away, not averaged (A36).
  6. Refuses an alias-ambiguous window (more than one alias candidate inside ±150 ms), demands
     >= 8 kept pairs and sigma <= 40 ms per measurement (A28, consensus §6.5).
  7. Reports the drift of the three points in ms/10 min with its standard error, and — with
     ``--linearity`` — the protocol §4.4 linearity check: a second pair of recordings 100 ms
     apart in DELTA_V2 must move the measured offset by +100 ms (slope 1:1 ±10 %).

OWNER-SIDE PRECONDITION (this tool makes noise; it is written tonight, run at the gate):
  - an open audio window: the owner allows audible playback in the kitchen;
  - the telephone in wireless debugging, paired, ``adb devices`` listing it, lying where it
    hears both the TV and the kitchen (same spot for every measurement);
  - the v0.2 service deployed on 192.168.2.21:7640 and idle (no other session);
  - the test share URL whose soundtrack carries the 350 Hz beep (``--share-url``);
  - for ``--linearity`` only: WRITE access to /etc/castsender.conf (DELTA_V2 is changed twice)
    and permission to restart cast-sender.

Preconditions are checked, not assumed: ``--preflight`` prints what is missing and touches
nothing. ``--self-test`` runs the whole pipeline offline — synthetic recordings, a loopback fake
service, a stub recorder — and exits non-zero if any deliberate fault is not caught.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
ANALYZER = os.path.join(ROOT, "measure", "analyze_offset.py")
RECORD_CMD = "tools/phone_record.sh {phone} {sec} {out}"
DEFAULT_URL = "http://192.168.2.21:7640"

BIAS_MS = 6.0  # evidence/23-acoustic-chain-selftest.json: measured - true, subtract it
MAX_STDEV_MS = 40.0  # consensus §6.5 / protocol §5
MIN_PAIRS = 8  # A28: a window with fewer pairs says nothing
MAX_OFFSET_MS = 150.0  # protocol §5: the perceptual budget
SETTLED_S = 12.0  # A28: 12 s without a transport event before recording
DRIFT_LIMIT_MS = 60.0  # protocol §5: |projected 10 min| <= 60 ms
LINEARITY_TOL = 0.10  # protocol §4.4: 1:1 ±10 %


def redact(url: str) -> str:
    if not url or "://" not in url:
        return url or ""
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0]}/<redacted>"


def http(url: str, body: dict | None = None, timeout: float = 10.0) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:  # a refusal is a JSON body too (quiet_refused)
        try:
            return json.loads(exc.read().decode() or "{}") | {"_http": exc.code}
        except json.JSONDecodeError:
            return {"_http": exc.code, "error": str(exc)}


def status(base: str) -> dict:
    return http(base.rstrip("/") + "/api/status")


def share(base: str, url: str, measure: bool = True, start: float | None = None) -> dict:
    body = {"url": url, "measure": measure}
    if start is not None:
        body["start"] = start
    return http(base.rstrip("/") + "/api/share", body, timeout=30)


def control(base: str, action: str, **kw) -> dict:
    return http(base.rstrip("/") + "/api/control", {"action": action, **kw}, timeout=30)


def wait_settled(
    base: str, timeout_s: float, settled_s: float = SETTLED_S, tries: int = 6, wait_s: float = 10.0
) -> dict | None:
    """Wait until the service reports playing and a steady_since older than ``settled_s``."""
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        st = status(base)
        since = st.get("steady_since")
        if (
            st.get("state") == "playing"
            and isinstance(since, (int, float))
            and time.time() - since >= settled_s
        ):
            return st
        time.sleep(1.0)
    for _ in range(tries):
        st = status(base)
        since = st.get("steady_since")
        if (
            st.get("state") == "playing"
            and isinstance(since, (int, float))
            and time.time() - since >= settled_s
        ):
            return st
        print("  not settled yet (+%gs)" % wait_s, flush=True)
        time.sleep(wait_s)
    return None


def run_analyzer(
    wav: str, json_out: str, tv_freq: float, grp_freq: float, period: float, expect_ms: float | None
) -> dict:
    """Call measure/analyze_offset.py unchanged and read its JSON."""
    cmd = [
        PY,
        ANALYZER,
        wav,
        "--tv-freq",
        str(tv_freq),
        "--grp-freq",
        str(grp_freq),
        "--period",
        str(period),
        "--json",
        json_out,
    ]
    if expect_ms is not None:
        cmd += ["--expect-ms", str(expect_ms)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(json_out):
        raise RuntimeError(
            f"analyze_offset.py failed (rc={r.returncode}): {(r.stderr or r.stdout)[-300:]}"
        )
    return json.load(open(json_out))


def alias_ambiguous(res: dict, window_ms: float = MAX_OFFSET_MS) -> list[float]:
    """Alias candidates that would all pass the acceptance: more than one is ambiguous."""
    cands = res.get("alias_candidates_ms") or []
    inside = sorted(c for c in cands if abs(c) <= window_ms)
    return inside if len(inside) > 1 else []


def evaluate(res: dict, bias_ms: float, expect_ms: float | None = None) -> dict:
    """Turn one analyzer result into a gate verdict. Never invents a number."""
    out = {
        "pairs_kept": res.get("pairs_kept"),
        "pairs_rejected": res.get("pairs_rejected"),
        "stdev_ms": res.get("offset_stdev_ms"),
        "measured_ms": res.get("offset_median_ms"),
        "alias_candidates_ms": res.get("alias_candidates_ms"),
        "analyzer_verdict": res.get("verdict"),
    }
    reasons = []
    if res.get("offset_median_ms") is None:
        out.update(
            {
                "ok": False,
                "corrected_ms": None,
                "reasons": ["no paired onsets: the recording carries no measurement"],
            }
        )
        return out
    corrected = float(res["offset_median_ms"]) - bias_ms
    used = corrected
    cands = alias_ambiguous(res)
    if cands:
        if expect_ms is None:
            reasons.append(f"alias-ambiguous: candidates inside ±{MAX_OFFSET_MS:g} ms are {cands}")
        else:
            # lift the alias with the digital estimate, and re-check that only one survives
            best = min(cands, key=lambda c: abs(c - expect_ms))
            if abs(best - expect_ms) <= MAX_OFFSET_MS:
                used = best - bias_ms
                out["alias_lifted_by_expect_ms"] = expect_ms
            else:
                reasons.append(
                    f"alias not lifted: no candidate near the digital estimate "
                    f"{expect_ms:g} ms ({cands})"
                )
    out["corrected_ms"] = round(used, 1)
    if (res.get("pairs_kept") or 0) < MIN_PAIRS:
        reasons.append(f"only {res.get('pairs_kept')} kept pairs (< {MIN_PAIRS})")
    sd = res.get("offset_stdev_ms")
    if sd is not None and sd > MAX_STDEV_MS:
        reasons.append(f"sigma {sd:.1f} ms > {MAX_STDEV_MS:g} ms")
    if str(res.get("verdict", "")).startswith("AMBIGUOUS"):
        reasons.append("analyzer refused the window: " + str(res.get("verdict")))
    if abs(used) > MAX_OFFSET_MS:
        reasons.append(f"corrected offset {used:.0f} ms exceeds ±{MAX_OFFSET_MS:g} ms")
    out["ok"] = not reasons
    out["reasons"] = reasons
    return out


def series(points: list[dict]) -> dict:
    """Drift of the series in ms/10 min (least squares) and the protocol §5 verdict."""
    ok_pts = [
        p
        for p in points
        if p.get("ok")
        and isinstance(p.get("minute"), (int, float))
        and isinstance(p.get("corrected_ms"), (int, float))
    ]
    if len(ok_pts) < 3:
        return {
            "drift_ms_per_10min": None,
            "sigma_ms_per_10min": None,
            "note": f"{len(ok_pts)} valid point(s): a drift needs three",
            "ok": False,
        }
    x = np.array([p["minute"] * 60.0 for p in ok_pts], dtype=float)
    y = np.array([p["corrected_ms"] for p in ok_pts], dtype=float)
    slope, icpt = np.polyfit(x - x[0], y, 1)
    resid = y - (slope * (x - x[0]) + icpt)
    sxx = float(np.sum(((x - x[0]) - (x - x[0]).mean()) ** 2))
    se = float(math.sqrt(float(np.sum(resid**2)) / max(1, len(y) - 2) / sxx)) if sxx else None
    proj, sigma = slope * 600.0, (None if se is None else se * 600.0)
    counts = sigma is not None and abs(proj) > 2 * sigma
    ok = abs(proj) <= DRIFT_LIMIT_MS or not counts
    return {
        "drift_ms_per_10min": round(float(proj), 1),
        "sigma_ms_per_10min": None if sigma is None else round(float(sigma), 1),
        "trend_counts": bool(counts),
        "limit_ms_per_10min": DRIFT_LIMIT_MS,
        "points": len(ok_pts),
        "ok": bool(ok),
        "rule": "protocol §5: |projected 10 min| <= 60 ms, or a trend not beyond 2 sigma",
    }


def linearity_check(a_ms: float, b_ms: float, delta_step_ms: float = 100.0) -> dict:
    """Protocol §4.4: DELTA_V2 + 100 ms must move the measured offset by +100 ms (1:1 ±10 %)."""
    slope = (b_ms - a_ms) / delta_step_ms
    ok = abs(slope - 1.0) <= LINEARITY_TOL
    return {
        "delta_step_ms": delta_step_ms,
        "offset_a_ms": a_ms,
        "offset_b_ms": b_ms,
        "slope": round(slope, 3),
        "ok": bool(ok),
        "rule": f"slope 1:1 ±{LINEARITY_TOL * 100:g} % (protocol §4.4)",
    }


def synth_recording(
    path: str,
    offset_ms: float,
    seconds: float = 30.0,
    sr: int = 48000,
    tv_freq: float = 350.0,
    grp_freq: float = 2310.0,
    period: float = 1.0,
    noise: float = 0.006,
    seed: int = 3,
    jitter_ms: float = 0.0,
) -> None:
    """A room recording with known bands: TV beep at t=k*period, kitchen click offset by offset_ms."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    x = rng.normal(0, noise, n)

    def burst(t0: float, freq: float, dur: float, amp: float) -> None:
        i0 = int(round(t0 * sr))
        m = int(dur * sr)
        if i0 < 0 or i0 + m > n:
            return
        env = np.hanning(m)
        x[i0 : i0 + m] += amp * env * np.sin(2 * np.pi * freq * (np.arange(m) / sr))

    k = 0.5
    while k < seconds:
        burst(k, tv_freq, 0.075, 0.25)
        jit = float(rng.normal(0, jitter_ms / 1000.0)) if jitter_ms else 0.0
        burst(k + offset_ms / 1000.0 + jit, grp_freq, 0.030, 0.18)
        k += period
    pcm = np.clip(x, -1, 1)
    import wave

    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((pcm * 32767).astype("<i2").tobytes())


# --- the fake service used by --self-test ----------------------------------------------------


class FakeService:
    """A loopback stand-in for the contract's HTTP surface. No device, no sound.

    Meant to be used with ``assert_offline()`` installed, so an accidental real URL fails.
    """

    def __init__(self, state="playing", steady_age=60.0):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.state = state
        self.steady_since = time.time() - steady_age
        self.packets_to_group = 0
        self.shares = []
        self.actions = []
        self.group_level = 0.4
        self.position = 100.0
        self.session = "s1"
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/healthz":
                    return self._send(200, {"ok": True, "owner": "cast-sender"})
                if self.path == "/api/status":
                    return self._send(
                        200,
                        {
                            "state": outer.state,
                            "session_id": outer.session,
                            "video_id": "FAKE",
                            "position_video": outer.position,
                            "position_group": outer.position + 0.0,
                            "digital_error_ms": 0.0,
                            "p95_ms": 4.0,
                            "rate": 1.0,
                            "steady_since": outer.steady_since,
                            "quiet": False,
                            "group_owner": "cast-sender",
                            "delta_v2": 0.0,
                            "last_event": outer.actions[-1] if outer.actions else "share",
                            "last_error": None,
                        },
                    )
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/share":
                    outer.shares.append(body)
                    outer.state = "resolving"
                    outer.steady_since = time.time()
                    if outer.share_refuses:
                        outer.state = "quiet_refused"
                        return self._send(202, {"session_id": None, "state": "quiet_refused"})
                    outer.state = "playing"
                    outer.session = "s%d" % len(outer.shares)
                    outer.packets_to_group += 1
                    return self._send(202, {"session_id": outer.session, "state": "resolving"})
                if self.path == "/api/control":
                    outer.actions.append(body.get("action"))
                    if body.get("action") == "stop":
                        outer.state = "idle"
                        outer.session = None
                        return self._send(200, {"ok": True, "state": "idle"})
                    if body.get("action") == "pause":
                        outer.state = "paused"
                        return self._send(200, {"ok": True, "state": "paused"})
                    if body.get("action") == "play":
                        outer.state = "playing"
                        outer.steady_since = time.time()
                        return self._send(200, {"ok": True, "state": "playing"})
                    if body.get("action") == "seek":
                        outer.steady_since = time.time()
                        return self._send(200, {"ok": True, "state": outer.state})
                    if body.get("action") == "volume":
                        outer.group_level = body.get("level", outer.group_level)
                        return self._send(200, {"ok": True, "state": outer.state})
                    return self._send(400, {"ok": False, "state": outer.state})
                if self.path == "/_fake/reload":  # self-test only: a reload mid-window
                    outer.steady_since = time.time()
                    outer.packets_to_group += 1
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})

        self.share_refuses = False
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def assert_offline(tag: str) -> None:
    """Refuse every non-loopback connection: the self-tests are offline by construction."""
    import socket

    real = socket.socket.connect

    def guarded(sock, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"[{tag}] self-test tried to reach {host}: offline by construction")
        return real(sock, address, *a, **kw)

    socket.socket.connect = guarded


# --- self-test ------------------------------------------------------------------------------


def self_test(out: str) -> int:
    assert_offline("acoustic_v020")
    tmp = tempfile.mkdtemp(prefix="acoustic-v020-")
    cases = []

    def case(name, got, expect, detail=None):
        ok = bool(got == expect)
        cases.append({"case": name, "ok": ok, "got": got, "expected": expect, "detail": detail})
        print(("  ok   " if ok else "  FAIL ") + f"{name}: {got}")
        return ok

    try:
        # 1. the real analyzer, unchanged, on synthetic recordings with known offsets
        for true_ms in (0.0, 40.0, -60.0):
            wav = os.path.join(tmp, f"rec-{true_ms:+.0f}.wav")
            synth_recording(wav, true_ms, seconds=20.0)
            res = run_analyzer(wav, wav + ".json", 350.0, 2310.0, 1.0, None)
            ev = evaluate(res, bias_ms=0.0)  # synthetic chain has no +6 ms bias
            got = ev["corrected_ms"]
            cases.append(
                {
                    "case": f"known offset {true_ms:+.0f} ms",
                    "ok": bool(ev["ok"] and got is not None and abs(got - true_ms) <= 6.0),
                    "measured_ms": got,
                    "true_ms": true_ms,
                    "pairs": ev["pairs_kept"],
                    "stdev_ms": ev["stdev_ms"],
                    "reasons": ev["reasons"],
                }
            )
            print(
                ("  ok   " if cases[-1]["ok"] else "  FAIL ")
                + f"known offset {true_ms:+.0f} ms -> measured {got} ms "
                f"({ev['pairs_kept']} pairs, sd {ev['stdev_ms']})"
            )

        # 2. the +6 ms instrument bias is subtracted, not added
        ev = evaluate(
            {
                "offset_median_ms": 40.0,
                "offset_stdev_ms": 2.0,
                "pairs_kept": 40,
                "alias_candidates_ms": [40.0],
                "verdict": "PASS",
            },
            bias_ms=BIAS_MS,
        )
        case("instrument bias +6 ms is subtracted", ev["corrected_ms"], 34.0)

        # 3. a deliberately ambiguous alias: +480 ms reads as +480 inside a ±150 window twice
        wav = os.path.join(tmp, "ambiguous.wav")
        synth_recording(wav, 480.0, seconds=20.0)
        res = run_analyzer(wav, wav + ".json", 350.0, 2310.0, 1.0, None)
        ev = evaluate(res, bias_ms=0.0)
        cases.append(
            {
                "case": "alias-ambiguous window refused",
                "ok": not ev["ok"],
                "reasons": ev["reasons"],
                "measured_ms": ev["measured_ms"],
            }
        )
        print(
            ("  ok   " if not ev["ok"] else "  FAIL ")
            + "alias-ambiguous window refused: "
            + "; ".join(ev["reasons"])[:100]
        )

        # 4. too much dispersion: jittered onsets push sigma over 40 ms
        wav = os.path.join(tmp, "noisy.wav")
        synth_recording(wav, 10.0, seconds=25.0, jitter_ms=120.0)
        res = run_analyzer(wav, wav + ".json", 350.0, 2310.0, 1.0, None)
        ev = evaluate(res, bias_ms=0.0)
        cases.append(
            {
                "case": "dispersion > 40 ms refused",
                "ok": not ev["ok"],
                "stdev_ms": ev["stdev_ms"],
                "reasons": ev["reasons"],
            }
        )
        print(("  ok   " if not ev["ok"] else "  FAIL ") + f"dispersion refused: sd {ev['stdev_ms']} ms")

        # 5. too few pairs: a 6 s recording cannot carry 8 clicks... it can; use 4 s
        wav = os.path.join(tmp, "short.wav")
        synth_recording(wav, 0.0, seconds=4.0)
        res = run_analyzer(wav, wav + ".json", 350.0, 2310.0, 1.0, None)
        ev = evaluate(res, bias_ms=0.0)
        cases.append(
            {
                "case": "fewer than 8 pairs refused",
                "ok": not ev["ok"],
                "pairs": ev["pairs_kept"],
                "reasons": ev["reasons"],
            }
        )
        print(
            ("  ok   " if not ev["ok"] else "  FAIL ")
            + f"too few pairs refused ({ev['pairs_kept']} pairs)"
        )

        # 6. an injected drift across the series
        pts = [
            {"minute": 1.5, "corrected_ms": 2.0, "ok": True},
            {"minute": 5.0, "corrected_ms": 16.0, "ok": True},
            {"minute": 8.5, "corrected_ms": 30.0, "ok": True},
        ]
        s = series(pts)
        cases.append(
            {
                "case": "injected drift recovered (2 -> 30 ms over 7 min)",
                "ok": bool(abs(s["drift_ms_per_10min"] - 40.0) < 6.0 and s["trend_counts"]),
                "drift": s,
            }
        )
        print(
            ("  ok   " if cases[-1]["ok"] else "  FAIL ")
            + f"injected drift: {s['drift_ms_per_10min']} ms/10 min "
            f"(counts={s['trend_counts']}, ok={s['ok']}: within the 60 ms limit)"
        )
        big = [
            {"minute": 1.5, "corrected_ms": 0.0, "ok": True},
            {"minute": 5.0, "corrected_ms": 40.0, "ok": True},
            {"minute": 8.5, "corrected_ms": 80.0, "ok": True},
        ]
        sb = series(big)
        cases.append(
            {"case": "drift over the 60 ms/10 min limit rejected", "ok": bool(not sb["ok"]), "drift": sb}
        )
        print(
            ("  ok   " if not sb["ok"] else "  FAIL ")
            + f"over-limit drift rejected: {sb['drift_ms_per_10min']} ms/10 min"
        )
        flat = [
            {"minute": 1.5, "corrected_ms": 14.0, "ok": True},
            {"minute": 5.0, "corrected_ms": 15.0, "ok": True},
            {"minute": 8.5, "corrected_ms": 13.0, "ok": True},
        ]
        case("flat series accepted", series(flat)["ok"], True)

        # 7. the linearity check of protocol §4.4
        case("linearity 1:1 holds (+100 ms -> +98 ms)", linearity_check(10.0, 108.0)["ok"], True)
        case("linearity 1:1 broken (+100 ms -> +10 ms)", linearity_check(10.0, 20.0)["ok"], False)
        case("linearity sign inverted", linearity_check(10.0, -90.0)["ok"], False)

        # 8. the recording schedule discards a window cut by a reload (A36)
        with FakeService(state="playing") as svc:
            st0 = status(svc.url)
            t_before = st0["steady_since"]
            http(svc.url, {})  # warm-up POST (loopback) -- then the reload:
            http(svc.url + "/_fake/reload", {})  # a load lands mid-window
            st1 = status(svc.url)
            moved = st1["steady_since"] > t_before
            cases.append(
                {
                    "case": "window cut by a reload is discarded",
                    "ok": bool(moved),
                    "steady_since_before": t_before,
                    "steady_since_after": st1["steady_since"],
                }
            )
            print(
                ("  ok   " if moved else "  FAIL ")
                + "reload inside the window changes steady_since -> window discarded"
            )

        # 9. record -> analyze -> evaluate through the stub recorder, with the fake service
        with FakeService(state="idle") as svc:
            stub = os.path.join(tmp, "stub_record.sh")
            src = os.path.join(tmp, "rec-+40.wav")
            open(stub, "w").write(f'#!/bin/sh\ncp "{src}" "$3"\n')
            os.chmod(stub, 0o755)
            a = argparse.Namespace(
                url=svc.url,
                phone="127.0.0.1:0",
                share_url="http://fake/x",
                minutes_at=[0.01, 0.02, 0.03],
                record_s=20.0,
                hold_to=0.03,
                bias_ms=0.0,
                delta=0.0,
                tv_freq=350.0,
                grp_freq=2310.0,
                period=1.0,
                expect_ms=None,
                record_cmd=stub + " {phone} {sec} {out}",
                out_dir=tmp,
                keep_running=False,
                json_out=None,
                linearity=None,
                settle_timeout=20.0,
                settled_s=0.5,
                preflight_only=False,
                dry_run=False,
            )
            rep = run_series(a)
            pts = rep["points"]
            ok = bool(
                rep["session_started"]
                and len(pts) == 3
                and all(p.get("ok") and p.get("discarded") is False for p in pts)
                and rep.get("PASS")
            )
            cases.append(
                {
                    "case": "share -> 3 recordings -> analyze -> series verdict (stub recorder)",
                    "ok": ok,
                    "points": [
                        {k: p.get(k) for k in ("minute", "corrected_ms", "pairs_kept", "ok")}
                        for p in pts
                    ],
                    "series": rep.get("series"),
                    "verdict": rep.get("verdict"),
                    "stopped": rep.get("stop_sent"),
                }
            )
            print(("  ok   " if ok else "  FAIL ") + f"pipeline verdict: {rep.get('verdict')}")

        ok_all = all(c["ok"] for c in cases)
        res = {
            "tool": "acoustic_v020",
            "self_test": True,
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "offline": True,
            "targets_touched": [],
            "note": "synthetic recordings + a loopback fake service + a stub recorder; "
            "measure/analyze_offset.py is called unchanged through a subprocess",
            "cases": cases,
            "passed": sum(1 for c in cases if c["ok"]),
            "total": len(cases),
            "result": "SELF-TEST PASS" if ok_all else "SELF-TEST FAIL",
        }
        if out:
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            json.dump(res, open(out, "w"), indent=1, ensure_ascii=False)
            print(f"  -> {out}")
        print(f"  {res['result']} — {res['passed']}/{res['total']}")
        return 0 if ok_all else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- the real series ------------------------------------------------------------------------


def record(cmd: str, phone: str, secs: float, out: str, timeout: float = 180.0) -> None:
    full = cmd.format(phone=phone, sec=secs, out=out)
    r = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(
            f"recorder failed: {full} -> rc={r.returncode} {(r.stderr or r.stdout)[-200:]}"
        )


def run_series(a) -> dict:
    base = a.url.rstrip("/")
    rep = {
        "tool": "acoustic_v020",
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": base,
        "share": redact(a.share_url),
        "minutes_at": list(a.minutes_at),
        "record_s": a.record_s,
        "bias_ms": a.bias_ms,
        "delta": a.delta,
        "session_started": False,
        "points": [],
        "stop_sent": False,
    }
    os.makedirs(a.out_dir, exist_ok=True)
    h = http(base + "/healthz")
    rep["healthz"] = h
    if not (h.get("ok") and h.get("owner") == "cast-sender"):
        rep["verdict"] = "FAIL — no v0.2 service on " + base
        return rep
    st = status(base)
    rep["status_at_start"] = {k: st.get(k) for k in ("state", "session_id", "steady_since")}
    if st.get("state") not in ("idle", "ended"):
        rep["verdict"] = f"REFUSED — the service is {st.get('state')}: one session at a time"
        return rep
    if a.share_url is None:
        rep["verdict"] = "REFUSED — no --share-url (the beep video the protocol requires)"
        return rep
    r = share(base, a.share_url, measure=True)
    rep["share_reply"] = r
    if r.get("state") == "quiet_refused":
        rep["verdict"] = "REFUSED — quiet hours (quiet_refused); nothing was recorded"
        return rep
    rep["session_started"] = True
    t0 = time.time()
    settled = wait_settled(base, a.settle_timeout, getattr(a, "settled_s", SETTLED_S))
    rep["settled"] = bool(settled)
    if not settled:
        rep["verdict"] = "FAIL — the session never settled (no recording made)"
        if not a.keep_running:
            control(base, "stop")
            rep["stop_sent"] = True
        return rep
    try:
        for n, minute in enumerate(a.minutes_at, 1):
            row = {"n": n, "minute": minute}
            wait = t0 + minute * 60.0 - time.time()
            if wait > 0:
                time.sleep(wait)
            row["waited_s"] = round(time.time() - t0, 1)
            st = status(base)
            since = st.get("steady_since")
            need = getattr(a, "settled_s", SETTLED_S)
            if (
                st.get("state") != "playing"
                or not isinstance(since, (int, float))
                or time.time() - since < need
            ):
                row.update(
                    {
                        "ok": False,
                        "discarded": True,
                        "reasons": [
                            f"not settled at the mark (state {st.get('state')}, "
                            f"steady for {None if not isinstance(since, (int, float)) else round(time.time() - since, 1)} s)"
                        ],
                    }
                )
                rep["points"].append(row)
                print(f"  minute {minute}: skipped, not settled", flush=True)
                continue
            since_before = since
            wav = os.path.join(a.out_dir, f"48-acoustic-{n}.wav")
            t_rec = time.time()
            record(a.record_cmd, a.phone, a.record_s, wav)
            st2 = status(base)
            since_after = st2.get("steady_since")
            row["recording_s"] = round(time.time() - t_rec, 1)
            if isinstance(since_after, (int, float)) and since_after > since_before + 0.5:
                row.update(
                    {
                        "ok": False,
                        "discarded": True,
                        "wav": wav,
                        "reasons": [
                            f"a transport event landed inside the window "
                            f"(steady_since moved {round(since_before, 1)} -> "
                            f"{round(since_after, 1)}): two alignments in one "
                            f"recording (A36)"
                        ],
                    }
                )
                rep["points"].append(row)
                print(f"  minute {minute}: discarded, reload inside the window", flush=True)
                continue
            res = run_analyzer(wav, wav + ".json", a.tv_freq, a.grp_freq, a.period, a.expect_ms)
            ev = evaluate(res, a.bias_ms, a.expect_ms)
            row.update(
                {
                    "wav": wav,
                    "raw": {
                        k: res.get(k)
                        for k in (
                            "offset_median_ms",
                            "offset_stdev_ms",
                            "pairs_kept",
                            "pairs_rejected",
                            "alias_candidates_ms",
                            "ambiguity_window_ms",
                            "verdict",
                        )
                    },
                    "discarded": False,
                    **{
                        k: ev[k]
                        for k in (
                            "ok",
                            "corrected_ms",
                            "reasons",
                            "pairs_kept",
                            "stdev_ms",
                            "alias_candidates_ms",
                        )
                    },
                }
            )
            rep["points"].append(row)
            print(
                f"  minute {minute}: {ev['corrected_ms']} ms, sd {ev['stdev_ms']}, "
                f"{ev['pairs_kept']} pairs -> {'PASS' if ev['ok'] else 'REFUSED'}",
                flush=True,
            )
    finally:
        if not a.keep_running:
            control(base, "stop")
            rep["stop_sent"] = True
    rep["series"] = series(rep["points"])
    if a.linearity:
        rep["linearity"] = a.linearity
    ok_points = [p for p in rep["points"] if p.get("ok")]
    rep["PASS"] = bool(
        len(ok_points) >= 3 and rep["series"].get("ok") and (not a.linearity or a.linearity.get("ok"))
    )
    rep["verdict"] = (
        f"PASS — {len(ok_points)}/{len(rep['points'])} windows valid; series drift "
        f"{rep['series'].get('drift_ms_per_10min')} ± {rep['series'].get('sigma_ms_per_10min')}"
        f" ms/10 min"
        if rep["PASS"]
        else f"FAIL — {len(ok_points)}/{len(rep['points'])} valid windows; "
        f"{[p.get('reasons') for p in rep['points'] if not p.get('ok')][:3]}"
    )
    return rep


def preflight(a) -> dict:
    base = a.url.rstrip("/")
    out = {"url": base, "checks": {}}
    try:
        out["checks"]["healthz"] = http(base + "/healthz")
    except Exception as exc:  # noqa: BLE001
        out["checks"]["healthz"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        out["checks"]["status"] = {
            k: v
            for k, v in status(base).items()
            if k in ("state", "session_id", "steady_since", "quiet")
        }
    except Exception as exc:  # noqa: BLE001
        out["checks"]["status"] = {"error": f"{type(exc).__name__}: {exc}"}
    adb = shutil.which("adb")
    out["checks"]["adb"] = {"path": adb}
    if adb:
        r = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=20)
        out["checks"]["adb"]["devices"] = r.stdout.strip().splitlines()[1:]
    out["checks"]["phone_reachable_here"] = bool(a.phone)
    out["checks"]["share_url_given"] = bool(a.share_url)
    out["checks"]["recorder"] = a.record_cmd
    out["checks"]["analyzer"] = ANALYZER if os.path.exists(ANALYZER) else "MISSING"
    out["audio_window_required"] = [p["minute"] for p in []] or "yes: this tool makes noise"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="The v0.2 acoustic series (3 x 60 s in one session >= 10 min).",
        epilog="Owner-side precondition" + __doc__.split("OWNER-SIDE PRECONDITION")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", default=DEFAULT_URL, help=f"service base (default {DEFAULT_URL})")
    ap.add_argument(
        "--phone",
        default=os.environ.get("PHONE", ""),
        help="telephone IP:PORT for adb wireless debugging",
    )
    ap.add_argument("--share-url", default=None, help="the video whose soundtrack has the beep")
    ap.add_argument(
        "--minutes-at",
        default="1.5,5,8.5",
        help="recording start times in minutes (default 1.5,5,8.5 per A28)",
    )
    ap.add_argument("--record-s", type=float, default=60.0, help="recording length (60 s)")
    ap.add_argument(
        "--hold-to",
        type=float,
        default=11.0,
        help="keep the session alive until this many minutes (default 11)",
    )
    ap.add_argument("--settle-timeout", type=float, default=180.0)
    ap.add_argument(
        "--bias-ms",
        type=float,
        default=BIAS_MS,
        help="instrument bias subtracted from every measurement (default 6.0, "
        "evidence/23-acoustic-chain-selftest.json)",
    )
    ap.add_argument("--delta", type=float, default=0.0, help="DELTA_V2 in force (reported)")
    ap.add_argument("--tv-freq", type=float, default=350.0)
    ap.add_argument("--grp-freq", type=float, default=2310.0)
    ap.add_argument("--period", type=float, default=1.0, help="click period (aliasing window)")
    ap.add_argument(
        "--expect-ms",
        type=float,
        default=None,
        help="digital estimate, to lift an alias ambiguity (protocol §3)",
    )
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "evidence"))
    ap.add_argument("--json", help="write the series report here")
    ap.add_argument(
        "--keep-running",
        action="store_true",
        help="do not stop the session at the end (default: stop it)",
    )
    ap.add_argument(
        "--preflight",
        action="store_true",
        help="print what is missing; touches nothing, records nothing",
    )
    ap.add_argument("--record-cmd", default=RECORD_CMD, help="test hook: recorder template")
    ap.add_argument(
        "--linearity",
        default=None,
        help="JSON with two series results at DELTA and DELTA+0.1 s, to check §4.4",
    )
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="synthetic recordings + loopback fake service; exits non-zero on a "
        "deliberately broken input",
    )
    a = ap.parse_args(argv)
    a.minutes_at = [float(x) for x in str(a.minutes_at).split(",") if x.strip()]
    if isinstance(a.linearity, str):
        a.linearity = json.load(open(a.linearity))

    if a.self_test:
        return self_test(a.json)
    if a.preflight:
        print(json.dumps(preflight(a), indent=1, ensure_ascii=False))
        return 0
    if not a.phone:
        print(
            "acoustic_v020: --phone is required (wireless debugging) or use --self-test / --preflight",
            file=sys.stderr,
        )
        return 2
    rep = run_series(a)
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        json.dump(rep, open(a.json, "w"), indent=1, ensure_ascii=False)
        print(f"  -> {a.json}")
    print(
        json.dumps(
            {k: rep[k] for k in ("verdict", "PASS", "series") if k in rep}, indent=1, ensure_ascii=False
        )
    )
    return 0 if rep.get("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
