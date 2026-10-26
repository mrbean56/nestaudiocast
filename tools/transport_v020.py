#!/usr/bin/env python3
"""The transport matrix of notes/ACCEPTANCE.md §5, against a *running* v0.2 service.

    tools/transport_v020.py [--url http://192.168.2.21:7640] --share-url URL
                            [--duration-s 600] [--json OUT]

Every row of acceptance §5 becomes one JSON verdict with the time it took:

  1. play                 POST /api/share, then playing and the video position advancing
  2. video                the video side is playing the same media (position advances, id known)
  3. pause                POST /api/control pause, then paused and the position frozen
  4. resume               POST /api/control play, then playing and the position advancing again
  5. seek + / 6. seek -   POST /api/control seek, then the position lands within 1.5 s
  7. volume down          POST /api/control volume (while playing), then the group level follows
  8. volume up            idem, upwards
  9. stop                 POST /api/control stop, then idle and no session
 10. end of media         (needs --duration-s) seek to duration-4 s, then ended then idle
 11. new share mid-session POST /api/share while playing: a *new* session id, still one session

The volume check needs a level, and contracts §5 exposes none: this tool reads the group's level
read-only over CASTv2 (GET_STATUS only, never SET_VOLUME), or falls back to a ``group_volume``
field if the service happens to expose one. That gap is reported as a proposed amendment.

OWNER-SIDE PRECONDITION (this tool makes noise; written tonight, run at the gate):
  - an open audio window: the owner allows audible playback in the kitchen;
  - the v0.2 service deployed and idle on 192.168.2.21:7640;
  - the party URL whose video and audio come from one extraction (--share-url), and its real
    duration if the end-of-media row must be judged (--duration-s);
  - the group reachable read-only on the LAN (mDNS) for the volume rows.

``--preflight`` prints what is reachable and touches nothing. ``--self-test`` runs the whole
matrix against a loopback fake service and against four deliberately broken variants, and exits
non-zero if a broken variant passes or a good one fails.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = "http://192.168.2.21:7640"
SEEK_TOL_S = 1.5
POS_EPS_S = 0.30


def redact(url: str) -> str:
    if not url or "://" not in url:
        return url or ""
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0]}/<redacted>"


def call(url: str, body: dict | None = None, timeout: float = 15.0) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {},
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode() or "{}") | {"_http": exc.code}
        except json.JSONDecodeError:
            return {"_http": exc.code, "error": str(exc)}
    except urllib.error.URLError as exc:
        return {"error": str(exc.reason)}


class Service:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def status(self) -> dict:
        return call(self.base + "/api/status")

    def share(self, url: str, measure: bool = False) -> dict:
        return call(self.base + "/api/share", {"url": url, "measure": measure}, timeout=30)

    def control(self, action: str, **kw) -> dict:
        return call(self.base + "/api/control", {"action": action, **kw}, timeout=30)

    def health(self) -> dict:
        return call(self.base + "/healthz")


def group_volume() -> dict:
    """Read-only level/mute of the Cuisine group (GET_STATUS only). Never sets anything."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import groupguard
        from castv2 import CastLink
        groups = groupguard.discover_groups(timeout=4)
        if "Cuisine" not in groups:
            return {"error": "group Cuisine not found by mDNS"}
        link = CastLink(*groups["Cuisine"])
        try:
            link.connect_platform()
            st = link.receiver_status()
            v = st.get("volume") or {}
            return {"level": round(float(v.get("level") or 0.0), 4), "muted": v.get("muted")}
        finally:
            link.close()
    except Exception as exc:                       # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def wait_for(svc: Service, pred, timeout: float, step: float = 0.5):
    t0 = time.time()
    st = svc.status()
    while time.time() - t0 < timeout:
        st = svc.status()
        if pred(st):
            return round(time.time() - t0, 2), st
        time.sleep(step)
    return None, st


def advances(svc: Service, seconds: float = 3.0) -> tuple[bool, float]:
    a = svc.status().get("position_video")
    time.sleep(seconds)
    b = svc.status().get("position_video")
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return False, 0.0
    return (b - a) > POS_EPS_S, round(b - a, 3)


