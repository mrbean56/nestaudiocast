#!/usr/bin/env python3
"""The foreign-takeover test (contracts §3.3): a clean end when another sender takes the device.

    tools/foreign_takeover_v020.py --url http://192.168.2.21:7640 --events-log PATH
                                   [--host 192.168.2.80] [--watch-s 30] [--yes-takeover]
                                   [--json OUT]

A v0.2 session is running on the Chromecast. A **second sender** — a throwaway process that
speaks bare CASTv2, exactly like a phone pressing Cast in the Google Home app — launches the
Default Media Receiver on the same device. The contract then demands, within one poll (<= 1 s):

  * the kitchen group is **stopped** (no leftover session on a group that contains audio only);
  * ``GET /api/status`` says ``idle`` with no ``session_id``;
  * the session log carries **exactly one** ``device_lost`` event;
  * **no relaunch** in the following 30 s: the state stays ``idle``, no new session appears, and
    no ``share``/``launch`` event is written. Relaunching needs a new share (§3.3).

Read-only first: the tool probes the Chromecast with GET_STATUS and refuses to act unless it is
idle *and* running our app (nothing to take over otherwise). The takeover itself is a LAUNCH from
a child process, so nothing of ours stays connected to the device.

OWNER-SIDE PRECONDITION (tonight this tool is written, not run: only the receiver task may launch
an app on 192.168.2.80):
  - a v0.2 session running (this tool takes the device away from it — the audio window must be
    open because the kitchen group was playing);
  - the events log the service writes (``--events-log``); contracts §5 has no events endpoint,
    so this path is a proposed amendment, and without it the count falls back to polling
    ``last_event`` (which can miss a duplicate — the report says which source it used);
  - ``--yes-takeover``: an explicit human decision to launch a foreign app on the Chromecast.

``--preflight`` only reads. ``--self-test`` runs the whole detector offline against a loopback
fake service and four deliberately broken variants (never stops, two ``device_lost``, relaunch,
group not stopped) and exits non-zero if any of them passes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = "http://192.168.2.21:7640"
DEFAULT_HOST = "192.168.2.80"
DEFAULT_APP = "CC1AD845"  # Default Media Receiver: what the Google Home app launches
OUR_APP = "28028CD2"  # VideoNetCaster (frozen)
DEVICE_LOST = "device_lost"
RELAUNCH_KINDS = ("share", "launch", "resolving", "starting")


def redact(url: str) -> str:
    if not url or "://" not in url:
        return url or ""
    scheme, rest = url.split("://", 1)
    return f"{scheme}://{rest.split('/', 1)[0]}/<redacted>"


def call(url: str, body: dict | None = None, timeout: float = 10.0) -> dict:
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

    def health(self) -> dict:
        return call(self.base + "/healthz")

    def status(self) -> dict:
        return call(self.base + "/api/status")

    def share(self, url: str) -> dict:
        return call(self.base + "/api/share", {"url": url}, timeout=30)

    def control(self, action: str, **kw) -> dict:
        return call(self.base + "/api/control", {"action": action, **kw}, timeout=30)


# --- read-only device probes ---------------------------------------------------------------


def device_state(host: str = DEFAULT_HOST) -> dict:
    """Read-only: what app is on the Chromecast right now. Sends GET_STATUS only."""
    try:
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        from castv2 import probe_endpoint

        r = probe_endpoint(host)
        return {k: r.get(k) for k in ("app_id", "app_name", "error", "connect_ms")}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def group_state() -> dict:
    """Read-only: is any app still running on the Cuisine group?"""
    try:
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import groupguard
        from castv2 import CastLink, receiver_app

        groups = groupguard.discover_groups(timeout=4)
        if "Cuisine" not in groups:
            return {"error": "group Cuisine not found by mDNS"}
        link = CastLink(*groups["Cuisine"])
        try:
            link.connect_platform()
            app_id, name, tr = receiver_app(link.receiver_status())
            return {"app_id": app_id, "app_name": name}
        finally:
            link.close()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def throwaway_launch(host: str, app_id: str) -> dict:
    """The second sender, as a *separate process*: connect, read, LAUNCH, leave."""
    sys.path.insert(0, os.path.join(ROOT, "lib"))
    from castv2 import CastLink, receiver_app

    out = {"host": host, "app_id": app_id, "when": time.time()}
    link = None
    try:
        link = CastLink(host)
        link.connect_platform()
        st = link.receiver_status()
        before = receiver_app(st)
        out["before"] = {"app_id": before[0], "app_name": before[1]}
        if before[0] not in (None, app_id):
            out["refused"] = f"the device already runs {before[0]} ({before[1]})"
            return out
        launch = link.launch(app_id, timeout=25)
        out["launch_reply"] = {k: launch.get(k) for k in ("type", "applications", "requestId")}
        time.sleep(1.0)
        after = receiver_app(link.receiver_status())
        out["after"] = {"app_id": after[0], "app_name": after[1]}
        out["ok"] = after[0] == app_id
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["ok"] = False
    finally:
        if link is not None:
            link.close()
    return out


def launch_from_throwaway(host: str, app_id: str, timeout: float = 90.0) -> dict:
    """The takeover, in a *separate process*: nothing of ours stays connected to the device."""
    cmd = [sys.executable, os.path.abspath(__file__), "--throwaway-launch", host, app_id]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"throwaway launch timed out after {timeout:g} s"}
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {
        "ok": False,
        "rc": r.returncode,
        "error": "the throwaway process printed no JSON: " + (r.stderr or "")[-200:],
    }


def read_events(path: str, since: float) -> list[dict]:
    """The service's session log, from ``since`` on. A missing file is reported, not invented."""
    if not path or not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if float(d.get("wall") or d.get("t") or 0.0) >= since:
                    out.append(d)
    except OSError:
        return []
    return out


