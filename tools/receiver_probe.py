#!/usr/bin/env python3
"""Task C probe: does the Chromecast Salon honour `playbackRate`, and what does a rate change cost?

Read-only preflight first (eureka_info + CASTv2 GET_STATUS, which launches nothing), then the
measurements, each archived in its own file so a partial run is still evidence:

    evidence/41-preflight.json          device identity, app, volume — the gate that allows a launch
    evidence/41a-codec-matrix.json      what actually plays (not what canPlayType claims)
    evidence/41b-rate-trim.json         asked rate -> measured slope, least squares, 3 sigma test
    evidence/41c-rate-stall.json        what a rate change costs: buffering, dropped frames, gaps
    evidence/41d-clock.json             200 clock pings at 5 Hz: RTT, offset, residual
    evidence/41e-start-dispersion.json  10 x play_at: the video-side landing sigma
    evidence/41z-final-state.json       the device is back on Backdrop, volume as we found it

Safety, in this order:
  * nothing is launched unless the device is idle on Backdrop (--probe-only stops before that);
  * the device is muted (never set to level 0) for the whole run and put back as found;
  * every asset is video-only, so even a connected HDMI cable would stay silent;
  * the run always ends with `stop` + STOP on the session, and the device on Backdrop.

    ./tools/receiver_probe.py --probe-only
    ./tools/receiver_probe.py --make-assets
    ./tools/receiver_probe.py                      # everything, ~7 minutes

Media assets are generated on CT 121 with its own ffmpeg (`--make-assets`, needs no local ffmpeg)
and served by tools/receiver_serve.sh on 192.168.2.21:7640.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from castv2 import CastError, CastLink, receiver_app  # noqa: E402

NS = "urn:x-cast:com.beansden.nestaudiocast.v1"
APP_ID = "28028CD2"
HOST = "192.168.2.80"
HTTP_BASE = "http://192.168.2.21:7640"
SERVE_HOST = "192.168.2.21"
BACKDROP = {"E8C28D3C"}
EVID = os.path.join(ROOT, "evidence")
#: the exact command line, written into every evidence file it produces
COMMAND = " ".join(["./tools/receiver_probe.py"] + sys.argv[1:])

#: file -> (content_type, what it is) — all video-only, all <= 10 s except the rate asset
ASSETS = [
    ("codec-h264.mp4", "video/mp4", "H.264 Baseline 640x360 30fps"),
    ("codec-hevc.mp4", "video/mp4", "HEVC Main 640x360 30fps (hvc1)"),
    ("codec-vp9.mp4", "video/mp4", "VP9 profile 0 640x360 30fps"),
    ("codec-vp9.webm", "video/webm", "VP9 profile 0 640x360 30fps (webm control)"),
    ("codec-av1.mp4", "video/mp4", "AV1 main 640x360 30fps"),
    ("rate-90s.mp4", "video/mp4", "H.264 640x360 30fps, 90 s, for the rate slope"),
]
ASSET_SECONDS = {
    "codec-h264.mp4": 6,
    "codec-hevc.mp4": 6,
    "codec-vp9.mp4": 6,
    "codec-vp9.webm": 6,
    "codec-av1.mp4": 6,
    "rate-90s.mp4": 90,
}


def now_ms() -> float:
    return time.monotonic() * 1000.0


def redact(text: str) -> str:
    """A googlevideo URL is a short-lived token: scheme://host/<6 chars>… everywhere."""
    out = []
    for tok in str(text).replace('"', " ").split():
        if "googlevideo" in tok or tok.startswith("http"):
            try:
                scheme, rest = tok.split("://", 1)
                host = rest.split("/", 1)[0]
                out.append("%s://%s/<redacted>" % (scheme, host))
            except ValueError:
                out.append("<redacted>")
        else:
            out.append(tok)
    return " ".join(out)


def first_status(reply: dict) -> dict:
    """The first entry of a MEDIA_STATUS array, tolerating `status: {}` and `status: []`.

    A Cast device may serialise an empty repeated field as `{}` instead of `[]`; indexing that
    with [0] raises KeyError, which is how this probe once lost a whole rate window.
    """
    st = reply.get("status") if isinstance(reply, dict) else None
    if isinstance(st, list):
        return (st[0] if st else {}) or {}
    if isinstance(st, dict):
        return st
    return {}


def fit(pts):
    """Least squares slope of media vs wall (both seconds), plus its standard error."""
    n = len(pts)
    if n < 3:
        return None
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if sxx <= 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    a = sxy / sxx
    b = my - a * mx
    resid = [p[1] - (a * p[0] + b) for p in pts]
    s2 = sum(r * r for r in resid) / max(1, n - 2)
    se = (s2 / sxx) ** 0.5
    return {
        "n": n,
        "ratio": a,
        "ratio_sigma": se,
        "residual_max_ms": max(abs(r) for r in resid) * 1000.0,
        "span_s": pts[-1][0] - pts[0][0],
    }


def robust(pairs, key):
    vals = sorted(p[key] for p in pairs)
    if not vals:
        return {}
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals]) if len(vals) > 1 else 0.0
    p95 = vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))]
    return {"median": med, "p95": p95, "min": vals[0], "max": vals[-1], "sigma_mad": 1.4826 * mad}


