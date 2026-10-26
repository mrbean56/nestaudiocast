"""Offline self-test: the evidence run of the owner-app skeleton (brief G's "done when").

Three things, all in memory, none audible:

1. **the HTTP request matrix** — every endpoint of §5, good and bad input, over a real socket
   on ``127.0.0.1``, plus a second server that refuses a non-LAN client;
2. **the 10-minute simulated session** — 2400 ticks of 0.25 s with a *hostile* controller that
   asks for a reload on every tick: zero ``load``, zero ``seek``, zero ``play_at`` may leave the
   process after the start event;
3. **the crash-restart cleanup** — a session record left behind, then ``boot_cleanup()``.

``python -m sender.selftest --evidence evidence/40-sender-selftest.json`` regenerates the
archived evidence; ``python -m sender.service --self-test`` does the same through the service.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import time

from v020 import contracts as C

from .api import SenderAPI, serve_in_thread
from .config import Config
from .group_lock import boot_cleanup, read_session_record, session_record_path, write_session_record
from .log import EventLog
from .offline import FakeClock, HostileController, TrimController, seams
from .service import build_service
from .session import SessionManager

TICKS_10_MIN = 2400          # 600 s at 0.25 s
TICK_S = 0.25


def _client(port: int, method: str, path: str, body=None, *, headers=None, raw=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = raw
    head = dict(headers or {})
    if payload is None and body is not None:
        payload = json.dumps(body).encode()
        head.setdefault("Content-Type", "application/json")
    if payload is not None:
        head.setdefault("Content-Length", str(len(payload)))
    connection.putrequest(method, path, skip_host=False, skip_accept_encoding=True)
    for key, value in head.items():
        connection.putheader(key, value)
    connection.endheaders()
    if payload is not None:
        connection.send(payload)
    reply = connection.getresponse()
    data = reply.read()
    connection.close()
    parsed = None
    try:
        parsed = json.loads(data) if data else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = None
    return reply.status, reply.getheader("Content-Type") or "", parsed, data


class Matrix:
    def __init__(self) -> None:
        self.port = 0
        self.cases: list[dict] = []

    def check(self, name: str, method: str, path: str, expected, *, body=None, headers=None,
              raw=None, need_error=False) -> tuple[int, dict | None, bytes]:
        status, ctype, payload, data = _client(self.port, method, path, body, headers=headers,
                                               raw=raw)
        want = expected if isinstance(expected, (tuple, list)) else (expected,)
        ok = status in want
        if need_error:
            ok = ok and isinstance(payload, dict) and "error" in payload
        record = {"case": name, "method": method, "path": path, "status": status,
                  "expected": list(want), "ok": bool(ok)}
        if payload is not None and need_error:
            record["error"] = payload.get("error")
        self.cases.append(record)
        return status, ctype, payload, data

    @property
    def ok(self) -> bool:
        return all(case["ok"] for case in self.cases)

    @property
    def failed(self) -> list[dict]:
        return [case for case in self.cases if not case["ok"]]


def _temp_config(root: str, *, quiet_start: str = "", quiet_end: str = "",
                 quiet_policy: str = "refuse") -> Config:
    return Config(
        http_bind="127.0.0.1", http_port=C.HTTP_PORT_DEFAULT,
        quiet_start=quiet_start, quiet_end=quiet_end, quiet_policy=quiet_policy,
        state_dir=os.path.join(root, "state"),
        evidence_dir=os.path.join(root, "evidence"),
        receiver_dir=os.path.join(root, "receiver"),
        assets_dir=os.path.join(root, "assets"),
    ).validate()


# ---------------------------------------------------------------------------------------------
# 1. HTTP request matrix
# ---------------------------------------------------------------------------------------------
def http_matrix(log) -> dict:
    root = tempfile.mkdtemp(prefix="cast-sender-matrix-")
    try:
        os.makedirs(os.path.join(root, "assets", "measure"), exist_ok=True)
        with open(os.path.join(root, "assets", "measure", "click_train_2310.m4a"), "wb") as fh:
            fh.write(b"\x00" * 2048)
        with open(os.path.join(root, "assets", "measure", "notes.txt"), "w") as fh:
            fh.write("not served")
        config = _temp_config(root)
        service = build_service(config, offline=True, log=log,
                                state_dir=config.state_dir, spawn=lambda fn: fn(),
                                extra_allowed=("127.0.0.1",))
        server, _thread = serve_in_thread(service.api, port=0)
        server_refusing, _ = serve_in_thread(
            SenderAPI(service.sender, config=config, log=log, proxy=service.proxy,
                      receiver_dir=config.receiver_dir, assets_dir=config.assets_dir,
                      extra_allowed=()),
            port=0)
        matrix = Matrix()
        matrix.port = server.server_address[1]
        try:
            # -- happy paths ---------------------------------------------------------------
            status, _ctype, payload, _data = matrix.check("healthz", "GET", C.PATH_HEALTH, 200)
            health_ok = payload == {"ok": True, "owner": "cast-sender"}
            status, _ctype, payload, _data = matrix.check("status idle", "GET", C.PATH_STATUS, 200)
            fields_ok = isinstance(payload, dict) and all(k in payload for k in (
                "state", "session_id", "video_id", "position_video", "position_group",
                "digital_error_ms", "p95_ms", "rate", "steady_since", "quiet", "group_owner",
                "delta_v2", "last_event", "last_error"))
            status, ctype, payload, data = matrix.check(
                "ui", "GET", C.PATH_UI, 200, raw=None)
            ui_ok = b"<html" in data.lower() and b"cdn" not in data.lower()
            matrix.check("receiver missing", "GET", C.PATH_RECEIVER, 404, need_error=True)
            os.makedirs(config.receiver_dir, exist_ok=True)
            with open(os.path.join(config.receiver_dir, "receiver.html"), "w") as fh:
                fh.write("<!doctype html><title>receiver</title>")
            status, ctype, payload, data = matrix.check("receiver served", "GET",
                                                       C.PATH_RECEIVER, 200)
            receiver_ok = b"receiver" in data

            # -- share --------------------------------------------------------------------
            status, _ctype, payload, _data = matrix.check(
                "share ok", "POST", C.PATH_SHARE, 202,
                body={"url": "https://www.youtube.com/watch?v=QzomK1fdSUg", "start": 5,
                      "measure": False})
            share_ok = isinstance(payload, dict) and payload.get("session_id") \
                and payload.get("state") == C.STATE_RESOLVING
            session_id = (payload or {}).get("session_id")
            sid = getattr(service.sender._session, "sid", None)
            matrix.check("status playing", "GET", C.PATH_STATUS, 200)
            matrix.check("media manifest", "GET", f"{C.PATH_MEDIA_PREFIX}{sid}/manifest.mpd", 200)
            matrix.check("media manifest unknown sid", "GET",
                         f"{C.PATH_MEDIA_PREFIX}nope/manifest.mpd", 410, need_error=True)
            matrix.check("media bytes unavailable", "GET",
                         f"{C.PATH_MEDIA_PREFIX}{sid}/audio.m4a", 501, need_error=True)
            matrix.check("measure asset", "GET", "/assets/measure/click_train_2310.m4a", 200)
            matrix.check("measure asset wrong type", "GET", "/assets/measure/notes.txt", 403,
                         need_error=True)
            matrix.check("measure asset traversal", "GET", "/assets/measure/..%2Fstate", 400,
                         need_error=True)

            # -- control ------------------------------------------------------------------
            for action, extra in (("pause", {}), ("play", {}), ("seek", {"position": 42.5}),
                                  ("volume", {"level": 0.3}), ("volume", {"muted": True}),
                                  ("volume", {"level": 0.4, "muted": False}), ("stop", {})):
                matrix.check(f"control {action} {sorted(extra) or ''}".strip(), "POST",
                             C.PATH_CONTROL, 200, body={"action": action, **extra})
            status, _ctype, payload, _data = matrix.check("status after stop", "GET", C.PATH_STATUS, 200)
            stop_ok = isinstance(payload, dict) and payload.get("state") in (
                C.STATE_IDLE, C.STATE_ENDED)
            matrix.check("control play when idle", "POST", C.PATH_CONTROL, 409,
                         body={"action": "play"}, need_error=True)

            # -- bad input ----------------------------------------------------------------
            matrix.check("share empty body", "POST", C.PATH_SHARE, 400, raw=b"",
                         headers={"Content-Type": "application/json"}, need_error=True)
            matrix.check("share bad json", "POST", C.PATH_SHARE, 400, raw=b"{nope",
                         headers={"Content-Type": "application/json"}, need_error=True)
            matrix.check("share not an object", "POST", C.PATH_SHARE, 400, raw=b"[]",
                         headers={"Content-Type": "application/json"}, need_error=True)
            matrix.check("share missing url", "POST", C.PATH_SHARE, 400, body={"start": 1},
                         need_error=True)
            matrix.check("share url not a string", "POST", C.PATH_SHARE, 400, body={"url": 12},
                         need_error=True)
            matrix.check("share url not http", "POST", C.PATH_SHARE, 400,
                         body={"url": "ftp://example/x"}, need_error=True)
            matrix.check("share negative start", "POST", C.PATH_SHARE, 400,
                         body={"url": "https://e/x", "start": -1}, need_error=True)
            matrix.check("share unknown field", "POST", C.PATH_SHARE, 400,
                         body={"url": "https://e/x", "Start": 1, "autoplay": True},
                         need_error=True)
            matrix.check("share wrong content type", "POST", C.PATH_SHARE, 415,
                         raw=b'{"url":"https://e/x"}',
                         headers={"Content-Type": "text/plain"}, need_error=True)
            matrix.check("share body too large", "POST", C.PATH_SHARE, 413,
                         raw=b"{" + b"a" * (70 * 1024) + b"}",
                         headers={"Content-Type": "application/json"}, need_error=True)
            matrix.check("control unknown action", "POST", C.PATH_CONTROL, 400,
                         body={"action": "explode"}, need_error=True)
            matrix.check("control seek without position", "POST", C.PATH_CONTROL, 400,
                         body={"action": "seek"}, need_error=True)
            matrix.check("control volume level 0", "POST", C.PATH_CONTROL, 400,
                         body={"action": "volume", "level": 0}, need_error=True)
            matrix.check("control volume nothing", "POST", C.PATH_CONTROL, 400,
                         body={"action": "volume"}, need_error=True)
            matrix.check("control muted not bool", "POST", C.PATH_CONTROL, 400,
                         body={"action": "volume", "muted": "yes"}, need_error=True)

            # -- routing ------------------------------------------------------------------
            matrix.check("unknown path", "GET", "/nope", 404, need_error=True)
            matrix.check("GET on share", "GET", C.PATH_SHARE, 405, need_error=True)
            matrix.check("POST on status", "POST", C.PATH_STATUS, 405,
                         body={}, need_error=True)
            matrix.check("DELETE on ui", "DELETE", C.PATH_UI, 405, need_error=True)
            matrix.check("HEAD on status", "HEAD", C.PATH_STATUS, 405)

            # -- non-LAN client -----------------------------------------------------------
            status, _ctype, payload, _data = _client(server_refusing.server_address[1], "GET",
                                              C.PATH_HEALTH)
            refused = status == 403 and isinstance(payload, dict) and "error" in payload
            matrix.cases.append({"case": "non-LAN client refused", "method": "GET",
                                 "path": C.PATH_HEALTH, "status": status, "expected": [403],
                                 "ok": bool(refused), "error": (payload or {}).get("error")})
        finally:
            server.shutdown()
            server.server_close()
            server_refusing.shutdown()
            server_refusing.server_close()
        result = {
            "ok": matrix.ok and health_ok and fields_ok and ui_ok and receiver_ok
            and bool(share_ok) and stop_ok,
            "cases": len(matrix.cases),
            "passed": len(matrix.cases) - len(matrix.failed),
            "failed": matrix.failed,
            "checks": {
                "healthz_body": bool(health_ok),
                "status_has_all_5_fields": bool(fields_ok),
                "ui_is_self_contained": bool(ui_ok),
                "receiver_served_from_receiver_dir": bool(receiver_ok),
                "share_reply_shape": bool(share_ok),
                "stop_leaves_idle_or_ended": bool(stop_ok),
                "non_lan_refused": bool(refused),
            },
            "session_id": session_id,
        }
        service.close()
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
# 2. the 10-minute session: zero reloads after the start event
# ---------------------------------------------------------------------------------------------
def ten_minute_session(log, controller_factory=HostileController) -> dict:
    root = tempfile.mkdtemp(prefix="cast-sender-10min-")
    try:
        config = _temp_config(root)
        clock = FakeClock()
        link, group, proxy, resolver = seams(clock=clock)
        controller = controller_factory()
        sender = SessionManager(link=link, group=group, controller=controller, proxy=proxy,
                                config=config, log=log, resolver=resolver, clock=clock,
                                wall=clock.now, spawn=lambda fn: fn(),
                                state_dir=config.state_dir)
        reply = sender.share("https://www.youtube.com/watch?v=QzomK1fdSUg", start=0.0)
        started = dict(runtime_calls=_calls(link), state=sender.status()["state"],
                       group_calls=list(group.calls))
        marks = {name: _calls(link).count(name) for name in ("load", "seek", "play_at", "rate")}
        refusals_before = _count_events(log, "reload_refused")
        group_marks = {name: group.calls.count(name) for name in
                       ("prepare", "play", "pause", "seek", "stop")}
        loads_after = seeks_after = play_ats_after = 0
        for _ in range(TICKS_10_MIN):
            clock.advance(TICK_S)
            sender.tick()
        after = _calls(link)
        loads_after = after.count("load") - marks["load"]
        seeks_after = after.count("seek") - marks["seek"]
        play_ats_after = after.count("play_at") - marks["play_at"]
        group_after = {name: group.calls.count(name) - group_marks[name] for name in
                       ("prepare", "play", "pause", "seek", "stop")}
        status = sender.status()
        result = {
            "controller": type(controller).__name__,
            "session_id": reply["session_id"],
            "state_after_share": started["state"],
            "state_after_10_min": status["state"],
            "ticks": TICKS_10_MIN,
            "simulated_seconds": round(TICKS_10_MIN * TICK_S, 1),
            "start_commands": marks,
            "group_start_calls": started["group_calls"],
            "load_after_start": loads_after,
            "seek_after_start": seeks_after,
            "play_at_after_start": play_ats_after,
            "group_playhead_moves_after_start": {
                name: count for name, count in group_after.items() if name in ("prepare", "seek")},
            "rate_commands_after_start": after.count("rate") - marks["rate"],
            "controller_requests": {
                "loads": getattr(controller, "asked_loads", None),
                "seeks": getattr(controller, "asked_seeks", None),
                "play_at": getattr(controller, "asked_play_at", None),
            },
            "stepped": controller.steps,
            "refusal_events": _count_events(log, "reload_refused") - refusals_before,
            "digital_error_ms": status["digital_error_ms"],
            "p95_ms": status["p95_ms"],
            "rate": status["rate"],
            "position_video": status["position_video"],
            "position_group": status["position_group"],
            "lock_held": os.path.isfile(os.path.join(config.state_dir, "group.owner")),
        }
        result["ok"] = (loads_after == 0 and seeks_after == 0 and play_ats_after == 0
                        and group_after.get("prepare", 0) == 0 and group_after.get("seek", 0) == 0
                        and status["state"] == C.STATE_PLAYING)
        sender.close()
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _calls(link) -> list[str]:
    calls = getattr(link, "calls", None)
    if calls is None:
        return []
    return list(calls)


def _count_events(log, name: str) -> int:
    path = getattr(log, "path", None)
    if not path or not os.path.isfile(path):
        return 0
    total = 0
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            try:
                if json.loads(line).get("event") == name:
                    total += 1
            except json.JSONDecodeError:
                continue
    return total


# ---------------------------------------------------------------------------------------------
# 3. crash restart: boot_cleanup acts on our record only
# ---------------------------------------------------------------------------------------------
def crash_restart(log) -> dict:
    root = tempfile.mkdtemp(prefix="cast-sender-boot-")
    try:
        config = _temp_config(root)
        clock = FakeClock()
        link, group, proxy, resolver = seams(clock=clock)
        sender = SessionManager(link=link, group=group, controller=TrimController(),
                                proxy=proxy, config=config, log=log, resolver=resolver,
                                clock=clock, wall=clock.now, spawn=lambda fn: fn(),
                                state_dir=config.state_dir)
        sender.share("https://www.youtube.com/watch?v=QzomK1fdSUg")
        record_path = session_record_path(config.state_dir)
        record = read_session_record(record_path) or {}
        # A mute we set (the seam has no volume getter today, so the record is written by hand
        # to prove boot_cleanup's "only our mutes" rule end to end).
        record["mutes"] = [{"name": "Cuisine", "level": 0.35, "muted": False, "by": "cast-sender"},
                           {"name": "Cuisine", "level": 0.5, "muted": True, "by": "cast-relay"}]
        write_session_record(record_path, record)
        # "crash": no sender.close(), a fresh process comes up with fresh seams
        link2, group2, proxy2, _resolver2 = seams(clock=FakeClock())
        result = boot_cleanup(record_path, group=group2, link=link2, proxy=proxy2, log=log)
        # a foreign record must be left alone
        foreign_path = session_record_path(os.path.join(root, "state2"))
        write_session_record(foreign_path, {"owner": "cast-relay", "pid": 1, "session_id": "x"})
        link3, group3, proxy3, _ = seams(clock=FakeClock())
        foreign = boot_cleanup(foreign_path, group=group3, link=link3, proxy=proxy3, log=log)
        sender.close()
        return {
            "record": {"session_id": record.get("session_id"), "sid": record.get("sid"),
                       "group_prepared": record.get("group_prepared"),
                       "receiver_launched": record.get("receiver_launched")},
            "actions": result.get("actions"),
            "group_calls": list(group2.calls),
            "receiver_calls": list(link2.calls),
            "proxy_sessions_left": len(proxy2.sessions),
            "record_removed": not os.path.exists(record_path),
            "foreign_record_actions": foreign.get("actions"),
            "foreign_group_calls": list(group3.calls),
            "foreign_record_kept": os.path.exists(foreign_path),
            "ok": (result.get("actions") == ["group_stopped", "receiver_stopped",
                                             "proxy_closed", "mute_undone:Cuisine"]
                   and "stop" in group2.calls and "stop" in link2.calls
                   and len(proxy2.sessions) == 0 and not os.path.exists(record_path)
                   and foreign.get("actions") == [] and group3.calls == []
                   and os.path.exists(foreign_path)),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------------------------
def run(*, evidence_path: str | None = None) -> dict:
    root = tempfile.mkdtemp(prefix="cast-sender-selftest-")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    log_path = os.path.join(root, "sender-self-test.jsonl")
    log = EventLog(path=log_path)
    try:
        log("selftest_start", stamp=stamp, mode="sender skeleton", audible=False)
        matrix = http_matrix(log)
        hostile = ten_minute_session(log, HostileController)
        trim = ten_minute_session(log, lambda: TrimController(always_trim=True))
        cleanup = crash_restart(log)
        # the archived JSONL must live next to the other evidence, so copy it there
        archived_log = None
        if evidence_path:
            evidence_dir = os.path.dirname(os.path.abspath(evidence_path))
            os.makedirs(evidence_dir, exist_ok=True)
            archived_log = os.path.join(evidence_dir,
                                        f"sender-{time.strftime('%Y-%m-%d')}.jsonl")
            log.close()
            with open(log_path, "r", encoding="utf-8") as source, \
                    open(archived_log, "a", encoding="utf-8") as target:
                shutil.copyfileobj(source, target)
        result = {
            "suite": "sender offline self-test (brief G)",
            "stamp": stamp,
            "host": os.uname().nodename,
            "offline": True,
            "audible": False,
            "http_matrix": matrix,
            "no_reload": {"hostile_controller": hostile, "trim_controller": trim},
            "boot_cleanup": cleanup,
            "log": archived_log,
            "ok": bool(matrix.get("ok") and hostile.get("ok") and trim.get("ok")
                       and trim["rate_commands_after_start"] > 0 and cleanup.get("ok")),
        }
        if evidence_path:
            with open(evidence_path, "w", encoding="utf-8") as handle:
                json.dump(result, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        return result
    finally:
        try:
            log.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="sender.selftest")
    parser.add_argument("--evidence", default=None,
                        help="write the JSON result here (e.g. evidence/40-sender-selftest.json)")
    args = parser.parse_args(argv)
    result = run(evidence_path=args.evidence)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