def kinds_of(events: list[dict]) -> list[str]:
    return [str(e.get("type") or e.get("ev") or "") for e in events]


# --- the detector --------------------------------------------------------------------------


def assess(
    start: dict,
    timeline: list[dict],
    events: list[dict],
    group_after: dict | None,
    events_source: str,
    watch_s: float,
) -> dict:
    """Decide the contract's four conditions from a status timeline and the session log.

    ``timeline`` is one dict per poll after the takeover, oldest first:
    ``{"dt": seconds_since_takeover, "state": str, "session_id": str|None}``.
    """
    reasons = []
    t_take = float(start.get("t_takeover", 0.0))
    idle_row = next((r for r in timeline if r.get("state") == "idle" and not r.get("session_id")), None)
    if idle_row is None:
        reasons.append("the service never reached idle with no session")
    elif idle_row["dt"] > 1.5:
        reasons.append(f"idle only after {idle_row['dt']:.1f} s (contract: within one poll)")
    lost = [e for e in events if str(e.get("type") or e.get("ev")) == DEVICE_LOST]
    if len(lost) != 1:
        reasons.append(f"{len(lost)} device_lost event(s), expected exactly 1")
    idle_dt = idle_row["dt"] if idle_row else 0.0
    later = [r for r in timeline if r.get("dt", 0) > idle_dt]
    relaunch = [r for r in later if r.get("state") not in ("idle",)]
    new_session = [r for r in later if r.get("session_id")]
    if relaunch or new_session:
        reasons.append(
            "the service left idle after the takeover "
            f"({len(relaunch)} state(s), {len(new_session)} new session(s))"
        )
    relaunch_events = [
        str(e.get("type") or e.get("ev") or "")
        for e in events
        if str(e.get("type") or e.get("ev") or "") in RELAUNCH_KINDS
        and float(e.get("wall") or e.get("t") or 0.0) >= t_take - 0.001
    ]
    if relaunch_events:
        reasons.append(f"relaunch attempt in the log: {relaunch_events}")
    if group_after is not None and group_after.get("app_id"):
        reasons.append(
            f"the group still runs {group_after.get('app_name')} ({group_after.get('app_id')})"
        )
    watch_complete = watch_s >= 30.0
    if reasons:
        verdict = "FAIL — " + "; ".join(reasons)
    elif not watch_complete:
        verdict = (
            f"INCOMPLETE — the four conditions held over {watch_s:g} s, but the contract "
            f"asks for 30 s without a relaunch; rerun with --watch-s 30"
        )
    else:
        verdict = (
            f"PASS — idle {idle_dt:.1f} s after the takeover, one device_lost, group "
            f"stopped, no relaunch over {watch_s:g} s"
        )
    return {
        "PASS": not reasons and watch_complete,
        "conditions_held": not reasons,
        "watch_complete": watch_complete,
        "verdict": verdict,
        "reasons": reasons,
        "idle_after_s": None if idle_row is None else idle_dt,
        "device_lost_events": len(lost),
        "device_lost_source": events_source,
        "polls_after_takeover": len(timeline),
        "relaunch_states": relaunch,
        "new_sessions": new_session,
        "relaunch_events": relaunch_events,
        "group_after": group_after,
        "watch_s": watch_s,
    }


