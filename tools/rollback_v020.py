#!/usr/bin/env python3
"""The v0.2 rollback test, levels 0 and 1 — the A27 method (evidence/28), with the receiver too.

    tools/rollback_v020.py [--url http://192.168.2.21:7640] [--json OUT]

before   : volume/mute/app of the 5 kitchen speakers, the TV, the group, and the app on the
           Chromecast (read-only GET_STATUS everywhere);
play     : the session under test (already running: this tool rolls back what is playing);
level 0  : POST /api/control {"action":"stop"} (contracts §8), then lib/emergency.py (the same
           cleanup as ExecStopPost), then stop the service, then stop *our* receiver app on
           192.168.2.80 if it still runs. Time to silence is measured;
check    : nothing plays anywhere, the group is idle, and the Chromecast no longer runs our app;
level 1  : lib/emergency.py --restore (the archived volume values);
after    : the same snapshot. PASS iff every level/mute equals `before` for the 5 speakers and
           the TV, and the Chromecast is left on its idle page.

Comparison is by equality on (level, muted), exactly as A27 did: a rollback that "looks silent"
but changed a level is a failure.

OWNER-SIDE PRECONDITION (written tonight, run at the gate):
  - an open audio window: the session under test is audible, and stopping it is the point;
  - root (or a sudo rule) on CT 121 for ``systemctl stop cast-sender`` and lib/emergency.py;
  - permission to stop the receiver app on 192.168.2.80 (only the receiver task may talk to it
    tonight, so this tool is not run tonight);
  - a v0.2 session running (the rollback is measured against a real session).

``--preflight`` snapshots and touches nothing. ``--self-test`` runs levels 0 and 1 against a
fake fleet, and four deliberately broken variants (a restore that misses a level, a group left
playing, our receiver left running, a restore that changes the TV's volume), exiting non-zero if
any of them passes.
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
PY = sys.executable
DEFAULT_URL = "http://192.168.2.21:7640"
OUR_APP = "28028CD2"
CHROMECAST = "192.168.2.80"
IDLE_APPS = (None, "", "Backdrop", "Idle")


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
            return {"_http": exc.code}
    except urllib.error.URLError as exc:
        return {"error": str(exc.reason)}


# --- fleets: the same interface over the LAN and over a fake --------------------------------

class LanFleet:
    """Every device call this tool makes: read-only GET_STATUS, plus the rollback actions."""

    def __init__(self, url: str = DEFAULT_URL):
        sys.path.insert(0, os.path.join(ROOT, "lib"))
        import groupguard
        from castv2 import CastLink, receiver_app
        from silence import GROUPS, KITCHEN, TV
        self.CastLink, self.receiver_app = CastLink, receiver_app
        self.groupguard = groupguard
        self.devices = dict(KITCHEN)
        self.devices.update(GROUPS)
        self.devices.update(TV)
        self.url = url

    # -- reads -------------------------------------------------------------------------------
    def snapshot(self) -> dict:
        out = {}
        for name, (host, port) in self.devices.items():
            link = self.CastLink(host, port)
            try:
                link.connect_platform()
                st = link.receiver_status()
                v = st.get("volume") or {}
                out[name] = {"level": round(float(v.get("level") or 0.0), 4),
                             "muted": bool(v.get("muted")),
                             "app_id": self.receiver_app(st)[0],
                             "app_name": self.receiver_app(st)[1]}
            except Exception as exc:                # noqa: BLE001
                out[name] = {"error": f"{type(exc).__name__}"}
            finally:
                link.close()
            time.sleep(0.05)
        groups = self.groupguard.discover_groups(timeout=4)
        if "Cuisine" in groups:
            link = self.CastLink(*groups["Cuisine"])
            try:
                link.connect_platform()
                out["group Cuisine"] = {"app_id": self.receiver_app(link.receiver_status())[0],
                                        "app_name": self.receiver_app(link.receiver_status())[1]}
            except Exception as exc:                # noqa: BLE001
                out["group Cuisine"] = {"error": f"{type(exc).__name__}"}
            finally:
                link.close()
        out["chromecast " + CHROMECAST] = self._chromecast_app()
        return out

    def _chromecast_app(self) -> dict:
        try:
            from castv2 import probe_endpoint
            r = probe_endpoint(CHROMECAST)
            return {"app_id": r.get("app_id"), "app_name": r.get("app_name"),
                    "error": r.get("error")}
        except Exception as exc:                    # noqa: BLE001
            return {"error": f"{type(exc).__name__}"}

    # -- actions -----------------------------------------------------------------------------
    def status(self) -> dict:
        return call(self.url.rstrip("/") + "/api/status")

    def control_stop(self) -> dict:
        return call(self.url.rstrip("/") + "/api/control", {"action": "stop"}, timeout=30)

    def emergency(self) -> dict:
        r = subprocess.run([PY, os.path.join(ROOT, "lib", "emergency.py")],
                           capture_output=True, text=True, timeout=120, cwd=ROOT)
        return {"rc": r.returncode, "tail": (r.stdout + r.stderr).strip()[-400:]}

    def emergency_restore(self) -> dict:
        r = subprocess.run([PY, os.path.join(ROOT, "lib", "emergency.py"), "--restore"],
                           capture_output=True, text=True, timeout=120, cwd=ROOT)
        return {"rc": r.returncode, "tail": (r.stdout + r.stderr).strip()[-400:]}

    def stop_service(self) -> dict:
        r = subprocess.run("sudo systemctl stop cast-sender", shell=True, capture_output=True,
                           text=True, timeout=120)
        return {"rc": r.returncode, "tail": (r.stdout + r.stderr).strip()[-300:]}

    def stop_our_receiver(self) -> dict:
        """Stop the app we launched on the Chromecast; never another sender's session."""
        link = self.CastLink(CHROMECAST)
        try:
            link.connect_platform()
            st = link.receiver_status()
            app_id, name, tr = self.receiver_app(st)
            if app_id != OUR_APP:
                return {"stopped": False, "why": f"the device runs {app_id} ({name}), not ours"}
            link.stop_app(tr)
            time.sleep(1.0)
            after = self.receiver_app(link.receiver_status())
            return {"stopped": after[0] != OUR_APP, "after": {"app_id": after[0],
                                                              "app_name": after[1]}}
        except Exception as exc:                    # noqa: BLE001
            return {"stopped": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            link.close()


class FakeFleet:
    """A fleet with no sockets: levels and apps in memory, one fault at a time.

    fault: None | "restore_misses_level" | "group_left_playing" | "receiver_left_running"
           | "restore_touches_tv"
    """

    def __init__(self, fault: str | None = None):
        self.fault = fault
        self.devices = {
            "Cuisine Fenetre Vert": (0.05, False), "Cuisine Frigo Orange": (0.55, False),
            "Cuisine Frigo Jaune": (0.55, False), "Cuisine Meuble Bleu": (0.0834, False),
            "Cuisine Meuble Rose": (0.0834, False), "TV Salon": (0.40, False),
        }
        self.apps = {k: None for k in self.devices}
        self.apps["Cuisine Frigo Orange"] = "Default Media Receiver"
        self.apps["group Cuisine"] = "Default Media Receiver"
        self.apps["chromecast " + CHROMECAST] = OUR_APP
        self.saved: dict | None = None
        self.calls: list[str] = []
        self.state = "playing"

    def snapshot(self) -> dict:
        out = {}
        for name, (level, muted) in self.devices.items():
            out[name] = {"level": level, "muted": muted, "app_id": self.apps.get(name),
                         "app_name": self.apps.get(name)}
        out["group Cuisine"] = {"app_id": self.apps.get("group Cuisine"),
                                "app_name": self.apps.get("group Cuisine")}
        out["chromecast " + CHROMECAST] = {"app_id": self.apps["chromecast " + CHROMECAST],
                                           "app_name": "VideoNetCaster"}
        return out

    def status(self) -> dict:
        return {"state": self.state, "session_id": "s1" if self.state == "playing" else None,
                "group_owner": "cast-sender"}

    def control_stop(self) -> dict:
        self.calls.append("control_stop")
        self.state = "idle"
        return {"ok": True, "state": "idle"}

    def emergency(self) -> dict:
        self.calls.append("emergency")
        self.saved = {k: v for k, v in self.devices.items()}
        for k in self.devices:
            if k != "TV Salon":
                self.devices[k] = (self.devices[k][0], True)     # mute only, never zero
        if self.fault != "group_left_playing":
            self.apps["group Cuisine"] = None
            for k in self.devices:
                if k != "TV Salon":
                    self.apps[k] = None
        return {"rc": 0}

    def stop_service(self) -> dict:
        self.calls.append("stop_service")
        return {"rc": 0}

    def stop_our_receiver(self) -> dict:
        self.calls.append("stop_our_receiver")
        if self.fault != "receiver_left_running":
            self.apps["chromecast " + CHROMECAST] = "Backdrop"
            return {"stopped": True}
        return {"stopped": False, "why": "receiver still running"}

    def emergency_restore(self) -> dict:
        self.calls.append("emergency_restore")
        if not self.saved:
            return {"rc": 1, "tail": "no saved state"}
        for k, v in self.saved.items():
            if self.fault == "restore_misses_level" and k == "Cuisine Frigo Orange":
                continue
            self.devices[k] = v
        if self.fault == "restore_touches_tv":
            self.devices["TV Salon"] = (0.10, False)
        return {"rc": 0}


# --- the test -------------------------------------------------------------------------------

def compare(before: dict, after: dict, keys) -> dict:
    """(level, muted) equality per device — the A27 rule, no tolerance."""
    diffs = {}
    for k in keys:
        b, a = before.get(k) or {}, after.get(k) or {}
        if (b.get("level"), b.get("muted")) != (a.get("level"), a.get("muted")):
            diffs[k] = {"before": {"level": b.get("level"), "muted": b.get("muted")},
                        "after": {"level": a.get("level"), "muted": a.get("muted")}}
    return diffs


def silent_after(snapshot: dict) -> dict:
    """Nothing plays: no app except an idle page, or muted. The TV is judged separately."""
    bad = {}
    for name, v in snapshot.items():
        if name.startswith("chromecast "):
            continue
        app = v.get("app_id")
        if app in IDLE_APPS:
            continue
        if v.get("muted"):
            continue
        bad[name] = {"app_name": v.get("app_name"), "app_id": app}
    return bad


def run_test(a, fleet) -> dict:
    rep = {"tool": "rollback_v020", "when": time.strftime("%Y-%m-%d %H:%M:%S"),
           "url": a.url, "method": "A27 / evidence/28: equality on (level, muted)"}
    rep["before"] = fleet.snapshot()
    rep["service_before"] = fleet.status()
    if a.require_session and rep["service_before"].get("state") != "playing":
        rep.update({"PASS": False,
                    "verdict": f"REFUSED — no playing session to roll back "
                               f"(state {rep['service_before'].get('state')})"})
        return rep
    keys = [k for k in rep["before"] if not k.startswith("chromecast ")
            and k != "group Cuisine"]
    rep["devices_compared"] = keys
    t0 = time.time()
    rep["level0"] = {"control_stop": fleet.control_stop()}
    rep["level0"]["emergency"] = fleet.emergency()
    rep["level0"]["service"] = fleet.stop_service()
    rep["level0"]["receiver"] = fleet.stop_our_receiver()
    rep["level0_s"] = round(time.time() - t0, 1)
    time.sleep(a.settle_s)
    rep["after_level0"] = fleet.snapshot()
    rep["playable_after_level0"] = silent_after(rep["after_level0"])
    rep["receiver_stopped"] = (rep["after_level0"].get("chromecast " + CHROMECAST, {})
                               .get("app_id") != OUR_APP)
    rep["level1"] = {"restore": fleet.emergency_restore()}
    time.sleep(a.settle_s)
    rep["after"] = fleet.snapshot()
    rep["volume_diffs"] = compare(rep["before"], rep["after"], keys)
    rep["tv_untouched"] = "TV Salon" not in rep["volume_diffs"]
    reasons = []
    if rep["playable_after_level0"]:
        reasons.append(f"still playing after level 0: {sorted(rep['playable_after_level0'])}")
    if not rep["receiver_stopped"]:
        reasons.append("our receiver app is still on the Chromecast after level 0")
    if rep["volume_diffs"]:
        reasons.append(f"{len(rep['volume_diffs'])} device(s) changed by the rollback: "
                       f"{sorted(rep['volume_diffs'])}")
    if not rep["tv_untouched"]:
        reasons.append("the rollback changed the TV's volume (it must never be touched)")
    rep["reasons"] = reasons
    rep["PASS"] = not reasons
    rep["verdict"] = ("PASS — silence in %ss, volumes identical before/after (including the "
                      "receiver app on %s)" % (rep["level0_s"], CHROMECAST)
                      if rep["PASS"] else "FAIL — " + "; ".join(reasons))
    return rep


def assert_offline(tag: str) -> None:
    import socket
    real = socket.socket.connect

    def guarded(sock, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"[{tag}] self-test tried to reach {host}: offline by construction")
        return real(sock, address, *a, **kw)

    socket.socket.connect = guarded


def self_test(out_path: str) -> int:
    assert_offline("rollback_v020")
    cases = []

    def case(name, ok, detail):
        cases.append({"case": name, "ok": bool(ok), "detail": detail})
        print(("  ok   " if ok else "  FAIL ") + name)

    a = argparse.Namespace(url=DEFAULT_URL, settle_s=0.01, require_session=True)
    fake = FakeFleet()
    rep = run_test(a, fake)
    case("healthy fleet: silence, volumes identical, receiver stopped, TV untouched",
         rep["PASS"] and not rep["volume_diffs"] and rep["receiver_stopped"],
         {k: rep.get(k) for k in ("PASS", "verdict", "level0_s", "volume_diffs",
                                  "playable_after_level0", "receiver_stopped")})
    case("the five speakers and the TV are the comparison set",
         sorted(rep["devices_compared"]) == sorted(
             ["Cuisine Fenetre Vert", "Cuisine Frigo Orange", "Cuisine Frigo Jaune",
              "Cuisine Meuble Bleu", "Cuisine Meuble Rose", "TV Salon"]),
         {"devices_compared": rep["devices_compared"]})

    for fault, note in [
            ("restore_misses_level", "a restore that misses one speaker's level is caught"),
            ("group_left_playing", "a group left playing after level 0 is caught"),
            ("receiver_left_running", "a receiver left running on the Chromecast is caught"),
            ("restore_touches_tv", "a restore that changes the TV's volume is caught")]:
        f = FakeFleet(fault=fault)
        r = run_test(a, f)
        case(note, (not r["PASS"]) and bool(r["reasons"]),
             {k: r.get(k) for k in ("PASS", "verdict", "reasons", "volume_diffs",
                                    "playable_after_level0", "receiver_stopped")})

    # a fleet where the speaker is already silenced at level 0 must still be judged: nothing plays
    f = FakeFleet()
    f.devices["Cuisine Meuble Rose"] = (0.0834, True)
    r = run_test(a, f)
    case("a speaker muted before the test stays muted after the restore",
         r["PASS"] and f.devices["Cuisine Meuble Rose"][1] is True,
         {"device": f.devices["Cuisine Meuble Rose"], "PASS": r["PASS"]})

    # no session -> refuse before any packet
    f = FakeFleet()
    f.state = "idle"
    r = run_test(argparse.Namespace(url=DEFAULT_URL, settle_s=0.01, require_session=True), f)
    case("no playing session -> REFUSED before any action",
         (not r["PASS"]) and r["verdict"].startswith("REFUSED") and f.calls == [],
         {"verdict": r["verdict"], "calls": f.calls})

    ok_all = all(c["ok"] for c in cases)
    res = {"tool": "rollback_v020", "self_test": True,
           "when": time.strftime("%Y-%m-%d %H:%M:%S"), "offline": True,
           "targets_touched": [],
           "note": "an in-memory fleet only: no socket, no speaker, no TV, no Chromecast. The "
                   "same compare()/silent_after() functions judge the LAN run.",
           "cases": cases, "passed": sum(1 for c in cases if c["ok"]), "total": len(cases),
           "result": "SELF-TEST PASS" if ok_all else "SELF-TEST FAIL"}
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        json.dump(res, open(out_path, "w"), indent=1, ensure_ascii=False)
        print(f"  -> {out_path}")
    print(f"  {res['result']} — {res['passed']}/{res['total']}")
    return 0 if ok_all else 1


def preflight(url: str) -> dict:
    fleet = LanFleet(url)
    snap = fleet.snapshot()
    return {"url": url,
            "service": {k: v for k, v in call(url.rstrip("/") + "/api/status").items()
                        if k in ("state", "session_id", "group_owner")},
            "snapshot": snap,
            "owner_precondition": ("root on CT 121 to stop the service and run lib/emergency.py; "
                                   "an open audio window; permission to stop our receiver app on "
                                   + CHROMECAST)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="v0.2 rollback levels 0/1 (A27 method, evidence/28).",
        epilog="Owner-side precondition" + __doc__.split("OWNER-SIDE PRECONDITION")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--settle-s", type=float, default=2.0)
    ap.add_argument("--allow-no-session", dest="require_session", action="store_false",
                    help="run even if nothing is playing (the gate wants a real session)")
    ap.add_argument("--json", help="write the report here")
    ap.add_argument("--preflight", action="store_true", help="snapshot only; touch nothing")
    ap.add_argument("--self-test", action="store_true",
                    help="in-memory fleet + four broken variants; exits non-zero if one passes")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test(a.json)
    if a.preflight:
        print(json.dumps(preflight(a.url), indent=1, ensure_ascii=False))
        return 0
    rep = run_test(a, LanFleet(a.url))
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        json.dump(rep, open(a.json, "w"), indent=1, ensure_ascii=False)
        print(f"  -> {a.json}")
    print(json.dumps({k: rep.get(k) for k in ("PASS", "verdict", "level0_s", "volume_diffs",
                                              "playable_after_level0", "receiver_stopped",
                                              "reasons")}, indent=1, ensure_ascii=False))
    return 0 if rep.get("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
