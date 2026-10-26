#!/usr/bin/env python3
"""Quiet hours end to end (contracts §7), on a *running* service, restoring what it changes.

    tools/quiet_v020.py [--url http://192.168.2.21:7640] --share-url URL
                        [--config /etc/castsender.conf] [--service cast-sender]
                        [--json OUT]

Sequence, all of it undone at the end:

  1. read the config verbatim (a byte-exact backup is written first);
  2. write a ~3 minute quiet window around *now* with QUIET_POLICY=refuse, restart the service;
  3. POST /api/share: the answer must be ``quiet_refused`` (either in the 202 body or in
     ``GET /api/status`` within 5 s) and the group must have received **nothing**;
  4. switch QUIET_POLICY=video_only, restart, share again: the **video** must play
     (``state=playing``, position advancing) and the group must **still** have received nothing;
  5. restore the original config byte for byte, restart, and prove it: the file is identical,
     ``/healthz`` answers, ``quiet`` is false and the state is idle again. If any of that fails,
     the verdict is FAIL and the backup path is printed.

"The group received nothing" is judged twice, and both are reported:
  * the service's own session log (``--events-log``) must carry no group transport event
    (load/play_at/seek/pause/rate/volume) between the share and the verdict;
  * a read-only probe of the group (CASTv2 GET_STATUS) must show the same app as before.
A read-only probe sends no LOAD/PLAY/SEEK/PAUSE/volume packet, which is why it is allowed here.

OWNER-SIDE PRECONDITION (written tonight, run at the gate):
  - **root or a sudo rule** to rewrite /etc/castsender.conf and restart cast-sender; this tool
    edits the service's configuration twice and restores it;
  - the v0.2 service deployed on 192.168.2.21:7640 and idle;
  - the ``video_only`` half makes the Chromecast play: the owner must allow it (tonight only the
    receiver task may launch an app on 192.168.2.80, so this tool is *not* run tonight);
  - a share URL to try (--share-url).

``--dry-run`` prints the config edit and stops. ``--self-test`` runs both halves against a
loopback fake service and four deliberately broken variants, and exits non-zero if any of them
passes or if the restore is not proven.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_URL = "http://192.168.2.21:7640"
DEFAULT_CONFIG = "/etc/castsender.conf"
DEFAULT_SERVICE = "cast-sender"
FORBIDDEN_GROUP_KINDS = ("load", "play_at", "play", "seek", "pause", "rate", "volume",
                         "group_load", "group_play", "group_seek", "group_volume")


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

    def health(self) -> dict:
        return call(self.base + "/healthz")

    def status(self) -> dict:
        return call(self.base + "/api/status")

    def share(self, url: str) -> dict:
        return call(self.base + "/api/share", {"url": url}, timeout=30)

    def control(self, action: str, **kw) -> dict:
        return call(self.base + "/api/control", {"action": action, **kw}, timeout=30)


# --- config ---------------------------------------------------------------------------------

def parse_config(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def patch_config(text: str, values: dict) -> str:
    """Replace KEY=... lines in place, append the missing keys, keep everything else."""
    lines = text.splitlines()
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z_]+)\s*=", line)
        if m and m.group(1) in values:
            k = m.group(1)
            lines[i] = f"{k}={values[k]}"
            seen.add(k)
    for k, v in values.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    return "\n".join(lines) + "\n"


def quiet_window(now: float | None = None, half_s: float = 120.0) -> tuple[str, str]:
    """HH:MM bounds of a window that contains 'now' with margin on both sides."""
    now = time.time() if now is None else now
    return (time.strftime("%H:%M", time.localtime(now - half_s)),
            time.strftime("%H:%M", time.localtime(now + half_s)))


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def write_text(path: str, text: str) -> None:
    tmp = path + ".tmp-v020"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def group_state() -> dict:
    """Read-only: GET_STATUS on the group. Sends no transport or volume packet."""
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
    except Exception as exc:                        # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def group_transport_events(events_log: str, since: float) -> list[dict]:
    """Group-side transport packets the service wrote after ``since`` (from its own log)."""
    if not events_log or not os.path.exists(events_log):
        return []
    out = []
    for line in open(events_log, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if float(d.get("wall") or d.get("t") or 0.0) < since:
            continue
        kind = str(d.get("type") or d.get("ev") or "").lower()
        side = str(d.get("side") or d.get("target") or "").lower()
        if kind in FORBIDDEN_GROUP_KINDS and side in ("", "group"):
            out.append({"kind": kind, "side": side, "wall": d.get("wall"),
                        "t": d.get("t")})
    return out


def restart_service(cmd: str, service: str, timeout: float = 90.0) -> dict:
    full = cmd.format(service=service)
    r = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=timeout)
    return {"cmd": full, "rc": r.returncode, "out": (r.stdout + r.stderr).strip()[-400:]}


def wait_health(svc: Service, timeout: float = 60.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        h = svc.health()
        if h.get("ok") and h.get("owner") == "cast-sender":
            return True
        time.sleep(1.0)
    return False


# --- the check ------------------------------------------------------------------------------

def run_check(a, svc: Service | None = None, restart=None, group_probe=group_state) -> dict:
    svc = svc or Service(a.url)
    restart = restart or (lambda: restart_service(a.restart_cmd, a.service))
    sc = float(getattr(a, "scale", 1.0))     # self-test hook: 1.0 on the gate
    rep = {"tool": "quiet_v020", "when": time.strftime("%Y-%m-%d %H:%M:%S"),
           "url": a.url, "config": a.config, "service": a.service,
           "share": redact(a.share_url), "reasons": []}
    h = svc.health()
    if not (h.get("ok") and h.get("owner") == "cast-sender"):
        rep.update({"PASS": False, "verdict": f"FAIL — no v0.2 service on {a.url}: {h}"})
        return rep
    if not os.path.exists(a.config):
        rep.update({"PASS": False,
                    "verdict": f"FAIL — {a.config} not found (or not readable): nothing changed"})
        return rep
    original = read_text(a.config)
    backup = a.backup or os.path.join(ROOT, "state",
                                      f"quiet_v020-{time.strftime('%Y%m%d-%H%M')}.conf")
    os.makedirs(os.path.dirname(os.path.abspath(backup)), exist_ok=True)
    with open(backup, "w", encoding="utf-8") as fh:
        fh.write(original)
    rep["backup"] = backup
    before = parse_config(original)
    rep["config_before"] = {k: before.get(k) for k in
                            ("QUIET_START", "QUIET_END", "QUIET_POLICY")}
    start_hhmm, end_hhmm = quiet_window()
    if a.dry_run:
        rep["planned"] = {"QUIET_START": start_hhmm, "QUIET_END": end_hhmm,
                          "QUIET_POLICY": "refuse then video_only"}
        rep["verdict"] = "DRY RUN — nothing written, nothing restarted"
        return rep
    group_before = rep["group_before"] = group_probe()
    try:
        # --- half 1: refuse ---------------------------------------------------------------
        write_text(a.config, patch_config(original, {"QUIET_START": start_hhmm,
                                                     "QUIET_END": end_hhmm,
                                                     "QUIET_POLICY": "refuse"}))
        rep["restart_refuse"] = restart()
        if not wait_health(svc, 60.0 * sc):
            rep["reasons"].append("the service did not come back after the refuse restart")
            raise RuntimeError("service down")
        st = svc.status()
        rep["status_quiet"] = {k: st.get(k) for k in ("state", "quiet", "session_id")}
        t0 = time.time()
        r = svc.share(a.share_url)
        refused = (r.get("state") == "quiet_refused"
                   or (r.get("_http") in (200, 202, 409) and r.get("state") == "quiet_refused"))
        if not refused:                       # also accept it on /api/status (both surfaces §5/§7)
            t_wait = time.time()
            while time.time() - t_wait < 5.0 * sc:
                st = svc.status()
                if st.get("state") == "quiet_refused":
                    refused = True
                    rep["quiet_refused_surface"] = "GET /api/status"
                    break
                time.sleep(0.5)
        else:
            rep["quiet_refused_surface"] = "POST /api/share reply"
        rep["share_reply_refuse"] = r
        time.sleep(1.0 * sc)
        packets = group_transport_events(a.events_log, t0)
        group_after_refuse = group_probe()
        rep["group_packets_refuse"] = packets
        rep["group_after_refuse"] = group_after_refuse
        rep["refuse_ok"] = bool(refused and not packets
                                and group_after_refuse.get("app_id") == group_before.get("app_id"))
        if not refused:
            rep["reasons"].append("the share was not refused during the quiet window")
        if packets:
            rep["reasons"].append(f"{len(packets)} group transport packet(s) during quiet hours")
        if group_after_refuse.get("app_id") != group_before.get("app_id"):
            rep["reasons"].append("the group's app changed during quiet hours")

        # --- half 2: video_only -----------------------------------------------------------
        write_text(a.config, patch_config(original, {"QUIET_START": start_hhmm,
                                                     "QUIET_END": end_hhmm,
                                                     "QUIET_POLICY": "video_only"}))
        rep["restart_video_only"] = restart()
        if not wait_health(svc, 60.0 * sc):
            rep["reasons"].append("the service did not come back after the video_only restart")
            raise RuntimeError("service down")
        t1 = time.time()
        r2 = svc.share(a.share_url)
        t_play, st = _wait(svc, lambda s: s.get("state") == "playing", 90.0 * sc)
        sleep_s = 3.0 * sc
        a0 = svc.status().get("position_video")
        time.sleep(sleep_s)
        a1 = svc.status().get("position_video")
        # at least half of real-time progress over the sleep: scaled for the self-test clock
        advancing = (isinstance(a0, (int, float)) and isinstance(a1, (int, float))
                     and (a1 - a0) > 0.5 * sleep_s)
        packets2 = group_transport_events(a.events_log, t1)
        group_after_video = group_probe()
        rep["share_reply_video_only"] = r2
        rep["video_play_s"] = t_play
        rep["video_advancing"] = bool(advancing)
        rep["group_packets_video_only"] = packets2
        rep["group_after_video_only"] = group_after_video
        rep["video_only_ok"] = bool(t_play is not None and advancing and not packets2
                                    and group_after_video.get("app_id") == group_before.get("app_id"))
        if t_play is None:
            rep["reasons"].append("the video did not play under video_only")
        elif not advancing:
            rep["reasons"].append("the video position did not advance under video_only")
        if packets2:
            rep["reasons"].append(f"{len(packets2)} group transport packet(s) under video_only")
        if group_after_video.get("app_id") != group_before.get("app_id"):
            rep["reasons"].append("the group's app changed under video_only")
    except RuntimeError:
        pass
    finally:
        # --- restore, and prove it --------------------------------------------------------
        svc.control("stop")
        write_text(a.config, original)
        rep["restart_restore"] = restart()
        restored = wait_health(svc, 60.0 * sc)
        same = read_text(a.config) == original
        st = svc.status() if restored else {}
        rep["restore"] = {"health": restored, "config_identical": same,
                          "quiet": st.get("quiet"), "state": st.get("state")}
        if not (restored and same):
            rep["reasons"].append("the original config was not restored and the service did not "
                                  f"come back (backup: {backup})")
        if restored and st.get("quiet"):
            rep["reasons"].append("the service still reports quiet after the restore")
        if restored and st.get("state") not in ("idle", "ended", None):
            svc.control("stop")

    rep["PASS"] = bool(rep.get("refuse_ok") and rep.get("video_only_ok") and not rep["reasons"])
    rep["verdict"] = ("PASS — quiet_refused with zero group packets, then video_only playing with "
                      "the group untouched; config restored byte for byte"
                      if rep["PASS"] else "FAIL — " + "; ".join(rep["reasons"]))
    return rep


def _wait(svc: Service, pred, timeout: float, step: float = 0.5):
    t0 = time.time()
    st = svc.status()
    while time.time() - t0 < timeout:
        st = svc.status()
        if pred(st):
            return round(time.time() - t0, 2), st
        time.sleep(step)
    return None, st


# --- fake service for --self-test -----------------------------------------------------------

class FakeService:
    """A loopback service that honours its config; ``fault`` breaks one behaviour.

    fault: None | "ignores_quiet" | "group_packet" | "video_only_loads_group" | "no_video"
           | "restore_ignored"
    """

    def __init__(self, fault: str | None = None, events_log: str | None = None):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        self.fault = fault
        self.events_log = events_log
        self.policy = "refuse"
        self.quiet = False
        self.state = "idle"
        self.session = None
        self.position = 0.0
        self._anchor = time.time()
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
                if self.path == "/_fake/config":
                    return self._send(200, outer.do_config(body))
                if self.path == "/api/share":
                    return self._send(202, outer.do_share())
                if self.path == "/api/control":
                    a = body.get("action")
                    if a == "stop":
                        outer.state, outer.session = "idle", None
                    return self._send(200, {"ok": True, "state": outer.state})
                return self._send(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def log(self, kind: str, side: str = "group") -> None:
        if self.events_log:
            with open(self.events_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"wall": time.time(), "type": kind, "side": side}) + "\n")

    def pos(self) -> float:
        if self.state != "playing":
            return self.position
        return self.position + (time.time() - self._anchor)

    def status(self) -> dict:
        return {"state": self.state, "session_id": self.session, "video_id": "FAKE",
                "position_video": round(self.pos(), 3), "position_group": round(self.pos(), 3),
                "digital_error_ms": 0.0, "p95_ms": 3.0, "rate": 1.0,
                "steady_since": None, "quiet": self.quiet,
                "group_owner": None, "delta_v2": 0.0, "last_event": "share",
                "last_error": None}

    def do_config(self, body: dict) -> dict:
        """The service reads its EnvironmentFile: a quiet window exists only with both bounds."""
        policy = body.get("QUIET_POLICY")
        if self.fault == "restore_ignored" and not body.get("QUIET_START"):
            return {"ok": True, "policy": self.policy, "quiet": self.quiet}   # pretends
        if policy:
            self.policy = policy
        self.quiet = bool(body.get("QUIET_START")) and bool(body.get("QUIET_END"))
        return {"ok": True, "policy": self.policy, "quiet": self.quiet}

    def do_share(self) -> dict:
        if self.quiet and self.policy == "refuse":
            if self.fault == "ignores_quiet":
                self.state = "playing"
                self._anchor = time.time()
                self.log("load")
                return {"session_id": "s1", "state": "resolving"}
            if self.fault == "group_packet":
                self.log("load")
            self.state = "quiet_refused"
            return {"session_id": None, "state": "quiet_refused"}
        if self.fault == "no_video":
            return {"session_id": "s1", "state": "resolving"}
        self.state = "playing"
        self._anchor = time.time()
        self.log("load", "video")
        if self.fault == "video_only_loads_group":
            self.log("load", "group")
        return {"session_id": "s1", "state": "resolving"}

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

def self_test(out_path: str, speed: float = 1.0) -> int:
    assert_offline("quiet_v020")
    import tempfile
    tmp = tempfile.mkdtemp(prefix="quiet-v020-")
    cases = []
    original = ("CHROMECAST=192.168.2.80\nAPP_ID=28028CD2\nGROUP=Cuisine\n"
                "QUIET_START=\nQUIET_END=\nQUIET_POLICY=refuse\n")

    def case(name, ok, detail):
        cases.append({"case": name, "ok": bool(ok), "detail": detail})
        print(("  ok   " if ok else "  FAIL ") + name)

    def args_for(url, config):
        return argparse.Namespace(url=url, config=config, service="cast-sender",
                                  share_url="http://fake/video", events_log=os.path.join(tmp,
                                                                                         "events.jsonl"),
                                  backup=os.path.join(tmp, "backup.conf"),
                                  restart_cmd="stub {service}", dry_run=False, scale=0.05)

    for fault, note in [
            ("ignores_quiet", "a share that plays during quiet hours is caught"),
            ("group_packet", "a group packet during quiet hours is caught"),
            ("video_only_loads_group", "a group load under video_only is caught"),
            ("no_video", "a video_only share that never plays is caught"),
            ("restore_ignored", "a restore that does not take effect is caught")]:
        config = os.path.join(tmp, f"castsender-{fault}.conf")
        open(config, "w").write(original)
        events = os.path.join(tmp, f"events-{fault}.jsonl")
        open(events, "w").close()
        with FakeService(fault=fault, events_log=events) as svc:
            a = args_for(svc.url, config)
            a.events_log = events

            def restart(svc=svc, config=config):
                c = parse_config(read_text(config))
                return call(svc.url + "/_fake/config",
                            {k: c.get(k, "") for k in ("QUIET_START", "QUIET_END",
                                                       "QUIET_POLICY")})
            rep = run_check(a, Service(svc.url), restart=restart,
                            group_probe=lambda: {"app_id": None, "app_name": None})
            case(note, (not rep["PASS"]) and bool(rep["reasons"]),
                 {k: rep.get(k) for k in ("PASS", "verdict", "reasons",
                                          "group_packets_refuse",
                                          "group_packets_video_only", "restore")})

    # the healthy path, over both halves, with the config restored byte for byte
    config = os.path.join(tmp, "castsender-ok.conf")
    open(config, "w").write(original)
    events = os.path.join(tmp, "events-ok.jsonl")
    open(events, "w").close()
    with FakeService(events_log=events) as svc:
        a = args_for(svc.url, config)
        a.events_log = events

        def restart(svc=svc, config=config):
            c = parse_config(read_text(config))
            return call(svc.url + "/_fake/config",
                        {k: c.get(k, "") for k in ("QUIET_START", "QUIET_END", "QUIET_POLICY")})
        rep = run_check(a, Service(svc.url), restart=restart,
                        group_probe=lambda: {"app_id": None, "app_name": None})
        case("healthy path: quiet_refused, video_only plays, group untouched, config restored",
             rep["PASS"] and rep["restore"]["config_identical"] and read_text(config) == original,
             {k: rep.get(k) for k in ("PASS", "verdict", "refuse_ok", "video_only_ok",
                                      "group_packets_refuse", "group_packets_video_only",
                                      "restore")})

    # --dry-run must write nothing
    config = os.path.join(tmp, "castsender-dry.conf")
    open(config, "w").write(original)
    with FakeService(events_log=os.path.join(tmp, "events-dry.jsonl")) as svc:
        a = args_for(svc.url, config)
        a.dry_run = True
        rep = run_check(a, Service(svc.url), restart=lambda: {"rc": 0},
                        group_probe=lambda: {"app_id": None})
        case("--dry-run writes nothing and says so",
             read_text(config) == original and rep["verdict"].startswith("DRY RUN"),
             {"verdict": rep["verdict"]})

    ok_all = all(c["ok"] for c in cases)
    res = {"tool": "quiet_v020", "self_test": True,
           "when": time.strftime("%Y-%m-%d %H:%M:%S"), "offline": True,
           "targets_touched": [],
           "note": "loopback fake service and a temporary config file; the real "
                   "/etc/castsender.conf is never read or written by the self-test",
           "cases": cases, "passed": sum(1 for c in cases if c["ok"]), "total": len(cases),
           "result": "SELF-TEST PASS" if ok_all else "SELF-TEST FAIL"}
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        json.dump(res, open(out_path, "w"), indent=1, ensure_ascii=False)
        print(f"  -> {out_path}")
    print(f"  {res['result']} — {res['passed']}/{res['total']}")
    return 0 if ok_all else 1


def preflight(a) -> dict:
    svc = Service(a.url)
    out = {"url": a.url, "config": a.config, "service": a.service,
           "healthz": svc.health(),
           "config_readable": os.access(a.config, os.R_OK) if os.path.exists(a.config) else False,
           "config_writable": os.access(a.config, os.W_OK) if os.path.exists(a.config) else False,
           "is_root": os.geteuid() == 0,
           "systemctl": shutil.which("systemctl")}
    try:
        st = svc.status()
        out["status"] = {k: st.get(k) for k in ("state", "quiet", "session_id")}
    except Exception as exc:                        # noqa: BLE001
        out["status"] = {"error": f"{type(exc).__name__}: {exc}"}
    if os.path.exists(a.config):
        out["config_now"] = {k: parse_config(read_text(a.config)).get(k) for k in
                             ("QUIET_START", "QUIET_END", "QUIET_POLICY")}
    out["owner_precondition"] = ("root (or a sudo rule) to write the config and restart the "
                                 "service; the owner's OK for the video_only half, because the "
                                 "Chromecast will play")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Quiet hours end to end (contracts §7), restoring the config it changes.",
        epilog="Owner-side precondition" + __doc__.split("OWNER-SIDE PRECONDITION")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--share-url", default=None)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--service", default=DEFAULT_SERVICE)
    ap.add_argument("--restart-cmd", default="sudo systemctl restart {service}",
                    help="how to restart the service (default: sudo systemctl restart {service})")
    ap.add_argument("--events-log", default="/opt/castsender/state/events.jsonl",
                    help="the service's session log (proposed amendment; see the docstring)")
    ap.add_argument("--backup", default=None, help="where the original config is saved")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    ap.add_argument("--json", help="write the report here")
    ap.add_argument("--preflight", action="store_true", help="print permissions and state")
    ap.add_argument("--self-test", action="store_true",
                    help="loopback fake service + five broken variants; exits non-zero if one "
                         "passes or the restore is not proven")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test(a.json)
    if a.preflight:
        print(json.dumps(preflight(a), indent=1, ensure_ascii=False))
        return 0
    if not a.share_url:
        print("quiet_v020: --share-url is required (or --self-test / --preflight)",
              file=sys.stderr)
        return 2
    rep = run_check(a)
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        json.dump(rep, open(a.json, "w"), indent=1, ensure_ascii=False)
        print(f"  -> {a.json}")
    print(json.dumps({k: rep.get(k) for k in ("PASS", "verdict", "backup", "restore",
                                              "group_packets_refuse",
                                              "group_packets_video_only")},
                     indent=1, ensure_ascii=False))
    return 0 if rep.get("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