def run_check(a, service: Service, takeover, group_probe=group_state) -> dict:
    rep = {
        "tool": "foreign_takeover_v020",
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "url": a.url,
        "host": a.host,
        "foreign_app": a.app_id,
    }
    h = service.health()
    if not (h.get("ok") and h.get("owner") == "cast-sender"):
        rep.update({"PASS": False, "reasons": [f"no v0.2 service on {a.url}: {h}"]})
        return rep
    st = service.status()
    rep["status_before"] = {k: st.get(k) for k in ("state", "session_id", "group_owner", "last_event")}
    if st.get("state") != "playing" or not st.get("session_id"):
        rep.update(
            {
                "PASS": False,
                "reasons": ["no running session: a takeover needs one"],
                "status_before": rep["status_before"],
            }
        )
        return rep
    if a.group_probe:
        rep["group_before"] = group_probe()
    rep["device_before"] = device_state(a.host)
    if a.device_probe and rep["device_before"].get("app_id") != OUR_APP:
        rep.update(
            {
                "PASS": False,
                "device_before": rep["device_before"],
                "reasons": [
                    f"the Chromecast is not running our app "
                    f"({rep['device_before'].get('app_id')}): nothing to take over"
                ],
            }
        )
        return rep

    t_takeover = time.time()
    rep["t_takeover"] = t_takeover
    rep["takeover"] = takeover()
    started = time.time()
    timeline = []
    while time.time() - started < a.idle_timeout:
        s = service.status()
        timeline.append(
            {
                "dt": round(time.time() - t_takeover, 2),
                "state": s.get("state"),
                "session_id": s.get("session_id"),
            }
        )
        if s.get("state") == "idle" and not s.get("session_id"):
            break
        time.sleep(0.25)
    watch_end = t_takeover + max(a.watch_s, 1.0)
    while time.time() < watch_end:
        s = service.status()
        timeline.append(
            {
                "dt": round(time.time() - t_takeover, 2),
                "state": s.get("state"),
                "session_id": s.get("session_id"),
            }
        )
        time.sleep(1.0)
    events = read_events(a.events_log, t_takeover - 2.0)
    source = (
        f"events log {a.events_log}"
        if (a.events_log and os.path.exists(a.events_log))
        else "status polling only (no --events-log): a duplicate device_lost could be missed"
    )
    group_after = group_probe() if a.group_probe else None
    rep["events_seen"] = kinds_of(events)[:40]
    rep["events_source"] = source
    rep["timeline"] = timeline
    rep["takeover_result"] = (
        dict(rep["takeover"]) if isinstance(rep["takeover"], dict) else rep["takeover"]
    )
    rep["device_after"] = device_state(a.host) if a.device_probe else None
    rep.update(assess({"t_takeover": t_takeover}, timeline, events, group_after, source, a.watch_s))
    # a takeover that did not actually change the app proves nothing about the service
    if (
        a.device_probe
        and isinstance(rep["takeover_result"], dict)
        and not rep["takeover_result"].get("ok")
    ):
        rep["PASS"] = False
        rep["reasons"] = rep.get("reasons", []) + ["the foreign launch itself did not land"]
    return rep