# ---------------------------------------------------------------------------------------------
class Recv:
    """Our app on the device: launch, speak the custom namespace, collect what it says."""

    def __init__(self, host: str, app_id: str, log=print):
        self.host, self.app_id, self.log = host, app_id, log
        self.link: CastLink | None = None
        self.transport = ""
        self.seq = 0
        self.states: list[dict] = []
        self.hello: dict | None = None
        self.errors: list[dict] = []
        self.intents: list[dict] = []
        self.ended: list[dict] = []
        self.replies: list[dict] = []
        self._pending: list[dict] = []

    # -- wire ------------------------------------------------------------------------------
    def connect(self) -> None:
        self.link = CastLink(self.host)
        self.link.connect_platform()

    def receiver_status(self) -> dict:
        return self.link.receiver_status()

    def app_status(self) -> tuple[str | None, str | None, str | None]:
        return receiver_app(self.receiver_status())

    def launch(self, timeout: float = 25.0) -> dict:
        st = self.link.launch(self.app_id, timeout=timeout)
        app_id, name, tr = receiver_app(self.link.receiver_status())
        self.transport = tr or ""
        return {"launch_reply_app": app_id, "app_name": name, "transport_id": self.transport, "raw": st}

    def attach(self) -> None:
        if not self.transport:
            _, _, self.transport = self.app_status()
        self.link.connect_transport(self.transport)
        time.sleep(0.2)

    def send(self, msg_type: str, **fields) -> int:
        self.seq += 1
        payload = {"type": msg_type, "seq": self.seq}
        payload.update(fields)
        self.link.send(self.transport, NS, payload)
        return self.seq

    def est_rx(self, fresh_within: float = 5.0) -> float | None:
        """The receiver clock right now, extrapolated from its latest state sample."""
        st = self.latest_state()
        if not st or (time.time() - st.get("_t", 0)) > fresh_within:
            return None
        return st["rx_ms"] + (now_ms() - st.get("_t_mono", now_ms()))

    def pump(self, seconds: float = 0.02) -> list[dict]:
        """Collect receiver messages into typed buckets, return the new ones."""
        self.link.pump(seconds)
        fresh, self.link.inbox = self.link.inbox, []
        out = []
        for rec in fresh:
            if rec["namespace"] != NS:
                continue
            p = rec["payload"] or {}
            t = p.get("type")
            p["_t"] = rec["t"]
            p["_t_mono"] = now_ms() - (time.time() - rec["t"]) * 1000.0
            out.append(p)
            if t == "state":
                self.states.append(p)
            elif t == "hello":
                if self.hello is None:
                    self.hello = p
            elif t == "error":
                self.errors.append(p)
            elif t == "intent":
                self.intents.append(p)
            elif t == "ended":
                self.ended.append(p)
            elif t == "clock_reply":
                self.replies.append(p)
        return out

    def wait(self, msg_type: str, timeout: float = 10.0) -> dict | None:
        """Pump until a message of ``msg_type`` arrives; None after ``timeout`` seconds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for m in self.pump(0.05):
                if m.get("type") == msg_type:
                    return m
            self.keepalive()
        return None

    def keepalive(self) -> None:
        if self.link and not self.link.closed:
            self.link.keepalive(5.0)

    def wait_hello(self, timeout: float = 25.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump(0.1)
            if self.hello:
                return self.hello
        return None

    def drain_states(self) -> list[dict]:
        self.pump(0.0)
        out, self.states = self.states, []
        return out

    def latest_state(self) -> dict | None:
        return self.states[-1] if self.states else None

    def collect(self, seconds: float, hz: float = 20.0) -> list[dict]:
        """Read the socket often; the receiver pushes state at 4 Hz on its own."""
        out = []
        t_end = time.time() + seconds
        while time.time() < t_end:
            out.extend(self.drain_states())
            self.keepalive()
            time.sleep(1.0 / hz)
        out.extend(self.drain_states())
        return out

    def close(self) -> None:
        try:
            if self.link:
                self.link.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------------------------
def eureka(host: str = HOST, timeout: float = 4.0) -> dict:
    """Read-only device info on 8008. Launches nothing, plays nothing."""
    out = {"url": "http://%s:8008/setup/eureka_info" % host}
    try:
        with urllib.request.urlopen(out["url"], timeout=timeout) as r:
            d = json.load(r)
        info = d.get("detail", {}) if isinstance(d.get("detail"), dict) else {}
        out.update(
            {
                "ok": True,
                "name": d.get("name"),
                "model": info.get("model_name") or d.get("model_name"),
                "device_info": info.get("device_info"),
                "cast_build_revision": d.get("cast_build_revision"),
                "build_version": d.get("build_version"),
                "uptime_s": round(d.get("uptime", 0.0), 1),
                "ip": d.get("ip_address"),
                "connected": d.get("connected"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        out.update({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    return out


def tcp_open(host: str, port: int, timeout: float = 3.0) -> dict:
    t0 = now_ms()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"port": port, "open": True, "connect_ms": round(now_ms() - t0, 1)}
    except OSError as exc:
        return {"port": port, "open": False, "error": str(exc)}


# ---------------------------------------------------------------------------------------------
def make_assets(verbose: bool = True) -> dict:
    """Generate the test assets on CT 121 with its own ffmpeg (no local ffmpeg needed)."""
    dest = "/opt/nac-dev/media"
    cmds = {
        "codec-h264.mp4": (
            "ffmpeg -y -f lavfi -i testsrc2=size=640x360:rate=30 -t 6 "
            "-c:v libx264 -profile:v baseline -level 3.0 -pix_fmt yuv420p "
            "-movflags +faststart"
        ),
        "codec-hevc.mp4": (
            "ffmpeg -y -f lavfi -i testsrc2=size=640x360:rate=30 -t 6 "
            "-c:v libx265 -tag:v hvc1 -pix_fmt yuv420p -x265-params log-level=0 "
            "-movflags +faststart"
        ),
        "codec-vp9.mp4": (
            "ffmpeg -y -f lavfi -i testsrc2=size=640x360:rate=30 -t 6 "
            "-c:v libvpx-vp9 -b:v 400k -pix_fmt yuv420p -movflags +faststart"
        ),
        "codec-vp9.webm": (
            "ffmpeg -y -f lavfi -i testsrc2=size=640x360:rate=30 -t 6 "
            "-c:v libvpx-vp9 -b:v 400k -pix_fmt yuv420p"
        ),
        "codec-av1.mp4": (
            "ffmpeg -y -f lavfi -i testsrc2=size=640x360:rate=30 -t 6 "
            "-c:v libsvtav1 -preset 10 -crf 40 -pix_fmt yuv420p "
            "-movflags +faststart"
        ),
        "rate-90s.mp4": (
            "ffmpeg -y -f lavfi -i testsrc2=size=640x360:rate=30 -t 90 "
            "-c:v libx264 -preset ultrafast -b:v 250k -g 30 -pix_fmt yuv420p "
            "-movflags +faststart"
        ),
    }
    out = {"host": SERVE_HOST, "dir": dest, "assets": {}}
    script = ["set -e", "mkdir -p %s" % dest]
    for name, cmd in cmds.items():
        path = "%s/%s" % (dest, name)
        script.append(
            "if [ ! -s %s ]; then %s %s >/dev/null 2>&1 && echo 'made %s'; "
            "else echo 'have %s'; fi" % (path, cmd, path, name, name)
        )
    script.append("ls -l %s" % dest)
    if verbose:
        print("== generating assets on %s:%s" % (SERVE_HOST, dest))
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "root@%s" % SERVE_HOST, "\n".join(script)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        out["stdout"] = res.stdout.strip().splitlines()
        out["stderr"] = res.stderr.strip()[-2000:]
        out["rc"] = res.returncode
        if verbose:
            for line in out["stdout"][:12]:
                print("   " + line)
    except Exception as exc:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def asset_ready(name: str, timeout: float = 4.0) -> dict:
    url = "%s/media/%s" % (HTTP_BASE, name)
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            size = int(r.headers.get("Content-Length") or 0)
            return {"name": name, "url": url, "ok": r.status == 200 and size > 0, "bytes": size}
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "url": url, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


# ---------------------------------------------------------------------------------------------
def mdns(host: str = HOST, timeout: float = 4.0) -> dict:
    """Model and capabilities from mDNS (read-only, no playback).

    This firmware answers /setup/eureka_info with model_name: null, so the model comes from the
    device's own mDNS TXT record (`md` = model, `ca` = capabilities, `id` = device UUID).
    """
    out = {"host": host, "source": "_googlecast._tcp.local."}
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

        found = {}

        class L(ServiceListener):
            def add_service(self, zc, type_, name):
                info = zc.get_service_info(type_, name, timeout=2000)
                if not info:
                    return
                props = {
                    (k.decode() if isinstance(k, bytes) else k): (
                        v.decode() if isinstance(v, bytes) else v
                    )
                    for k, v in (info.properties or {}).items()
                }
                addrs = [a for a in info.parsed_scoped_addresses() if ":" not in a]
                if addrs and addrs[0] == host:
                    found.update({k: props.get(k) for k in ("md", "fn", "ca", "id", "ve", "rs", "st")})
                    found["port"] = info.port

            def update_service(self, zc, type_, name):
                self.add_service(zc, type_, name)

            def remove_service(self, zc, type_, name):
                pass

        zc = Zeroconf()
        try:
            ServiceBrowser(zc, "_googlecast._tcp.local.", L())
            time.sleep(timeout)
        finally:
            zc.close()
        out.update(
            {
                "ok": bool(found),
                "model": found.get("md"),
                "friendly_name": found.get("fn"),
                "capabilities": found.get("ca"),
                "device_id": found.get("id"),
                "port": found.get("port"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        out.update({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
    return out


def preflight(host: str, out_dir: str | None) -> dict:
    """Read-only: what the device is, and whether it is idle. Launches nothing."""
    info = {
        "task": "C — receiver preflight",
        "command": COMMAND,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": host,
        "eureka": eureka(host),
        "mdns": mdns(host),
        "tcp": [tcp_open(host, 8008), tcp_open(host, 8009)],
        "http_serve": tcp_open(SERVE_HOST, 7640),
    }
    try:
        link = CastLink(host)
        link.connect_platform()
        st = link.receiver_status()
        app_id, app_name, transport = receiver_app(st)
        info["receiver_status"] = {
            "app_id": app_id,
            "app_name": app_name,
            "transport_id": transport,
            "volume": st.get("volume"),
            "idle_screen": bool((st.get("applications") or [{}])[0].get("isIdleScreen")),
            "applications": [
                {
                    "appId": a.get("appId"),
                    "displayName": a.get("displayName"),
                    "isIdleScreen": a.get("isIdleScreen"),
                }
                for a in (st.get("applications") or [])
            ],
        }
        apps = info["receiver_status"]["applications"]
        busy = bool(apps) and app_id not in BACKDROP and not info["receiver_status"]["idle_screen"]
        info["verdict"] = "busy" if busy else "idle"
        info["safe_to_launch"] = not busy
        # is our unpublished app even listed on this device? (read-only, launches nothing)
        try:
            av = link.request(
                "receiver-0",
                "urn:x-cast:com.google.cast.receiver",
                {"type": "GET_APP_AVAILABILITY", "appId": [APP_ID, "CC1AD845", "E8C28D3C"]},
                timeout=8,
            )
            info["app_availability"] = (av.get("payload") or {}).get("availability")
        except Exception as exc:  # noqa: BLE001
            info["app_availability_error"] = "%s: %s" % (type(exc).__name__, exc)
        link.close()
    except Exception as exc:  # noqa: BLE001
        info["receiver_status_error"] = "%s: %s" % (type(exc).__name__, exc)
        info["verdict"] = "unreachable"
        info["safe_to_launch"] = False
    if out_dir:
        _write(os.path.join(out_dir, "41-preflight.json"), info)
    return info


# ---------------------------------------------------------------------------------------------
def measure_codecs(dev: Recv, out_dir: str) -> dict:
    """(a) What actually plays, not what canPlayType claims."""
    res = {
        "task": "C (a) — codec matrix",
        "command": COMMAND,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "video-only assets; 'plays' = reached playing AND the playhead advanced",
        "claims_from_hello": (dev.hello or {}).get("codecs"),
        "results": [],
    }
    for name, ctype, what in ASSETS:
        if not name.startswith("codec-"):
            continue
        ready = asset_ready(name)
        row = {"asset": name, "content_type": ctype, "what": what, "asset_head": ready}
        if not ready.get("ok"):
            row["verdict"] = "asset_unreachable"
            res["results"].append(row)
            continue
        dev.drain_states()
        err0 = len(dev.errors)
        dev.send("load", url=ready["url"], content_type=ctype, media=0.0, autoplay=True)
        samples = dev.collect(9.0)
        states = [s for s in samples if s.get("player_state") != "idle"]
        played = [s for s in states if s.get("player_state") == "playing"]
        first = states[0]["player_state"] if states else "none"
        advance = 0.0
        if played:
            advance = max(s["media"] for s in played) - min(s["media"] for s in played)
        new_err = dev.errors[err0:]
        row.update(
            {
                "first_state": first,
                "states_seen": sorted({s.get("player_state") for s in states}),
                "samples": len(states),
                "playing_samples": len(played),
                "playhead_advance_s": round(advance, 3),
                "errors": [
                    {"code": e.get("code"), "message": redact(e.get("message", ""))[:160]}
                    for e in new_err
                ],
            }
        )
        row["verdict"] = "plays" if played and advance >= 1.0 else "no" if not played else "stalls"
        res["results"].append(row)
        print(
            "   %-16s %-8s advance=%5.2fs states=%s"
            % (name, row["verdict"], advance, ",".join(row["states_seen"]))
        )
        dev.send("stop")
        dev.collect(0.6)
    _write(os.path.join(out_dir, "41a-codec-matrix.json"), res)
    return res


def _band(states: list[dict], t0: float, t1: float) -> list[dict]:
    return [s for s in states if t0 <= s.get("rx_ms", 0) <= t1]


def measure_rate(dev: Recv, out_dir: str, seconds: float = 60.0) -> tuple[dict, dict]:
    """(b) the decisive number, and (c) what the change costs."""
    rate_res = {
        "task": "C (b) — rate trim",
        "command": COMMAND,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "method": "least squares of media vs the receiver's own rx_ms, 60 s per rate",
        "runs": [],
    }
    stall_res = {
        "task": "C (c) — cost of a rate change",
        "command": COMMAND,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "method": "state samples in a 3 s band around each rate command",
        "events": [],
    }
    asset = asset_ready("rate-90s.mp4")
    if not asset.get("ok"):
        rate_res["error"] = "rate asset unreachable"
        stall_res["error"] = "rate asset unreachable"
        _write(os.path.join(out_dir, "41b-rate-trim.json"), rate_res)
        _write(os.path.join(out_dir, "41c-rate-stall.json"), stall_res)
        return rate_res, stall_res

    dev.drain_states()
    dev.send("load", url=asset["url"], content_type="video/mp4", media=0.0, autoplay=True)
    dev.collect(4.0)
    st = dev.latest_state()
    rate_res["player_state_on_load"] = st and st.get("player_state")

    for asked in (1.0, 0.998, 1.002):
        # rewind so every window has a full 60 s of media ahead of it
        dev.send("seek", media=0.0)
        dev.collect(1.5)
        t_cmd = dev.est_rx()  # receiver clock at the moment the command leaves
        seq = dev.send("rate", value=asked)
        pre = dev.collect(2.0)
        pre_last = pre[-1] if pre else None
        samples = dev.collect(seconds)
        all_s = pre + samples
        pts = [
            (s["rx_ms"] / 1000.0, s["media"])
            for s in samples
            if s.get("player_state") == "playing" and isinstance(s.get("media"), (int, float))
        ]
        f = fit(pts)
        run = {
            "asked_rate": asked,
            "seq": seq,
            "state_samples": len(samples),
            "playing_samples": len(pts),
        }
        if f:
            gain = f["ratio"] - 1.0
            run.update(
                {
                    "measured_ratio": round(f["ratio"], 6),
                    "ratio_sigma": round(f["ratio_sigma"], 6),
                    "measured_gain": round(gain, 6),
                    "gain_sigma": round(f["ratio_sigma"], 6),
                    "asked_gain": round(asked - 1.0, 6),
                    "gain_error": round(gain - (asked - 1.0), 6),
                    "gain_error_sigmas": round((gain - (asked - 1.0)) / f["ratio_sigma"], 2)
                    if f["ratio_sigma"]
                    else None,
                    "distinguishable_from_zero_at_3sigma": bool(abs(gain) > 3 * f["ratio_sigma"]),
                    "drift_ms_per_10min": round(gain * 600 * 1000, 1),
                    "residual_max_ms": round(f["residual_max_ms"], 2),
                    "span_s": round(f["span_s"], 2),
                    "echoed_rate": pre_last and pre_last.get("rate"),
                }
            )
            print(
                "   asked %.3f -> ratio %.6f +- %.6f (gain %+.5f, %s at 3 sigma)"
                % (
                    asked,
                    f["ratio"],
                    f["ratio_sigma"],
                    gain,
                    "distinguishable"
                    if run["distinguishable_from_zero_at_3sigma"]
                    else "NOT distinguishable",
                )
            )
        else:
            run["error"] = "not enough playing samples"
        rate_res["runs"].append(run)

        # (c) the stall band: did the player leave 'playing', did the playhead step back, did
        # buffered_end collapse, did dropped frames jump, did the 4 Hz stream go quiet?
        band = _band(all_s, (t_cmd or 0) - 250, (t_cmd or 0) + 3000)
        gaps = [band[i + 1]["rx_ms"] - band[i]["rx_ms"] for i in range(len(band) - 1)]
        states_band = [s.get("player_state") for s in band]
        media_band = [s.get("media") for s in band if isinstance(s.get("media"), (int, float))]
        backwards = [media_band[i + 1] - media_band[i] for i in range(len(media_band) - 1)]
        buf = [s.get("buffered_end") for s in band if s.get("buffered_end") is not None]
        drop = [s.get("dropped") for s in band if s.get("dropped") is not None]
        event = {
            "asked_rate": asked,
            "command_seq": seq,
            "states_in_band": len(band),
            "player_states": sorted(set(states_band)),
            "left_playing": any(x != "playing" for x in states_band if x),
            "max_state_gap_ms": round(max(gaps), 1) if gaps else None,
            "playhead_backstep_s": round(min(backwards), 3) if backwards else None,
            "buffered_end_start_s": round(buf[0], 2) if buf else None,
            "buffered_end_end_s": round(buf[-1], 2) if buf else None,
            "buffered_end_collapsed": bool(buf and buf[-1] < buf[0] - 0.05),
            "dropped_start": drop[0] if drop else None,
            "dropped_end": drop[-1] if drop else None,
            "dropped_added": (drop[-1] - drop[0]) if len(drop) >= 2 else None,
        }
        stall_res["events"].append(event)
        print(
            "   stall: left_playing=%s gap=%sms dropped+%s buffered %.2f->%.2f"
            % (
                event["left_playing"],
                event["max_state_gap_ms"],
                event["dropped_added"],
                event["buffered_end_start_s"] or 0,
                event["buffered_end_end_s"] or 0,
            )
        )

    dev.send("rate", value=1.0)
    dev.collect(0.5)
    dev.send("stop")
    dev.collect(0.8)
    _write(os.path.join(out_dir, "41b-rate-trim.json"), rate_res)
    _write(os.path.join(out_dir, "41c-rate-stall.json"), stall_res)
    return rate_res, stall_res


def measure_clock(dev: Recv, out_dir: str, n: int = 200, hz: float = 5.0) -> dict:
    """(d) 200 pings at 5 Hz: RTT, offset, and the estimator's residual."""
    res = {
        "task": "C (d) — clock",
        "command": COMMAND,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pings": n,
        "rate_hz": hz,
        "samples": [],
    }
    period = 1.0 / hz
    for _ in range(n):
        t0 = now_ms()
        seq = dev.send("clock")
        rx = None
        deadline = time.time() + 1.0
        while time.time() < deadline:
            for m in dev.pump(0.02):
                if m.get("type") == "clock_reply" and m.get("seq") == seq:
                    rx = m.get("rx_ms")
                    break
            if rx is not None:
                break
        t1 = now_ms()
        dev.keepalive()
        if rx is None:
            res["samples"].append({"t0": t0, "rtt_ms": None})
        else:
            res["samples"].append(
                {
                    "t0": round(t0, 2),
                    "rtt_ms": round(t1 - t0, 2),
                    "offset_ms": round(rx - (t0 + t1) / 2.0, 2),
                    "rx_ms": round(rx, 2),
                }
            )
        time.sleep(max(0.0, period - (now_ms() - t0) / 1000.0))
    good = [s for s in res["samples"] if s.get("rtt_ms") is not None]
    res["answered"] = len(good)
    res["rtt_ms"] = robust(good, "rtt_ms")
    if good:
        best = sorted(good, key=lambda s: s["rtt_ms"])[: max(1, len(good) // 5)]
        res["offset_ms"] = robust(good, "offset_ms")
        res["offset_best_rtt_ms"] = robust(best, "offset_ms")
        res["estimator"] = {
            "method": "offset_i = rx_ms - (t0+t1)/2; min-RTT fifth of the samples",
            "offset_ms": round(statistics.median([s["offset_ms"] for s in best]), 2),
            "residual_ms": round(statistics.pstdev([s["offset_ms"] for s in best]), 2)
            if len(best) > 1
            else None,
            "rtt_of_best_ms": round(statistics.median([s["rtt_ms"] for s in best]), 2),
        }
        res["rx_ms_first_last"] = [good[0]["rx_ms"], good[-1]["rx_ms"]]
        res["timeorigin_looks_like_epoch"] = bool(good[-1]["rx_ms"] > 1.7e12)
    _write(os.path.join(out_dir, "41d-clock.json"), res)
    return res


def measure_start(
    dev: Recv, out_dir: str, trials: int = 10, pos: float = 3.0, lead_ms: float = 1200.0
) -> dict:
    """(e) 10 x play_at: the error of the first 'playing' sample against the target."""
    res = {
        "task": "C (e) — start dispersion",
        "command": COMMAND,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trials": trials,
        "target_media_s": pos,
        "lead_ms": lead_ms,
        "method": (
            "err = media(first playing sample) - [pos + (rx_ms - target)/1000 * rate]; "
            "negative = the picture lands behind the target"
        ),
        "results": [],
    }
    asset = asset_ready("rate-90s.mp4")
    if not asset.get("ok"):
        res["error"] = "asset unreachable"
        _write(os.path.join(out_dir, "41e-start-dispersion.json"), res)
        return res
    dev.drain_states()
    dev.send("load", url=asset["url"], content_type="video/mp4", media=0.0, autoplay=False)
    dev.collect(3.0)
    for i in range(trials):
        dev.send("pause")
        dev.send("seek", media=pos)
        parked = dev.collect(1.2)
        last = parked[-1] if parked else None
        if not last:
            res["results"].append({"trial": i + 1, "error": "no state while parking"})
            continue
        # the receiver's own clock, extrapolated from its last sample (a ping right before the
        # command pins the offset; the extrapolation only has to survive a few ms)
        target = last["rx_ms"] + (now_ms() - last.get("_t_mono", now_ms())) + lead_ms
        seq = dev.send("play_at", rx_ms=target, media=pos)
        samples = dev.collect(3.0 + lead_ms / 1000.0)
        playing = [
            s
            for s in samples
            if s.get("player_state") == "playing" and s.get("rx_ms", 0) >= target - 1500
        ]
        row = {
            "trial": i + 1,
            "seq": seq,
            "target_rx_ms": round(target, 1),
            "target_media_s": pos,
            "parked_media_s": round(last.get("media", -1), 3),
            "parked_state": last.get("player_state"),
        }
        if not playing:
            row["error"] = "never reached playing"
        else:
            first = playing[0]
            expected = pos + max(0.0, (first["rx_ms"] - target) / 1000.0) * (first.get("rate") or 1.0)
            row.update(
                {
                    "first_playing_rx_ms": round(first["rx_ms"], 1),
                    "first_playing_media_s": round(first["media"], 4),
                    "since_target_ms": round(first["rx_ms"] - target, 1),
                    "expected_media_s": round(expected, 4),
                    "error_ms": round((first["media"] - expected) * 1000.0, 1),
                    "rate": first.get("rate"),
                }
            )
            print(
                "   trial %2d  err %+7.1f ms  (first playing sample %+.0f ms after target)"
                % (i + 1, row["error_ms"], row["since_target_ms"])
            )
        res["results"].append(row)
    errs = [r["error_ms"] for r in res["results"] if "error_ms" in r]
    if errs:
        res["summary"] = {
            "n": len(errs),
            "mean_ms": round(statistics.mean(errs), 1),
            "median_ms": round(statistics.median(errs), 1),
            "sigma_ms": round(statistics.pstdev(errs), 1) if len(errs) > 1 else None,
            "min_ms": round(min(errs), 1),
            "max_ms": round(max(errs), 1),
            "p95_abs_ms": round(
                sorted(abs(e) for e in errs)[min(len(errs) - 1, int(round(0.95 * (len(errs) - 1))))], 1
            ),
        }
    dev.send("stop")
    dev.collect(0.5)
    _write(os.path.join(out_dir, "41e-start-dispersion.json"), res)
    return res


# ---------------------------------------------------------------------------------------------
class Dmr:
    """The platform's own video receiver (Default Media Receiver, CC1AD845) on the same device.

    Why it exists here: our custom app is unpublished and a Cast device only lists an unpublished
    app once its serial is registered in the Cast console. When that is not the case the device
    answers APP_UNAVAILABLE and our page can never load. This class measures the *platform's*
    playbackRate lever on the same Chromecast, with the same least-squares method as evidence/17,
    so the integrator gets the decisive number even when the page cannot be reached. It is not a
    substitute for the page measurement and every file it writes says so.
    """

    def __init__(self, host: str, log=print):
        self.host, self.log = host, log
        self.link: CastLink | None = None
        self.transport = ""
        self.session_id = None

    def connect(self) -> None:
        self.link = CastLink(self.host)
        self.link.connect_platform()

    def launch(self) -> dict:
        reply = self.link.launch("CC1AD845", timeout=25)
        _, _, tr = receiver_app(self.link.receiver_status())
        self.transport = tr or ""
        self.link.connect_transport(self.transport)
        time.sleep(0.3)
        return {"launch_reply": reply, "transport_id": self.transport}

    def load(self, url: str, content_type: str, start: float = 0.0, autoplay: bool = True) -> dict:
        r = self.link.load(
            self.transport,
            {
                "appId": "CC1AD845",
                "sessionId": self.transport,
                "media": {"contentId": url, "contentType": content_type, "streamType": "BUFFERED"},
                "autoplay": autoplay,
                "currentTime": start,
            },
            timeout=25,
        )
        st = first_status(r)
        self.session_id = st.get("mediaSessionId")
        return {
            "reply_type": r.get("type"),
            "player_state": st.get("playerState"),
            "media_session_id": self.session_id,
        }

    def cmd(self, name: str, **fields) -> dict:
        r = self.link.media_command(self.transport, name, self.session_id, fields or None, timeout=8)
        st = first_status(r)
        return {
            "reply": r.get("type"),
            "player_state": st.get("playerState"),
            "current_time": st.get("currentTime"),
            "playback_rate": st.get("playbackRate"),
        }

    def sample(self) -> dict | None:
        t0 = time.time()
        try:
            r = self.link.media_status(self.transport, timeout=4)
        except CastError:
            return None
        t1 = time.time()
        st = first_status(r)
        return {
            "t": (t0 + t1) / 2.0,
            "t0": t0,
            "t1": t1,
            "media": st.get("currentTime"),
            "player_state": st.get("playerState"),
            "rate": st.get("playbackRate"),
            "session": st.get("mediaSessionId"),
            "idle_reason": st.get("idleReason"),
        }

    def status_list(self) -> list[dict]:
        """The raw status array: empty means the player dropped the session."""
        try:
            r = self.link.media_status(self.transport, timeout=4)
        except CastError:
            return []
        st = r.get("status")
        if isinstance(st, list):
            return st
        return [st] if isinstance(st, dict) and st else []

    def poll(self, seconds: float, hz: float = 10.0) -> list[dict]:
        out, t_end, period = [], time.time() + seconds, 1.0 / hz
        while time.time() < t_end:
            t = time.time()
            s = self.sample()
            if s:
                out.append(s)
            self.link.keepalive(5.0)
            time.sleep(max(0.0, period - (time.time() - t)))
        return out

    def stop(self) -> None:
        try:
            if self.session_id is not None:
                self.link.media_command(self.transport, "STOP", self.session_id, None, timeout=8)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            if self.link:
                self.link.close()
        except Exception:  # noqa: BLE001
            pass


def measure_codecs_dmr(dmr: Dmr, out_dir: str) -> dict:
    res = {
        "task": "C (a) — codec matrix, platform DMR",
        "command": COMMAND,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "backend": "Default Media Receiver CC1AD845 on the same Chromecast (NOT our page)",
        "caveat": (
            "our custom receiver page could not be launched (APP_UNAVAILABLE); this "
            "measures what the device's own video player renders, which is the same "
            "platform media pipeline but not our HTML5 page"
        ),
        "results": [],
    }
    for name, ctype, what in ASSETS:
        if not name.startswith("codec-"):
            continue
        ready = asset_ready(name)
        row = {"asset": name, "content_type": ctype, "what": what, "asset_head": ready}
        if not ready.get("ok"):
            row["verdict"] = "asset_unreachable"
            res["results"].append(row)
            continue
        t_load = time.time()
        lr = dmr.load(ready["url"], ctype, 0.0, autoplay=True)
        row["load"] = lr
        samples = dmr.poll(9.0)
        st = [s for s in samples if s.get("media") is not None]
        playing = [s for s in st if s.get("player_state") == "PLAYING"]
        advance = (
            (max(s["media"] for s in playing) - min(s["media"] for s in playing)) if playing else 0.0
        )
        # an unsupported stream is not "no": the player accepts the LOAD, reports LOADING, then
        # drops the session. That difference is the whole point of a codec matrix.
        present = [s for s in samples if s.get("session") is not None]
        dropped_at = None
        for s in samples:
            if s.get("session") is None:
                dropped_at = round(s["t"] - t_load, 2)
                break
        commands = dmr.status_list()
        row.update(
            {
                "states_seen": sorted({s.get("player_state") for s in st}),
                "samples": len(st),
                "playing_samples": len(playing),
                "playhead_advance_s": round(advance, 3),
                "first_state": st[0]["player_state"] if st else None,
                "last_state": st[-1]["player_state"] if st else None,
                "session_polls": len(present),
                "session_dropped_after_s": dropped_at,
                "supported_media_commands_after_load": (
                    commands[0].get("supportedMediaCommands") if commands else None
                ),
                "idle_reason": (st[-1].get("idle_reason") if st else None),
            }
        )
        row["verdict"] = (
            "plays"
            if playing and advance >= 1.0
            else "stalls"
            if playing
            else "rejected"
            if dropped_at is not None
            else "no"
        )
        res["results"].append(row)
        print(
            "   %-16s %-8s advance=%5.2fs states=%s"
            % (name, row["verdict"], advance, ",".join(row["states_seen"]))
        )
        dmr.stop()
        time.sleep(1.2)
    _write(os.path.join(out_dir, "41a-codec-matrix-dmr.json"), res)
    return res


def measure_rate_dmr(dmr: Dmr, out_dir: str, seconds: float = 60.0) -> tuple[dict, dict]:
    rate_res = {
        "task": "C (b) — rate trim, platform DMR",
        "command": COMMAND,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "backend": "Default Media Receiver CC1AD845 on the same Chromecast (NOT our page)",
        "caveat": (
            "the receiver page could not be launched; the platform's own video "
            "player stands in for its media pipeline, as evidence/17 did on a Nest "
            "Audio for the group"
        ),
        "method": "least squares of currentTime vs (t0+t1)/2 across GET_STATUS polls, 60 s",
        "runs": [],
    }
    stall_res = {
        "task": "C (c) — cost of a rate change, platform DMR",
        "command": COMMAND,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "backend": "Default Media Receiver CC1AD845 (NOT our page)",
        "events": [],
    }
    asset = asset_ready("rate-90s.mp4")
    if not asset.get("ok"):
        rate_res["error"] = "rate asset unreachable"
        stall_res["error"] = "rate asset unreachable"
        _write(os.path.join(out_dir, "41b-rate-trim-dmr.json"), rate_res)
        _write(os.path.join(out_dir, "41c-rate-stall-dmr.json"), stall_res)
        return rate_res, stall_res
    rate_res["asset"] = asset
    dmr.load(asset["url"], "video/mp4", 0.0, autoplay=True)
    dmr.poll(4.0)

    for asked in (1.0, 0.998, 1.002):
        dmr.cmd("SEEK", currentTime=0.0)
        dmr.poll(1.5)
        pre = dmr.poll(2.0, hz=20.0)
        t_cmd = time.time()
        change = dmr.cmd("SET_PLAYBACK_RATE", playbackRate=asked)
        band = pre + dmr.poll(3.0, hz=20.0)
        samples = dmr.poll(seconds, hz=10.0)
        pts = [(s["t"], s["media"]) for s in samples if s.get("player_state") == "PLAYING"]
        gained = fit(pts)
        run = {
            "asked_rate": asked,
            "reply": change,
            "samples": len(samples),
            "playing_samples": len(pts),
            "echoed_rate": change.get("playback_rate"),
        }
        if gained:
            gain = gained["ratio"] - 1.0
            run.update(
                {
                    "measured_ratio": round(gained["ratio"], 6),
                    "ratio_sigma": round(gained["ratio_sigma"], 6),
                    "measured_gain": round(gain, 6),
                    "asked_gain": round(asked - 1.0, 6),
                    "gain_error": round(gain - (asked - 1.0), 6),
                    "gain_error_sigmas": round((gain - (asked - 1.0)) / gained["ratio_sigma"], 2)
                    if gained["ratio_sigma"]
                    else None,
                    "distinguishable_from_zero_at_3sigma": bool(abs(gain) > 3 * gained["ratio_sigma"]),
                    "drift_ms_per_10min": round(gain * 600 * 1000, 1),
                    "residual_max_ms": round(gained["residual_max_ms"], 2),
                    "span_s": round(gained["span_s"], 2),
                }
            )
            print(
                "   asked %.3f -> ratio %.6f +- %.6f (gain %+.5f, %s at 3 sigma)"
                % (
                    asked,
                    gained["ratio"],
                    gained["ratio_sigma"],
                    gain,
                    "distinguishable"
                    if run["distinguishable_from_zero_at_3sigma"]
                    else "NOT distinguishable",
                )
            )
        else:
            run["error"] = "not enough PLAYING samples"
        rate_res["runs"].append(run)

        around = [s for s in band if s["t"] >= t_cmd - 0.3]
        media = [s["media"] for s in around if s.get("media") is not None]
        steps = [media[i + 1] - media[i] for i in range(len(media) - 1)]
        gaps = [around[i + 1]["t"] - around[i]["t"] for i in range(len(around) - 1)]
        event = {
            "asked_rate": asked,
            "echoed_rate": change.get("playback_rate"),
            "player_states": sorted({s.get("player_state") for s in around}),
            "left_playing": any(s.get("player_state") != "PLAYING" for s in around),
            "playhead_backstep_s": round(min(steps), 3) if steps else None,
            "max_poll_gap_ms": round(max(gaps) * 1000, 1) if gaps else None,
            "median_poll_gap_ms": round(statistics.median(gaps) * 1000, 1) if gaps else None,
        }
        stall_res["events"].append(event)
        print(
            "   stall: left_playing=%s backstep=%ss gap=%sms"
            % (event["left_playing"], event["playhead_backstep_s"], event["max_poll_gap_ms"])
        )
    dmr.cmd("SET_PLAYBACK_RATE", playbackRate=1.0)
    dmr.stop()
    _write(os.path.join(out_dir, "41b-rate-trim-dmr.json"), rate_res)
    _write(os.path.join(out_dir, "41c-rate-stall-dmr.json"), stall_res)
    return rate_res, stall_res


# ---------------------------------------------------------------------------------------------
def _write(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("   -> %s" % os.path.relpath(path, ROOT))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe-only", action="store_true", help="preflight only; launches nothing")
    ap.add_argument("--make-assets", action="store_true", help="generate the test assets on CT 121")
    ap.add_argument("--only", default="abcde", help="which measurements to run (default abcde)")
    ap.add_argument(
        "--backend",
        default="auto",
        choices=("auto", "receiver", "dmr"),
        help="receiver = our page; dmr = the platform's own player (stand-in)",
    )
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--app-id", default=APP_ID)
    ap.add_argument("--seconds", type=float, default=60.0, help="seconds per rate window")
    ap.add_argument("--evidence-dir", default=EVID)
    args = ap.parse_args(argv)

    os.makedirs(args.evidence_dir, exist_ok=True)
    print("== preflight (read-only, launches nothing)")
    pf = preflight(args.host, args.evidence_dir)
    print(
        "   %s: %s  app=%s  model=%s  uptime=%ss  volume=%s"
        % (
            pf["host"],
            pf["verdict"],
            (pf.get("receiver_status") or {}).get("app_name"),
            (pf.get("mdns") or {}).get("model"),
            pf["eureka"].get("uptime_s"),
            (pf.get("receiver_status") or {}).get("volume"),
        )
    )
    print("   app availability: %s" % (pf.get("app_availability") or {}))
    if args.probe_only:
        return 0
    if not pf["safe_to_launch"]:
        print(
            "REFUSING to launch: the device is %s, not idle on Backdrop. "
            "Nothing was launched, nothing was played." % pf["verdict"]
        )
        return 3

    backend = args.backend
    if backend == "auto":
        avail = (pf.get("app_availability") or {}).get(args.app_id)
        backend = "receiver" if avail == "APP_AVAILABLE" else "dmr"
        print("   backend: %s (%s -> %s)" % (backend, args.app_id, avail))
    if backend == "receiver" and (pf.get("app_availability") or {}).get(args.app_id) != "APP_AVAILABLE":
        print(
            "REFUSING: the device does not list %s (%s). Our unpublished receiver only runs on "
            "a device whose serial is registered in the Cast console; run with --backend dmr to "
            "measure the platform's own player instead."
            % (args.app_id, (pf.get("app_availability") or {}).get(args.app_id))
        )
        return 6

    assets = make_assets()
    if not any(asset_ready(n).get("ok") for n, _, _ in ASSETS):
        print("REFUSING: no test asset is reachable on %s (is receiver_serve.sh running?)" % HTTP_BASE)
        return 4

    orig_volume = (pf.get("receiver_status") or {}).get("volume") or {}
    final = {
        "command": COMMAND,
        "backend": backend,
        "assets": assets,
        "volume_before": orig_volume,
        "measurements": args.only,
        "app_availability": pf.get("app_availability"),
    }
    rc = 0
    if backend == "dmr":
        dmr = Dmr(args.host)
        try:
            dmr.connect()
            print("== mute the device (level untouched; %s -> muted)" % orig_volume.get("muted"))
            dmr.link.set_receiver_volume(muted=True)
            print("== launch the platform player CC1AD845")
            final["launch"] = dmr.launch()
            if "a" in args.only:
                print("== (a) codec matrix (platform player)")
                measure_codecs_dmr(dmr, args.evidence_dir)
            if "b" in args.only or "c" in args.only:
                print(
                    "== (b) rate trim / (c) stall cost (platform player, %.0f s per rate)" % args.seconds
                )
                measure_rate_dmr(dmr, args.evidence_dir, seconds=args.seconds)
            if "d" in args.only or "e" in args.only:
                print("   (d) and (e) need OUR page's namespace: not measurable on this backend")
                final["not_measured"] = [
                    "d: clock pings (needs our namespace)",
                    "e: start dispersion (needs our play_at)",
                ]
        except BaseException as exc:  # noqa: BLE001
            print("ABORTED: %s: %s" % (type(exc).__name__, exc))
            final["aborted"] = "%s: %s" % (type(exc).__name__, exc)
            rc = rc or 1
        finally:
            print("== cleanup: stop the player, restore the volume")
            try:
                dmr.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                dmr.link.stop_app(dmr.transport)
            except Exception as exc:  # noqa: BLE001
                final["stop_error"] = "%s: %s" % (type(exc).__name__, exc)
            time.sleep(1.5)
            try:
                if orig_volume.get("muted") is not None:
                    dmr.link.set_receiver_volume(muted=bool(orig_volume.get("muted")))
                    final["volume_restored"] = {
                        "muted": bool(orig_volume.get("muted")),
                        "level": orig_volume.get("level"),
                    }
            except Exception as exc:  # noqa: BLE001
                final["restore_error"] = "%s: %s" % (type(exc).__name__, exc)
            dmr.close()
            after = preflight(args.host, None)
            final["after"] = {
                "verdict": after["verdict"],
                "app_name": (after.get("receiver_status") or {}).get("app_name"),
                "volume": (after.get("receiver_status") or {}).get("volume"),
            }
            final["left_on_backdrop"] = after["verdict"] == "idle"
            _write(os.path.join(args.evidence_dir, "41z-final-state.json"), final)
        print("== done (rc=%d)" % rc)
        return rc

    dev = Recv(args.host, args.app_id)
    try:
        dev.connect()
        print("== mute the device (level untouched; %s -> muted)" % orig_volume.get("muted"))
        dev.link.set_receiver_volume(muted=True)
        print("== launch %s" % args.app_id)
        launched = dev.launch()
        final["launch"] = launched
        if launched["launch_reply_app"] != args.app_id:
            raise CastError(
                "device launched %r instead of %r" % (launched["launch_reply_app"], args.app_id)
            )
        dev.attach()
        hello = dev.wait_hello(25.0)
        final["hello"] = hello
        if not hello:
            print("FAILED: no `hello` from the receiver page within 25 s")
            rc = 5
            raise SystemExit  # noqa: F821 - finally below does the cleanup
        print("   hello: version=%s ua=%s" % (hello.get("version"), (hello.get("ua") or "")[:60]))
        if "a" in args.only:
            print("== (a) codec matrix")
            measure_codecs(dev, args.evidence_dir)
        if "b" in args.only or "c" in args.only:
            print("== (b) rate trim / (c) stall cost  (%0.f s per rate)" % args.seconds)
            measure_rate(dev, args.evidence_dir, seconds=args.seconds)
        if "d" in args.only:
            print("== (d) clock: 200 pings at 5 Hz")
            c = measure_clock(dev, args.evidence_dir)
            print(
                "   RTT median %.1f ms p95 %.1f ms; offset %.1f ms, residual %.1f ms"
                % (
                    (c.get("rtt_ms") or {}).get("median", -1),
                    (c.get("rtt_ms") or {}).get("p95", -1),
                    (c.get("estimator") or {}).get("offset_ms", float("nan")),
                    (c.get("estimator") or {}).get("residual_ms") or float("nan"),
                )
            )
        if "e" in args.only:
            print("== (e) start dispersion: 10 play_at")
            e = measure_start(dev, args.evidence_dir)
            s = e.get("summary") or {}
            print(
                "   landing: mean %s ms, sigma %s ms, range %s..%s"
                % (s.get("mean_ms"), s.get("sigma_ms"), s.get("min_ms"), s.get("max_ms"))
            )
    except SystemExit:
        pass
    except BaseException as exc:  # noqa: BLE001
        print("ABORTED: %s: %s" % (type(exc).__name__, exc))
        final["aborted"] = "%s: %s" % (type(exc).__name__, exc)
        rc = rc or 1
    finally:
        # always: stop the app, leave the device on Backdrop, unmute exactly as we found it
        print("== cleanup: stop, STOP the session, restore the volume")
        try:
            if dev.link and not dev.link.closed and dev.transport:
                dev.send("stop")
                dev.collect(0.5)
                dev.send("idle")
                dev.collect(0.5)
                dev.drain_states()
                dev.link.stop_app(dev.transport)
        except Exception as exc:  # noqa: BLE001
            final["stop_error"] = "%s: %s" % (type(exc).__name__, exc)
        time.sleep(1.5)
        try:
            if dev.link and not dev.link.closed and orig_volume.get("muted") is not None:
                dev.link.set_receiver_volume(muted=bool(orig_volume.get("muted")))
                final["volume_restored"] = {
                    "muted": bool(orig_volume.get("muted")),
                    "level": orig_volume.get("level"),
                }
        except Exception as exc:  # noqa: BLE001
            final["restore_error"] = "%s: %s" % (type(exc).__name__, exc)
        dev.close()
        after = preflight(args.host, None)
        final["after"] = {
            "verdict": after["verdict"],
            "app_name": (after.get("receiver_status") or {}).get("app_name"),
            "volume": (after.get("receiver_status") or {}).get("volume"),
        }
        final["left_on_backdrop"] = after["verdict"] == "idle"
        _write(os.path.join(args.evidence_dir, "41z-final-state.json"), final)
    print("== done (rc=%d)" % rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