class Matrix:
    def __init__(self, svc: Service, share_url: str, duration_s: float | None,
                 volume_probe, keep_running: bool = False, sleep_scale: float = 1.0,
                 timeout_scale: float = 1.0):
        self.svc, self.share_url, self.duration_s = svc, share_url, duration_s
        self.volume_probe = volume_probe
        self.keep_running = keep_running
        self.sleep_scale = float(sleep_scale)     # self-test hook: 1.0 in the gate
        self.timeout_scale = float(timeout_scale)  # self-test hook: 1.0 in the gate
        self.rows: list[dict] = []
        self.first_session = None
        self.volume_before = None

    def t(self, seconds: float) -> float:
        return seconds * self.timeout_scale

    def record(self, name: str, ok: bool | None, seconds: float | None, detail: dict,
               unverified_reason: str | None = None) -> None:
        row = {"check": name, "pass": None if ok is None else bool(ok),
               "seconds": seconds, **detail}
        if unverified_reason:
            row["unverified"] = unverified_reason
        self.rows.append(row)
        tag = "UNVERIFIED" if ok is None else ("PASS" if ok else "FAIL")
        print(f"  {tag:10s} {name:26s} {json.dumps(detail)[:150]}", flush=True)

    def _volume(self) -> dict:
        if self.volume_probe is not None:
            v = self.volume_probe()
            return {**v, "source": "castv2 GET_STATUS (read-only)"}
        st = self.svc.status()
        if isinstance(st.get("group_volume"), (int, float)):
            return {"level": st["group_volume"], "muted": st.get("group_muted"),
                    "source": "status.group_volume (not in contracts §5)"}
        return {"error": "no group level available (pass --group-probe castv2)"}

    def run(self) -> dict:
        st0 = self.svc.status()
        if st0.get("state") not in ("idle", "ended"):
            self.record("precondition: idle service", False, 0.0, {"state": st0.get("state")})
            return {"checks": self.rows}
        self.volume_before = self._volume()

        # 1/2. play
        t0 = time.time()
        r = self.svc.share(self.share_url)
        self.record("play: share accepted", r.get("state") in ("resolving", "playing", "starting")
                    or r.get("_http") in (200, 202), round(time.time() - t0, 2), {"reply": r})
        t, st = wait_for(self.svc, lambda s: s.get("state") == "playing", self.t(60))
        self.record("play: state playing", t is not None, t, {"state": st.get("state")})
        self.first_session = st.get("session_id")
        t_v = 3.0 * self.sleep_scale
        adv, d = advances(self.svc, t_v)
        self.record("video: the video side is playing", adv, round(t_v, 2),
                    {"position_delta_s": d, "video_id": st.get("video_id"),
                     "position_video": st.get("position_video")})

        # 3. pause
        t0 = time.time()
        self.svc.control("pause")
        t, st = wait_for(self.svc, lambda s: s.get("state") == "paused", self.t(15))
        frozen = False
        if t is not None:
            a = self.svc.status().get("position_video")
            time.sleep(2.5 * self.sleep_scale)
            b = self.svc.status().get("position_video")
            frozen = isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(b - a) < POS_EPS_S
        self.record("pause: paused and frozen", bool(t is not None and frozen), t,
                    {"state": st.get("state"), "frozen": frozen})

        # 4. resume
        t0 = time.time()
        self.svc.control("play")
        t, st = wait_for(self.svc, lambda s: s.get("state") == "playing", self.t(30))
        adv2, d2 = advances(self.svc, 3.0 * self.sleep_scale) if t is not None \
            else (False, 0.0)
        self.record("resume: playing and advancing", bool(t is not None and adv2), t,
                    {"state": st.get("state"), "position_delta_s": d2})

        # 5/6. seek forward and back
        cur = self.svc.status().get("position_video") or 0.0
        for label, target in (("seek +: lands forward", min(cur + 60.0, (self.duration_s or 1e9) - 10.0)),
                              ("seek -: lands backward", max(10.0, cur - 45.0))):
            t0 = time.time()
            r = self.svc.control("seek", position=target)
            t, st = wait_for(self.svc, lambda s, tg=target: isinstance(s.get("position_video"),
                                                                     (int, float)) and
                             abs(s["position_video"] - tg) <= SEEK_TOL_S, self.t(40))
            self.record(label, t is not None, t,
                        {"target_s": round(target, 1), "position_video": st.get("position_video"),
                         "reply": r})

        # 7/8. volume, both directions, while playing
        back, st = wait_for(self.svc, lambda s: s.get("state") == "playing", self.t(30))
        if back is None:
            for label in ("volume down: group follows", "volume up: group follows"):
                self.record(label, False, None, {"state": st.get("state")},
                            unverified_reason=None)
            base = {"error": "the session is not playing"}
        else:
            base = self._volume()
        for label, delta in ([("volume down: group follows", -0.05),
                              ("volume up: group follows", +0.05)]
                             if back is not None else []):
            if not isinstance(base.get("level"), (int, float)):
                self.record(label, None, None, {"volume": base},
                            unverified_reason="no group level: " + str(base.get("error")))
                continue
            target = max(0.01, min(0.99, base["level"] + delta))
            t0 = time.time()
            r = self.svc.control("volume", level=round(target, 3))
            t, _ = wait_for(self.svc, lambda s, tg=target: abs(
                (self._volume().get("level") or -1) - tg) <= 0.02, self.t(12), step=0.5)
            got = self._volume()
            self.record(label, bool(t is not None and r.get("ok")), t,
                        {"target": round(target, 3), "level": got.get("level"),
                         "reply": r, "source": got.get("source")})
            base = got if isinstance(got.get("level"), (int, float)) else base

        # 9. stop
        t0 = time.time()
        r = self.svc.control("stop")
        t, st = wait_for(self.svc, lambda s: s.get("state") == "idle", self.t(20))
        self.record("stop: idle, no session", bool(t is not None and not st.get("session_id")), t,
                    {"state": st.get("state"), "session_id": st.get("session_id"), "reply": r})

        # 10. end of media
        if not self.duration_s:
            self.record("end of media: ended then idle", None, None, {},
                        unverified_reason="no --duration-s given: the duration is not in "
                                          "/api/status (proposed amendment)")
        else:
            t0 = time.time()
            self.svc.share(self.share_url, measure=False)
            wait_for(self.svc, lambda s: s.get("state") == "playing", self.t(60))
            self.svc.control("seek", position=max(0.0, self.duration_s - 4.0))
            t, st = wait_for(self.svc, lambda s: s.get("state") in ("ended", "idle"),
                             self.t(45))
            t2, st2 = wait_for(self.svc, lambda s: s.get("state") == "idle", self.t(20))
            self.record("end of media: ended then idle", bool(t is not None and t2 is not None),
                        round(time.time() - t0, 2),
                        {"state_after_end": st.get("state"), "final": st2.get("state")})

        # 11. new share mid-session
        self.svc.share(self.share_url, measure=False)
        t_play, _ = wait_for(self.svc, lambda s: s.get("state") == "playing", self.t(60))
        old = self.svc.status().get("session_id")
        t0 = time.time()
        r = self.svc.share(self.share_url, measure=False)
        t, st = wait_for(self.svc, lambda s, o=old: s.get("state") == "playing" and
                         s.get("session_id") not in (None, o), self.t(60))
        self.record("new share mid-session: new session", bool(t is not None),
                    round(time.time() - t0, 2),
                    {"old_session": old, "new_session": st.get("session_id"),
                     "state": st.get("state"), "reply": r})

        if not self.keep_running:
            self.svc.control("stop")
        if isinstance(self.volume_before.get("level"), (int, float)):
            self.svc.control("volume", level=self.volume_before["level"],
                             muted=bool(self.volume_before.get("muted")))
        return {"checks": self.rows}

    def verdict(self) -> dict:
        fails = [r for r in self.rows if r["pass"] is False]
        unver = [r for r in self.rows if r["pass"] is None]
        return {"passed": sum(1 for r in self.rows if r["pass"] is True),
                "failed": len(fails), "unverified": len(unver), "total": len(self.rows),
                "failed_checks": [r["check"] for r in fails],
                "unverified_checks": [r["check"] for r in unver]}