# --- fake service for --self-test -----------------------------------------------------------


class FakeService:
    """A loopback v0.2 service that reacts to a takeover, plus four broken reactions."""

    def __init__(
        self, fault: str | None = None, events_log: str | None = None, idle_delay_s: float = 0.1
    ):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.fault = fault
        self.events_log = events_log
        self.idle_delay = idle_delay_s
        self.state = "playing"
        self.session = "s1"
        self.last_event = "share"
        self.t_take = None
        self.group_app = {"app_id": "CC1AD845", "app_name": "Default Media Receiver"}
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
                    outer.tick()
                    return self._send(200, outer.status())
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)  # the fake ignores the body; drain it
                if self.path == "/api/share":
                    return self._send(202, outer.do_share())
                if self.path == "/_fake/takeover":
                    return self._send(200, outer.do_takeover())
                return self._send(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    # -- log ---------------------------------------------------------------------------------
    def log(self, kind: str) -> None:
        if not self.events_log:
            return
        with open(self.events_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"wall": time.time(), "type": kind, "session_id": self.session}) + "\n")

    def status(self) -> dict:
        return {
            "state": self.state,
            "session_id": self.session,
            "video_id": "FAKE",
            "position_video": 10.0,
            "position_group": 10.0,
            "digital_error_ms": 2.0,
            "p95_ms": 5.0,
            "rate": 1.0,
            "steady_since": None,
            "quiet": False,
            "group_owner": "cast-sender" if self.session else None,
            "delta_v2": 0.0,
            "last_event": self.last_event,
            "last_error": None,
        }

    def do_share(self) -> dict:
        self.shares += 1
        self.session = f"s{self.shares + 1}"
        self.state = "playing"
        self.last_event = "share"
        self.log("share")
        return {"session_id": self.session, "state": "resolving"}

    def do_takeover(self) -> dict:
        """The 'second sender' took the device: the service must end cleanly (or not)."""
        self.t_take = time.time()
        if self.fault == "keeps_playing":
            self.last_event = "foreign_app"
            return {"ok": True}
        self.state, self.session = "idle", None
        self.last_event = DEVICE_LOST
        self.log(DEVICE_LOST)
        if self.fault == "two_events":
            self.log(DEVICE_LOST)
        if self.fault != "group_not_stopped":
            self.group_app = {"app_id": None, "app_name": None}
        return {"ok": True}

    def tick(self) -> None:
        if self.t_take is None:
            return
        dt = time.time() - self.t_take
        if self.fault == "relaunch" and dt > 0.8 and self.session is None:
            self.shares += 1
            self.session = f"r{self.shares}"
            self.state = "playing"
            self.last_event = "share"
            self.log("share")

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


def _args(url: str, events_log: str, watch_s: float = 1.2, **kw) -> argparse.Namespace:
    base = dict(
        url=url,
        host="127.0.0.1",
        app_id=DEFAULT_APP,
        events_log=events_log,
        watch_s=watch_s,
        idle_timeout=6.0,
        group_probe=True,
        device_probe=False,
        yes_takeover=True,
        json=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def self_test(out_path: str) -> int:
    assert_offline("foreign_takeover_v020")
    import tempfile

    tmp = tempfile.mkdtemp(prefix="foreign-v020-")
    cases = []

    def case(name, ok, detail):
        cases.append({"case": name, "ok": bool(ok), "detail": detail})
        print(("  ok   " if ok else "  FAIL ") + name)

    for fault, note in [
        ("keeps_playing", "a service that keeps playing is caught"),
        ("two_events", "two device_lost events are caught"),
        ("relaunch", "a relaunch inside the watch window is caught"),
        ("group_not_stopped", "a group still playing is caught"),
    ]:
        log = os.path.join(tmp, f"events-{fault}.jsonl")
        with FakeService(fault=fault, events_log=log) as svc:
            rep = run_check(
                _args(svc.url, log),
                Service(svc.url),
                takeover=lambda: call(svc.url + "/_fake/takeover", {}),
                group_probe=lambda: dict(svc.group_app),
            )
            case(
                note,
                bool(rep["reasons"]) and rep["verdict"].startswith("FAIL"),
                {
                    k: rep.get(k)
                    for k in (
                        "PASS",
                        "verdict",
                        "device_lost_events",
                        "idle_after_s",
                        "relaunch_states",
                        "reasons",
                    )
                },
            )

    # healthy service, short window: the four conditions hold, but the claim is INCOMPLETE
    log = os.path.join(tmp, "events-short.jsonl")
    with FakeService(events_log=log) as svc:
        rep = run_check(
            _args(svc.url, log, watch_s=2.0),
            Service(svc.url),
            takeover=lambda: call(svc.url + "/_fake/takeover", {}),
            group_probe=lambda: dict(svc.group_app),
        )
        case(
            "short watch window -> INCOMPLETE, never a PASS",
            rep["conditions_held"]
            and not rep["watch_complete"]
            and not rep["PASS"]
            and rep["verdict"].startswith("INCOMPLETE"),
            {"verdict": rep["verdict"]},
        )

    # the contract's own window, run once for real (30 s of wall time)
    log = os.path.join(tmp, "events-full.jsonl")
    with FakeService(events_log=log) as svc:
        rep = run_check(
            _args(svc.url, log, watch_s=30.0),
            Service(svc.url),
            takeover=lambda: call(svc.url + "/_fake/takeover", {}),
            group_probe=lambda: dict(svc.group_app),
        )
        case(
            "healthy service over the contract's 30 s window: PASS",
            rep["PASS"] and rep["verdict"].startswith("PASS"),
            {
                "verdict": rep["verdict"],
                "idle_after_s": rep["idle_after_s"],
                "device_lost_events": rep["device_lost_events"],
            },
        )

    # no session -> refuse before touching anything
    log = os.path.join(tmp, "events-nosession.jsonl")
    with FakeService(events_log=log) as svc:
        svc.state, svc.session = "idle", None
        rep = run_check(
            _args(svc.url, log), Service(svc.url), takeover=lambda: call(svc.url + "/_fake/takeover", {})
        )
        case(
            "no running session -> refused before the takeover",
            (not rep["PASS"]) and "no running session" in " ".join(rep["reasons"]),
            {"reasons": rep["reasons"]},
        )

    # the real mode refuses to launch without --yes-takeover (tonight's rule)
    ap = argparse.Namespace(host=DEFAULT_HOST, app_id=DEFAULT_APP)
    case(
        "--takeover castv2 without --yes-takeover is refused",
        guard_takeover(ap, lambda **kw: None) is False,
        {},
    )

    ok_all = all(c["ok"] for c in cases)
    res = {
        "tool": "foreign_takeover_v020",
        "self_test": True,
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "offline": True,
        "targets_touched": [],
        "note": "loopback fake service only; the Chromecast is never contacted, and the real "
        "mode refuses to launch without --yes-takeover",
        "cases": cases,
        "passed": sum(1 for c in cases if c["ok"]),
        "total": len(cases),
        "result": "SELF-TEST PASS" if ok_all else "SELF-TEST FAIL",
    }
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        json.dump(res, open(out_path, "w"), indent=1, ensure_ascii=False)
        print(f"  -> {out_path}")
    print(f"  {res['result']} — {res['passed']}/{res['total']}")
    return 0 if ok_all else 1


def guard_takeover(a, launcher) -> bool:
    """The one place that may launch a foreign app. Refuses unless the owner said yes."""
    if not getattr(a, "yes_takeover", False):
        print(
            "  REFUSED — a foreign launch on the Chromecast needs --yes-takeover "
            "(owner-side decision; tonight only the receiver task launches apps)",
            file=sys.stderr,
        )
        return False
    if getattr(a, "takeover", "castv2") == "stub":
        r = launcher(url=a.stub_url)
        return bool(r) and r is not False
    r = launcher(host=a.host, app_id=a.app_id)
    return bool(r) and r.get("ok", False)


def preflight(url: str, host: str) -> dict:
    svc = Service(url)
    out = {"url": url, "host": host, "healthz": svc.health()}
    try:
        st = svc.status()
        out["status"] = {k: st.get(k) for k in ("state", "session_id", "group_owner")}
    except Exception as exc:  # noqa: BLE001
        out["status"] = {"error": f"{type(exc).__name__}: {exc}"}
    out["device"] = device_state(host)
    out["group"] = group_state()
    out["owner_precondition"] = (
        "a running session, an open audio window, the events log, and "
        "--yes-takeover to launch a foreign app on the Chromecast"
    )
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Foreign-takeover test (contracts §3.3).",
        epilog="Owner-side precondition" + __doc__.split("OWNER-SIDE PRECONDITION")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--app-id", default=DEFAULT_APP, help="the foreign app (Default Media Receiver)")
    ap.add_argument(
        "--events-log",
        default="/opt/castsender/state/events.jsonl",
        help="the service's session log (proposed amendment; see the docstring)",
    )
    ap.add_argument(
        "--watch-s",
        type=float,
        default=30.0,
        help="no-relaunch window after the takeover (contract: 30 s)",
    )
    ap.add_argument("--idle-timeout", type=float, default=10.0)
    ap.add_argument("--takeover", choices=("castv2", "stub"), default="castv2")
    ap.add_argument("--stub-url", default=None, help="self-test hook: the fake service's URL")
    ap.add_argument(
        "--yes-takeover",
        action="store_true",
        help="required: launching a foreign app on 192.168.2.80 is the owner's call",
    )
    ap.add_argument(
        "--no-group-probe",
        dest="group_probe",
        action="store_false",
        help="skip the read-only group probe (then the group condition is unproven)",
    )
    ap.add_argument(
        "--no-device-probe",
        dest="device_probe",
        action="store_false",
        help="skip the read-only Chromecast probes (not recommended)",
    )
    ap.add_argument("--json", help="write the report here")
    ap.add_argument("--preflight", action="store_true", help="read-only: print what is there")
    ap.add_argument(
        "--self-test",
        action="store_true",
        help="offline detector test; exits non-zero if a broken variant passes",
    )
    ap.add_argument(
        "--throwaway-launch", nargs=2, metavar=("HOST", "APP_ID"), help=argparse.SUPPRESS
    )  # internal: the second-sender subprocess
    a = ap.parse_args(argv)
    if a.throwaway_launch:
        host, app_id = a.throwaway_launch
        res = throwaway_launch(host, app_id)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1
    if a.self_test:
        return self_test(a.json)
    if a.preflight:
        print(json.dumps(preflight(a.url, a.host), indent=1, ensure_ascii=False))
        return 0

    def takeover():
        """The second sender. Stub for the self-test, a throwaway CASTv2 process otherwise."""
        if not a.yes_takeover:
            return {"ok": False, "refused": "no --yes-takeover"}
        if a.takeover == "stub":
            return call(a.stub_url + "/_fake/takeover", {})
        return launch_from_throwaway(a.host, a.app_id)

    if a.device_probe and not a.yes_takeover and a.takeover == "castv2":
        print(
            "foreign_takeover_v020: refusing to launch a foreign app without --yes-takeover "
            "(tonight only the receiver task may launch apps on 192.168.2.80)",
            file=sys.stderr,
        )
        return 2
    rep = run_check(a, Service(a.url), takeover)
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        json.dump(rep, open(a.json, "w"), indent=1, ensure_ascii=False)
        print(f"  -> {a.json}")
    print(
        json.dumps(
            {
                k: rep.get(k)
                for k in ("PASS", "verdict", "idle_after_s", "device_lost_events", "events_source")
            },
            indent=1,
            ensure_ascii=False,
        )
    )
    return 0 if rep.get("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