# --- fake service for --self-test -----------------------------------------------------------

class FakeService:
    """A loopback service with a real clock, plus four deliberate faults.

    fault: None | "volume" | "resume" | "newshare" | "stop" | "seek"
    """

    def __init__(self, fault: str | None = None, duration: float = 600.0):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.fault = fault
        self.duration = duration
        self.state = "idle"
        self.session = None
        self.session_n = 0
        self.position = 0.0
        self._anchor = time.time()
        self.group_volume = 0.40
        self.steady_since = None
        self.shares = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
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
                    return self._send(200, outer.status())
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/share":
                    return self._send(202, outer.do_share())
                if self.path == "/api/control":
                    return self._send(200, outer.do_control(body))
                return self._send(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def pos(self) -> float:
        if self.state != "playing":
            return self.position
        return self.position + (time.time() - self._anchor)

    def status(self) -> dict:
        if self.state == "playing" and self.pos() >= self.duration:
            self.position, self.state = self.duration, "ended"
            self._ended_at = time.time()
        if self.state == "ended" and time.time() - getattr(self, "_ended_at", 0) > 0.2:
            self.state, self.session, self.position = "idle", None, 0.0
        return {"state": self.state, "session_id": self.session, "video_id": "FAKE",
                "position_video": round(self.pos(), 3), "position_group": round(self.pos(), 3),
                "digital_error_ms": 2.0, "p95_ms": 6.0, "rate": 1.0,
                "steady_since": self.steady_since, "quiet": False,
                "group_owner": "cast-sender" if self.session else None, "delta_v2": 0.0,
                "last_event": "share" if self.session else "stop", "last_error": None,
                "group_volume": self.group_volume}

    def do_share(self) -> dict:
        self.shares += 1
        if self.state == "playing" and self.fault == "newshare":
            return {"session_id": self.session, "state": "resolving"}   # same session: broken
        self.session_n += 1
        self.session = f"s{self.session_n}"
        self.state = "playing"
        self.position = 0.0
        self._anchor = time.time()
        self.steady_since = time.time()
        return {"session_id": self.session, "state": "resolving"}

    def do_control(self, body: dict) -> dict:
        a = body.get("action")
        if a == "pause":
            self.position = self.pos()
            self.state = "paused"
        elif a == "play":
            if self.fault == "resume":
                return {"ok": True, "state": self.state}                 # ignores the resume
            self.state = "playing"
            self._anchor = time.time()
            self.steady_since = time.time()
        elif a == "seek":
            if self.fault == "seek":
                pass                                                      # ignores the seek
            else:
                self.position = float(body.get("position") or 0.0)
                self._anchor = time.time()
                self.steady_since = time.time()
                if self.position >= self.duration - 4.0:      # models an end of file
                    self.position, self.state = self.duration, "ended"
                    self._ended_at = time.time()
        elif a == "volume":
            if self.fault != "volume":                                    # "volume": ignores it
                self.group_volume = float(body.get("level", self.group_volume))
        elif a == "stop":
            if self.fault == "stop":
                return {"ok": True, "state": self.state}                 # keeps playing
            self.state = "idle"
            self.session = None
            self.position = 0.0
        return {"ok": True, "state": self.state}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def assert_offline(tag: str) -> None:
    import socket
    real = socket.socket.connect

    def guarded(sock, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"[{tag}] self-test tried to reach {host}: offline by construction")
        return real(sock, address, *a, **kw)

    socket.socket.connect = guarded


# --- self-test ------------------------------------------------------------------------------

def run_matrix(url: str, share_url: str, duration_s: float | None, probe=None,
               keep_running: bool = False, sleep_scale: float = 1.0,
               timeout_scale: float = 1.0) -> dict:
    svc = Service(url)
    m = Matrix(svc, share_url, duration_s, probe, keep_running, sleep_scale, timeout_scale)
    out = m.run()
    out.update(m.verdict())
    out["when"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out["url"] = url
    out["share"] = redact(share_url)
    return out


def self_test(out_path: str) -> int:
    assert_offline("transport_v020")
    cases = []

    def case(name, ok, detail):
        cases.append({"case": name, "ok": bool(ok), "detail": detail})
        print(("  ok   " if ok else "  FAIL ") + name)

    with FakeService() as svc:
        rep = run_matrix(svc.url, "http://fake/video", duration_s=600.0, sleep_scale=0.2, timeout_scale=0.08)
        ok = rep["failed"] == 0 and rep["unverified"] == 0 and rep["passed"] == 12
        case("healthy fake service: 12/12 rows pass", ok,
             {k: rep[k] for k in ("passed", "failed", "unverified", "total",
                                  "failed_checks")})
        # the same matrix must expose the time each check took
        case("every check carries a duration or an unverified reason",
             all((r["seconds"] is not None) or r.get("unverified") for r in rep["checks"]),
             {"rows": len(rep["checks"])})

    expect_fail = {
        "volume": "volume down: group follows",
        "resume": "resume: playing and advancing",
        "newshare": "new share mid-session: new session",
        "stop": "stop: idle, no session",
        "seek": "seek +: lands forward",
    }
    for fault, check_name in expect_fail.items():
        with FakeService(fault=fault) as svc:
            rep = run_matrix(svc.url, "http://fake/video", duration_s=600.0, sleep_scale=0.2, timeout_scale=0.08)
            bad = check_name in rep["failed_checks"]
            case(f"broken '{fault}' is caught ({check_name})", bad and rep["failed"] >= 1,
                 {"failed_checks": rep["failed_checks"]})

    # the end-of-media row cannot be judged without a duration: it must say so
    with FakeService() as svc:
        rep = run_matrix(svc.url, "http://fake/video", duration_s=None, sleep_scale=0.2)
        unv = [r for r in rep["checks"] if r["check"].startswith("end of media")]
        case("no duration -> end of media is unverified, not a pass",
             bool(unv and unv[0]["pass"] is None and unv[0].get("unverified")),
             {"row": unv[0] if unv else None})

    # a non-idle service must be refused before any packet is sent
    with FakeService() as svc:
        svc.state, svc.session = "playing", "s-existing"
        rep = run_matrix(svc.url, "http://fake/video", duration_s=None, sleep_scale=0.2)
        case("non-idle service refused before the matrix",
             bool(rep["failed"] == 1 and rep["checks"][0]["check"].startswith("precondition")),
             {"checks": rep["checks"]})

    ok_all = all(c["ok"] for c in cases)
    res = {"tool": "transport_v020", "self_test": True,
           "when": time.strftime("%Y-%m-%d %H:%M:%S"), "offline": True,
           "targets_touched": [],
           "note": "a loopback fake service only; no speaker, no group, no Chromecast",
           "cases": cases, "passed": sum(1 for c in cases if c["ok"]), "total": len(cases),
           "result": "SELF-TEST PASS" if ok_all else "SELF-TEST FAIL"}
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        json.dump(res, open(out_path, "w"), indent=1, ensure_ascii=False)
        print(f"  -> {out_path}")
    print(f"  {res['result']} — {res['passed']}/{res['total']}")
    return 0 if ok_all else 1


def preflight(url: str) -> dict:
    svc = Service(url)
    out = {"url": url, "healthz": svc.health()}
    try:
        st = svc.status()
        out["status"] = {k: st.get(k) for k in ("state", "session_id", "quiet", "group_owner",
                                                "delta_v2", "group_volume")}
        out["status_fields"] = sorted(st.keys())
    except Exception as exc:                        # noqa: BLE001
        out["status"] = {"error": f"{type(exc).__name__}: {exc}"}
    out["group_volume_castv2"] = group_volume()
    out["precondition"] = ("music will be audible: an open audio window is required; "
                           "the group volume is restored at the end")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Acceptance §5 transport matrix against a running v0.2 service.",
        epilog="Owner-side precondition" + __doc__.split("OWNER-SIDE PRECONDITION")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--share-url", default=None, help="the party URL to play")
    ap.add_argument("--duration-s", type=float, default=None,
                    help="real media duration, to judge the end-of-media row")
    ap.add_argument("--group-probe", choices=("castv2", "status"), default="castv2",
                    help="how to read the group level for the volume rows (default castv2, "
                         "read-only GET_STATUS)")
    ap.add_argument("--keep-running", action="store_true")
    ap.add_argument("--json", help="write the matrix report here")
    ap.add_argument("--preflight", action="store_true", help="print reachability; touch nothing")
    ap.add_argument("--self-test", action="store_true",
                    help="loopback fake service + four broken variants; exits non-zero if a "
                         "broken variant slips through")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test(a.json)
    if a.preflight:
        print(json.dumps(preflight(a.url), indent=1, ensure_ascii=False))
        return 0
    if not a.share_url:
        print("transport_v020: --share-url is required (or --self-test / --preflight)",
              file=sys.stderr)
        return 2
    probe = group_volume if a.group_probe == "castv2" else None
    rep = run_matrix(a.url, a.share_url, a.duration_s, probe, a.keep_running)
    rep["group_probe"] = a.group_probe
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        json.dump(rep, open(a.json, "w"), indent=1, ensure_ascii=False)
        print(f"  -> {a.json}")
    print(f"  {rep['passed']} passed, {rep['failed']} failed, {rep['unverified']} unverified "
          f"/ {rep['total']}")
    if rep["failed"]:
        return 1
    if rep["unverified"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
