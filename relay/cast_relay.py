#!/usr/bin/env python3
"""Cast relay: follow the living-room TV's video session and play the same audio,
time-aligned, on the kitchen speaker group.

Architecture (validated by evidence/04, 06, 08):

    TV 192.168.2.80  --CASTv2 MEDIA_STATUS (read-only)-->  relay  --yt-dlp--> audio file
                                                                |
                                                                +-- DMR on the kitchen group

The TV is the reference clock: we read its playhead, we know the video id, we fetch
that video's audio and we drive the group to `tv_playhead + delta`. Nothing is ever
sent to the TV (read-only), so its native YouTube session is untouched.

Two actuators, deliberately different in character:
  * SEEK          — coarse (±90 ms measured dispersion, ~0.5 s to take effect,
                    audible gap). Used only to recover large errors.
  * PLAYBACK_RATE — *not actually available on this hardware.* Measured over 60 s by least
                    squares: asked 1.005, got 0.999957 ±0.00004; asked 0.995, got 1.000008
                    (evidence/17). The device echoes the value in its status and ignores it.
                    Kept behind --use-rate for other hardware, off by default.

That is less alarming than it sounds: the group's own clock drifts only -12 ms per 10 min
(±1 ms, measured over 600 s), so a 10-minute session does not need a fine actuator at all.
The plan is therefore: align once at start, then correct only if the error grows past COARSE_S.

delta = (TV render latency) - (group render latency), measured acoustically. Until
that calibration exists, delta is 0 and the offset is "digital only" (playheads).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "lib"))

from castv2 import CastLink, CastError, receiver_app, NS_MEDIA  # noqa: E402
from asset_server import AssetServer  # noqa: E402
import groupguard  # noqa: E402

DMR = "CC1AD845"
YOUTUBE_APP = "233637DE"
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

COARSE_S = 0.35      # beyond this, reload. Every correction is an audible gap, so the
                     # threshold is generous: 150 ms is the real budget, not 35.
FINE_S = 0.030       # beyond this, trim with playback rate (if the device honours it)
RATE_TAU_S = 4.0     # seconds over which the rate trims an error
MAX_RATE_DELTA = 0.02
SEEK_LEAD_S = 0.50   # measured on the group: a SEEK takes effect ~0.5 s after the command
START_LEAD_S = 0.35  # LOAD -> group playhead running; learned bias settled at -0.20..-0.33 on
                     # top of 0.60 (evidence/21), refined per run by lead_adj
OUTLIER_MS = 60.0    # single bad MEDIA_STATUS reads happen (evidence/17: +162/-156 ms
                     # one-sample spikes): ignore an error that disagrees with its neighbours


PREP_LOAD_S = 3.0    # position ahead of the TV when parking with a LOAD (buffering ~1-2 s)
PREP_SEEK_S = 1.6    # ... and with a paused SEEK in the current file
PLAY_LEAD_S = 0.044  # PLAY lands 44 ms late on average (evidence/27): send it that much early
SETTLE_S = 2.0       # after a LOAD, ignore the group's playhead this long
LAND_TOL_S = 0.070   # landing error tolerated after a start. 90 ms let a 100 ms bias sit
                     # unchallenged (sim S4: 158 ms, over budget); 70 ms caps it near 110.
                     # stop/starts at the beginning of a video (user report, 2026-10-25).
                     # (a LOAD lands with ~+-75 ms scatter on the group, evidence/22)
MAX_FIXES = 2        # corrective reloads per start: each one is a gap the listener hears
MAINT_TOL_S = 0.110  # once retries are spent, still correct an error above this...
MAINT_EVERY_S = 60.0 # ...but rarely
BIAS_GAIN = 0.15     # landings scatter independently around a bias (evidence/26 run 3: the same
                     # lead landed +6/+37/-85 ms). Correcting a retry by the last error doubles
                     # the variance (observed: alternating +-100..170 ms), so retries reuse the
                     # learned bias unchanged and only the bias itself is learned, slowly.
TV_WINDOW_S = 10.0   # TV playhead estimator window (evidence/19)
TV_WARM_S = 2.5      # minimum history before the estimate is trusted (two sawtooth refreshes)
TV_JUMP_S = 0.40     # a read this far from the estimate is a seek, not noise


# --------------------------------------------------------------------------- TV
class TvClock:
    """Filtered TV playhead.

    The YouTube receiver reports a sawtooth playhead (evidence/19): between refreshes it
    advances at ~0.85x, then jumps ~150 ms every 1-1.5 s, so a single read is off by up to
    +-140 ms. The refresh points (upper envelope) are consistent: over a 10 s window, the
    second-largest (t - wall) is stable to sigma 4.6 ms. Playback rate is taken as 1 (the TV
    and the group both measured within 1e-4 of real time).
    """

    def __init__(self):
        self.samples: list[tuple[float, float]] = []   # (wall, t - wall)

    def reset(self):
        self.samples.clear()

    def offset(self):
        if not self.samples:
            return None
        offs = sorted(o for _, o in self.samples)
        return offs[-2] if len(offs) >= 4 else offs[-1]

    def add(self, wall, t) -> bool:
        """Add a read; returns False (and restarts) on a discontinuity such as a seek."""
        o = t - wall
        cur = self.offset()
        if cur is not None and abs(o - cur) > TV_JUMP_S:
            self.samples = [(wall, o)]
            return False
        self.samples.append((wall, o))
        self.samples = [(w, x) for w, x in self.samples if w > wall - TV_WINDOW_S]
        return True

    def ready(self):
        return len(self.samples) >= 4 and self.samples[-1][0] - self.samples[0][0] >= TV_WARM_S

    def at(self, wall):
        return wall + self.offset()


class RemoteTv:
    """Read-only view of the TV's current media session."""

    def __init__(self, host="192.168.2.80", port=8009):
        self.host, self.port = host, port
        self.link: CastLink | None = None
        self.transport = None
        self.app_id = None

    def _connect(self):
        self.link = CastLink(self.host, self.port)
        self.link.connect_platform()
        self.transport = None

    def snapshot(self) -> dict:
        try:
            if self.link is None or self.link.closed:
                self._connect()
            st = self.link.receiver_status(timeout=3)
            apps = st.get("applications") or []
            app = apps[0] if apps else {}
            app_id, app_name, tr = receiver_app(st)
            namespaces = {n.get("name") for n in (app.get("namespaces") or [])}
            if tr != self.transport:
                self.transport = tr
                self.app_id = app_id
                if tr:
                    self.link.connect_transport(tr)
                    time.sleep(0.10)
            vol = st.get("volume") or {}
            out = {"wall": time.time(), "app": app_name, "app_id": app_id,
                   "content": None, "t": None, "state": "NO_APP",
                   # volume must be available even with no media app: the idle screen has no
                   # media namespace but the receiver still reports volume (bug found live).
                   "volume": vol.get("level"), "muted": vol.get("muted")}
            if not tr:
                return out
            # Apps like Backdrop do not serve the media namespace at all; asking would
            # just burn a timeout every poll and stall the loop.
            if NS_MEDIA not in namespaces:
                out["state"] = "NO_MEDIA"
                return out
            t0 = time.time()
            ms = self.link.media_status(tr, timeout=2)
            t1 = time.time()
            out["wall"] = (t0 + t1) / 2      # the playhead belongs to the reply, not the poll
            out["rtt_ms"] = round((t1 - t0) * 1000, 1)
            s = (ms.get("status") or [{}])[0]
            media = s.get("media") or {}
            out.update({
                "content": media.get("contentId"),
                "content_type": media.get("contentType"),
                "title": (media.get("metadata") or {}).get("title"),
                "t": s.get("currentTime"),
                "state": s.get("playerState"),
                "session": s.get("mediaSessionId"),
                "volume": (st.get("volume") or {}).get("level"),
                "muted": (st.get("volume") or {}).get("muted"),
            })
            return out
        except (CastError, OSError) as exc:
            self.close()
            return {"wall": time.time(), "state": "ERROR", "error": str(exc)[:120]}

    def close(self):
        if self.link:
            try:
                self.link.close()
            except Exception:  # noqa: BLE001
                pass
        self.link = None


# ------------------------------------------------------------------------- sink
class GroupSink:
    """The kitchen group, driven over CASTv2 (Default Media Receiver)."""

    def __init__(self, host, port, log=print, name=None):
        self.host, self.port = host, port
        self.name = name
        self.log = log
        self.link: CastLink | None = None
        self.transport = None
        self.session = None
        self.state = "IDLE"
        self.content = None
        self.rate = 1.0

    def reconnect(self):
        """Drop a dead link and open a new one (re-resolving the group by name if known).

        evidence/21: the group closed our link when a video ended and the relay, which never
        reconnected, stayed deaf for 10 minutes. Every sink error now goes through here."""
        try:
            if self.link:
                self.link.close()
        except Exception:  # noqa: BLE001
            pass
        self.link = None
        self.transport = None
        self.session = None
        self.state = "NONE"
        if self.name:
            found = groupguard.discover_groups(timeout=4)
            if self.name in found:
                self.host, self.port = found[self.name]
        self.connect()

    def connect(self):
        members = groupguard.require_safe_group(self.host, self.port, "kitchen group")
        self.log(f"group guard OK: {[m['name'] for m in members]}")
        self.link = CastLink(self.host, self.port)
        self.link.connect_platform()

    def _ensure_app(self):
        st = self.link.receiver_status()
        app_id, app_name, tr = receiver_app(st)
        if not tr:
            self.link.launch(DMR, timeout=25)
            st = self.link.receiver_status()
            app_id, app_name, tr = receiver_app(st)
            if not tr:
                raise CastError("group did not launch DMR")
            time.sleep(0.2)
        if tr != self.transport:
            self.transport = tr
            self.link.connect_transport(tr)
            time.sleep(0.2)
        return tr

    def start(self, content_url, content_type, at_time, content_id=None):
        tr = self._ensure_app()
        payload = {
            "appId": DMR, "sessionId": tr,
            "media": {"contentId": content_url, "contentType": content_type,
                      "streamType": "BUFFERED",
                      "metadata": {"metadataType": 0, "title": content_id or ""}},
            "autoplay": True, "currentTime": max(0.0, at_time),
        }
        r = self.link.load(tr, payload, timeout=25)
        s = (r.get("status") or [{}])[0]
        self.session = s.get("mediaSessionId")
        self.state = s.get("playerState")
        self.content = content_url
        self.rate = 1.0
        return s

    def prepare(self, content_url, content_type, at_time, content_id=None) -> bool:
        """Park the group PAUSED and buffered at `at_time`; PLAY comes separately at a chosen
        instant. Measured repeatability of PAUSE->SEEK->PLAY: sigma 30 ms, vs 57 ms for SEEK
        while playing and 202 ms for LOAD (evidence/27)."""
        tr = self._ensure_app()
        at_time = max(0.0, at_time)
        if self.content == content_url and self.session and \
                self.state in ("PLAYING", "PAUSED", "BUFFERING"):
            if self.state != "PAUSED":
                self.pause()
            self.link.media_command(tr, "SEEK", self.session,
                                    {"currentTime": at_time, "resumeState": "PLAYBACK_PAUSE"},
                                    timeout=8)
        else:
            r = self.link.load(tr, {
                "appId": DMR, "sessionId": tr,
                "media": {"contentId": content_url, "contentType": content_type,
                          "streamType": "BUFFERED",
                          "metadata": {"metadataType": 0, "title": content_id or ""}},
                "autoplay": False, "currentTime": at_time}, timeout=25)
            st = (r.get("status") or [{}])[0]
            self.session = st.get("mediaSessionId")
            self.content = content_url
        deadline = time.time() + 5
        while time.time() < deadline:
            st = (self.link.media_status(tr, timeout=3).get("status") or [{}])[0]
            if st.get("playerState") == "PAUSED" and \
                    abs((st.get("currentTime") or 0) - at_time) < 0.05:
                self.state = "PAUSED"
                return True
            time.sleep(0.1)
        return False

    def play_now(self):
        self.link.media_command(self.transport, "PLAY", self.session, timeout=8)
        self.state = "PLAYING"

    def snapshot(self) -> dict:
        if not self.link or not self.transport:
            return {"wall": time.time(), "t": None, "state": "NONE"}
        t0 = time.time()
        try:
            ms = self.link.media_status(self.transport, timeout=3)
            t1 = time.time()
        except CastError as exc:
            return {"wall": time.time(), "t": None, "state": "ERROR", "error": str(exc)[:80]}
        s = (ms.get("status") or [{}])[0]
        self.state = s.get("playerState")
        self.session = s.get("mediaSessionId", self.session)
        self.rate = s.get("playbackRate", self.rate)
        return {"wall": (t0 + t1) / 2, "rtt_ms": (t1 - t0) * 1000,
                "t": s.get("currentTime"), "state": self.state, "rate": self.rate}

    def set_volume(self, level, muted=False):
        self.link.set_receiver_volume(level=level, muted=muted)

    def seek(self, t):
        self.link.media_command(self.transport, "SEEK", self.session,
                                {"currentTime": max(0.0, t)}, timeout=8)

    def set_rate(self, r):
        self.link.media_command(self.transport, "SET_PLAYBACK_RATE", self.session,
                                {"playbackRate": r}, timeout=8)

    def pause(self):
        self.link.media_command(self.transport, "PAUSE", self.session, timeout=8)

    def play(self):
        self.link.media_command(self.transport, "PLAY", self.session, timeout=8)

    def stop(self):
        try:
            if self.transport:
                self.link.stop_app(self.transport, timeout=10)
        except CastError:
            pass
        self.state = "IDLE"
        self.session = None

    def close(self):
        if self.link:
            self.link.close()


# ------------------------------------------------------------- audio acquisition
class AudioCache:
    """Download a YouTube video's audio once, serve it over HTTP with Range support."""

    def __init__(self, base_url, assets_dir=None, log=print):
        self.dir = assets_dir or os.path.join(ROOT, "assets", "yt")
        os.makedirs(self.dir, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self.log = log
        self.inflight: set[str] = set()

    def path_for(self, vid):
        for ext in (".m4a", ".webm", ".opus", ".mp3"):
            p = os.path.join(self.dir, vid + ext)
            if os.path.exists(p):
                return p
        return None

    def url_for(self, vid):
        p = self.path_for(vid)
        if not p:
            return None
        ext = os.path.splitext(p)[1]
        ctype = {".m4a": "audio/mp4", ".webm": "audio/webm", ".opus": "audio/ogg",
                 ".mp3": "audio/mpeg"}[ext]
        return f"{self.base_url}/yt/{os.path.basename(p)}", ctype

    def fetch(self, vid, timeout=90):
        """Blocking download; returns (url, content_type) or None."""
        if not YT_ID.match(vid or ""):
            return None
        if self.path_for(vid):
            return self.url_for(vid)
        t0 = time.time()
        cmd = [os.path.join(ROOT, ".venv", "bin", "yt-dlp"), "-q", "--no-warnings",
               "--no-playlist", "-f", "bestaudio[ext=m4a]/bestaudio",
               "-o", os.path.join(self.dir, "%(id)s.%(ext)s"),
               f"https://www.youtube.com/watch?v={vid}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.log(f"yt-dlp timeout for {vid}")
            return None
        ok = self.path_for(vid) is not None
        self.log(f"yt-dlp {vid}: rc={r.returncode} ok={ok} in {time.time()-t0:.1f}s"
                 + ("" if ok else f" err={r.stderr[-200:]}"))
        return self.url_for(vid) if ok else None

    def fetch_async(self, vid):
        if vid in self.inflight or self.path_for(vid):
            return
        self.inflight.add(vid)

        def work():
            try:
                self.fetch(vid)
            finally:
                self.inflight.discard(vid)

        threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------- controller
class Relay:
    def __init__(self, tv, sink, audio, delta=0.0, poll=0.25, log_path=None,
                 dry_run=False, verbose=True, measure_url=None, use_rate=False,
                 measure_dir=False, mirror_volume=False, volume_gain=1.0, volume_max=1.0):
        self.tv = tv
        self.sink = sink
        self.audio = audio
        self.delta = delta
        self.poll = poll
        self.dry_run = dry_run
        self.verbose = verbose
        self.log_path = log_path
        self.measure_url = measure_url   # measurement mode: our click train, not the video audio
        self.measure_dir = measure_dir   # per-video measurement tracks in assets/measure/
        self.use_rate = use_rate         # off: verified no-op on this group (evidence/17)
        self._err_hist: list[float] = []
        self._realigned: set[str] = set()   # one corrective reload per session
        self.cur = {"content": None, "url": None, "ctype": None, "since": None}
        self.degraded = False
        self.stats = {"seeks": 0, "rate_changes": 0, "samples": 0}
        self.clock = TvClock()
        self._started_at = 0.0
        self.lead_adj = self._load_lead_adj()   # learned start bias, seconds
        self._errors = 0
        self._last_fix = 0.0
        self.mirror_volume = mirror_volume
        self.volume_gain = volume_gain
        self.volume_max = volume_max
        self._tv_vol = None
        self._vol_sent_at = 0.0
        self._fresh_launch = False
        self._g_prev = None
        self._moving = 0
        self._fixes = 0
        self.need_start = False
        self._fh = open(log_path, "a") if log_path else None

    LEAD_STATE = os.path.join(ROOT, "state", "lead_adj.json")

    def _load_lead_adj(self):
        """Reuse the start bias learned by a previous run: without it, the first video after
        every start of the service pays 1-2 extra reloads (= audible gaps)."""
        try:
            with open(self.LEAD_STATE) as fh:
                v = float(json.load(fh)["lead_adj"])
            return max(-0.8, min(0.8, v))
        except Exception:  # noqa: BLE001
            return 0.0

    def _save_lead_adj(self):
        try:
            os.makedirs(os.path.dirname(self.LEAD_STATE), exist_ok=True)
            with open(self.LEAD_STATE, "w") as fh:
                json.dump({"lead_adj": round(self.lead_adj, 4),
                           "t": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh)
        except Exception:  # noqa: BLE001
            pass

    def _log(self, rec):
        rec = {"wall": round(time.time(), 4), **rec}
        if self._fh:
            self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._fh.flush()
        if self.verbose and rec.get("ev") != "sample":
            print(json.dumps(rec, ensure_ascii=False)[:240])

    def sample(self, tv, g, err, action):
        self.stats["samples"] += 1
        self._log({"ev": "sample", "wall": round(time.time(), 4),
                   "tv_wall": round(tv.get("wall", 0), 4), "tv_rtt": tv.get("rtt_ms"),
                   "grp_wall": None if g.get("wall") is None else round(g["wall"], 4),
                   "grp_rtt": None if g.get("rtt_ms") is None else round(g["rtt_ms"], 1),
                   "tv_t": None if tv.get("t") is None else round(tv["t"], 3),
                   "tv_state": tv.get("state"), "grp_t": None if g.get("t") is None else round(g["t"], 3),
                   "grp_state": g.get("state"), "err_ms": None if err is None else round(err * 1000, 1),
                   "rate": g.get("rate"), "action": action, "content": (tv.get("content") or "")[:24]})

    # ---- content handling
    def _switch(self, tv):
        vid = (tv.get("content") or "").strip()
        mfile = os.path.join(ROOT, "assets", "measure", vid + ".m4a") if YT_ID.match(vid) else ""
        if self.measure_dir and os.path.exists(mfile):
            # Per-video measurement track: clicks in another band at the exact media times
            # of the video's own beeps (tools/measure_track.py).
            self.degraded = False
            self.cur = {"content": vid, "url": f"{self.audio.base_url}/measure/{vid}.m4a",
                        "ctype": "audio/mp4", "since": None}
            self._log({"ev": "measure_track", "video": vid, "url": self.cur["url"]})
            return
        if self.measure_url:
            # Measurement mode: same playhead, but a 2 kHz click train instead of the
            # video's own audio, so one room recording can separate TV from kitchen.
            self.degraded = False
            self.cur = {"content": vid or "measure", "url": self.measure_url,
                        "ctype": "audio/wav", "since": None}
            self._log({"ev": "measure_mode", "url": self.measure_url, "video": vid})
            return
        want_url, ctype = (None, None)
        if YT_ID.match(vid):
            got = self.audio.url_for(vid)
            if not got:
                self.audio.fetch_async(vid)
                self._log({"ev": "audio_pending", "video": vid})
                if not self.dry_run:
                    self.sink.stop()
                self.degraded = True
                self.cur = {"content": vid, "url": None, "ctype": None, "since": None}
                return
            want_url, ctype = got
        else:
            self._log({"ev": "content_unusable", "content": vid})
            self.degraded = True
            if not self.dry_run:
                self.sink.stop()
            self.cur = {"content": vid, "url": None, "ctype": None, "since": None}
            return
        self.degraded = False
        self.cur = {"content": vid, "url": want_url, "ctype": ctype, "since": None}
        self._log({"ev": "content_ready", "video": vid, "url": want_url})

    def _start_sink(self, tv, keep_fixes=False, extra=0.0):
        if not keep_fixes:
            self._fixes = 0
        if self.dry_run:
            self._log({"ev": "would_start", "url": self.cur["url"]})
            return
        if hasattr(self.sink, "prepare"):
            return self._start_paused(extra)
        # Fallback (simulated sinks): LOAD with autoplay at the position the TV will be at.
        at = max(0.0, self.clock.at(time.time()) + self.delta + START_LEAD_S + self.lead_adj + extra)
        self._fresh_launch = getattr(self.sink, "transport", None) is None or \
            getattr(self.sink, "state", None) in ("IDLE", "NONE")
        s = self.sink.start(self.cur["url"], self.cur["ctype"], at,
                            content_id=self.cur["content"])
        self._started_at = time.time()
        self._log({"ev": "sink_started", "at": round(at, 3), "lead_adj": round(self.lead_adj, 3),
                   "state": s.get("playerState"), "reported_t": s.get("currentTime")})

    def _start_paused(self, extra=0.0):
        """Park the group paused a little ahead of the TV, then PLAY at the instant the TV
        reaches that position (+ learned lead)."""
        reuse = self.sink.content == self.cur["url"] and self.sink.session and \
            self.sink.state in ("PLAYING", "PAUSED", "BUFFERING")
        prep = PREP_SEEK_S if reuse else PREP_LOAD_S
        pos = self.clock.at(time.time()) + self.delta + prep
        self._fresh_launch = False
        t0 = time.time()
        ok = self.sink.prepare(self.cur["url"], self.cur["ctype"], pos,
                               content_id=self.cur["content"])
        prep_s = time.time() - t0
        if not ok:
            self._log({"ev": "prepare_failed", "pos": round(pos, 3), "after_s": round(prep_s, 2)})
            self.need_start = True
            return
        lead = PLAY_LEAD_S + self.lead_adj + extra
        # wall instant at which the TV (plus delta) will be at pos - lead
        w_fire = (pos - lead) - self.delta - self.clock.offset()
        if w_fire - time.time() < 0.05:
            self._log({"ev": "prepare_too_slow", "prep_s": round(prep_s, 2)})
            self.need_start = True
            return
        while True:
            left = w_fire - time.time()
            if left <= 0:
                break
            if left > 0.3:
                self.sink.link.keepalive()
            time.sleep(min(0.1, left) if left > 0.012 else 0)
        self.sink.play_now()
        self._started_at = time.time()
        self._g_prev = None
        self._moving = 0
        self._log({"ev": "sink_started", "mode": "seek" if reuse else "load", "pos": round(pos, 3),
                   "lead": round(lead, 3), "lead_adj": round(self.lead_adj, 3),
                   "prep_s": round(prep_s, 2), "fire_late_ms": round((time.time() - w_fire) * 1000, 1)})

    def _sync_volume(self, tv):
        """Mirror the TV's volume onto the group, edge-triggered.

        Edge-triggered on purpose: only a *change on the TV* pushes a value to the group, so a
        volume the user sets on the group itself is not fought by the relay every 250 ms.
        """
        lvl = tv.get("volume")
        if lvl is None or self.dry_run or not hasattr(self.sink, "set_volume"):
            return
        if self._tv_vol is not None and abs(lvl - self._tv_vol) < 0.01:
            return
        if time.time() - self._vol_sent_at < 0.5:
            return
        target = max(0.0, min(self.volume_max, lvl * self.volume_gain))
        self._tv_vol = lvl
        self._vol_sent_at = time.time()
        try:
            self.sink.set_volume(round(target, 3), muted=bool(tv.get("muted")))
            self._log({"ev": "volume_mirrored", "tv": round(lvl, 3), "group": round(target, 3)})
        except (CastError, OSError) as exc:
            self._log({"ev": "volume_mirror_failed", "error": str(exc)[:80]})

    def step(self):
        if not self.dry_run and getattr(self.sink, "link", None) is not None:
            self.sink.link.keepalive()
        tv = self.tv.snapshot()
        if self.mirror_volume:
            self._sync_volume(tv)
        state = tv.get("state")
        if state == "ERROR":
            self._log({"ev": "tv_read_error", "error": tv.get("error")})
            return
        if state in ("NO_APP", "NO_MEDIA", "IDLE") and self.cur.get("content"):
            # Session over (stop, end of queue, app closed): the kitchen stops too, and the
            # next session starts from scratch.
            if not self.dry_run:
                self.sink.stop()
            self._log({"ev": "tv_session_ended", "state": state, "app": tv.get("app")})
            self.cur = {"content": None, "url": None, "ctype": None, "since": None}
            self._err_hist.clear()
            return
        if state != "PLAYING" or tv.get("t") is None:
            # Say so, at most every 5 s: otherwise a long TV rebuffer (a big seek makes
            # YouTube buffer for tens of seconds) looks like the relay being stuck — that is
            # exactly how the 35 s after a +7 min seek looked in the transport test.
            now = time.time()
            if now - getattr(self, "_wait_logged", 0) > 5:
                self._wait_logged = now
                self._log({"ev": "tv_waiting", "tv_state": state})
            self.clock.reset()
            self.need_start = True
            if self.cur.get("content") and not self.degraded:
                if not self.dry_run and self.sink.state == "PLAYING" and getattr(self.sink, "session", True):
                    self.sink.pause()
                    self.sink.state = "PAUSED"
                    self._log({"ev": "tv_not_playing", "state": state})
            return

        vid = (tv.get("content") or "").strip()
        if vid != self.cur["content"]:
            self._log({"ev": "content_changed", "from": self.cur["content"], "to": vid})
            self._switch(tv)
            self.clock.reset()
            self.need_start = True
            if not self.dry_run and self.sink.state == "PLAYING" and getattr(self.sink, "session", True):
                self.sink.pause()     # old video's audio must not run on
                self.sink.state = "PAUSED"
            return

        if not self.clock.add(tv["wall"], tv["t"]):
            self._log({"ev": "tv_jump", "tv_t": tv["t"]})   # user seek on the TV
            self.need_start = True
            if not self.dry_run and self.sink.state == "PLAYING" and getattr(self.sink, "session", True):
                self.sink.pause()
                self.sink.state = "PAUSED"
            return
        if not self.clock.ready():
            return

        if not self.cur.get("url"):
            # Degraded: audio still downloading, or unusable content (ad). Re-evaluate: the
            # download may have finished since (evidence/24: stuck on audio_pending forever).
            if YT_ID.match(vid) and self.audio.url_for(vid) and vid not in self.audio.inflight:
                self._switch(tv)
                self.need_start = True
            return

        if self.need_start:
            self.need_start = False
            self._err_hist.clear()
            self._start_sink(tv)
            return

        if self.dry_run:
            self.sample(tv, {"t": None, "state": "n/a"}, None, "dry_run")
            return

        g = self.sink.snapshot()
        if g.get("state") == "ERROR":
            raise CastError(f"group status: {g.get('error')}")
        if g.get("t") is None:
            if g.get("state") in ("NONE", "IDLE"):
                self._start_sink(tv)      # no session on the group (after a reconnect)
            return
        if g.get("state") != "PLAYING":
            if time.time() - self._started_at < 3.0:
                return            # LOAD -> PLAYING takes ~0.6 s; do not reload meanwhile
            if self.sink.state in ("PAUSED", "IDLE", "NONE"):
                self._start_sink(tv)
            return

        prev, self._g_prev = self._g_prev, g
        moving = prev is not None and prev.get("t") is not None and \
            abs((g["t"] - prev["t"]) - (g["wall"] - prev["wall"])) < 0.05 and \
            g["wall"] - prev["wall"] > 0.1
        if moving:
            self._moving += 1
        else:
            self._moving = 0
        if time.time() - self._started_at < SETTLE_S or self._moving < 2:
            # Also wait until the group playhead advances at real-time speed for two
            # intervals: on a fresh launch it can sit still >2 s, and measuring then made
            # the corrections alternate -700/+800 ms (evidence/26).
            # Right after LOAD the group reports the requested position before audio actually
            # runs (evidence/21 run 1: +0.6 s read as error -> reload loop for 30 s).
            self.sample(tv, g, None, "settling")
            return

        target = self.clock.at(g["wall"]) + self.delta
        raw_err = g["t"] - target          # >0: audio ahead of video

        # A single MEDIA_STATUS can be stale by >150 ms (evidence/17: one-sample spikes of
        # +162/-156 ms). Never act on one sample: keep a short history and use the median,
        # and drop a sample that disagrees with both neighbours.
        self._err_hist.append(raw_err)
        if len(self._err_hist) > 5:
            self._err_hist.pop(0)
        err = sorted(self._err_hist)[len(self._err_hist) // 2]
        if len(self._err_hist) >= 3:
            recent = self._err_hist[-3:]
            if abs(raw_err - err) > OUTLIER_MS / 1000.0 and \
                    abs(recent[0] - recent[1]) < OUTLIER_MS / 1000.0:
                self.sample(tv, g, raw_err, "outlier_ignored")
                return

        # Start-bias learning. Where a LOAD lands relative to the TV depends on the group's
        # start-up time (measured 0.38-0.6 s, evidence/07, 16, 20), so instead of trusting
        # START_LEAD_S we measure the landing error once settled, correct it with a fresh LOAD
        # (LOAD lands exactly; SEEK scatters +-90 ms), and keep the correction for next starts.
        if self._fixes >= MAX_FIXES and len(self._err_hist) >= 5 and abs(err) > MAINT_TOL_S \
                and time.time() - self._last_fix > MAINT_EVERY_S:
            self._fixes = MAX_FIXES - 1      # retries ran out off target (evidence/24: -109 ms)
        if len(self._err_hist) >= 5 \
                and abs(err) > LAND_TOL_S and self._fixes < MAX_FIXES:
            self._last_fix = time.time()
            self._fixes += 1
            gain = 0.0 if self._fresh_launch else BIAS_GAIN   # a fresh app launch lands ~0.7 s
            self.lead_adj -= gain * err                       # late: not representative
            self._save_lead_adj()
            self.stats["realigns"] = self.stats.get("realigns", 0) + 1
            self.sample(tv, g, err, f"realign lead_adj={self.lead_adj:+.3f}")
            self._err_hist.clear()
            self._start_sink(tv, keep_fixes=True)      # re-roll with the learned bias
            return

        if abs(err) > COARSE_S and time.time() - self._started_at < 10 \
                and self._fixes < MAX_FIXES:
            # Large landing error right after a start: that is start bias, learn it.
            self._fixes += 1
            gain = 0.0 if self._fresh_launch else BIAS_GAIN   # a fresh app launch lands ~0.7 s
            self.lead_adj -= gain * err                       # late: not representative
            self.stats["realigns"] = self.stats.get("realigns", 0) + 1
            self.sample(tv, g, err, f"realign lead_adj={self.lead_adj:+.3f}")
            self._err_hist.clear()
            self._start_sink(tv, keep_fixes=True)      # re-roll with the learned bias
            return

        if abs(err) > COARSE_S:
            # A TV seek or a lost alignment: reload at the exact position (LOAD lands exactly,
            # SEEK scatters by +-90 ms on this group).
            self._start_sink(tv)
            self.stats["seeks"] += 1
            if self.use_rate:
                self.sink.set_rate(1.0)
            self._err_hist.clear()
            self.sample(tv, g, err, "seek")
        elif self.use_rate and abs(err) > FINE_S:
            r = max(1 - MAX_RATE_DELTA, min(1 + MAX_RATE_DELTA, 1 - err / RATE_TAU_S))
            r = round(r, 5)
            if abs(r - (g.get("rate") or 1.0)) > 1e-4:
                self.sink.set_rate(r)
                self.stats["rate_changes"] += 1
                self.sample(tv, g, err, f"rate->{r}")
            else:
                self.sample(tv, g, err, "hold")
        else:
            self.sample(tv, g, err, "in_sync")

    def run(self, duration=None):
        t_end = time.time() + duration if duration else None
        try:
            while not t_end or time.time() < t_end:
                try:
                    self.step()
                except (CastError, OSError) as exc:
                    self._log({"ev": "step_error", "error": f"{type(exc).__name__}: {exc}"[:160]})
                    self._errors += 1
                    if self.sink is not None and not self.dry_run and self._errors >= 2:
                        # Two failures in a row: assume the group link is dead and rebuild it.
                        try:
                            self.sink.reconnect()
                            self.need_start = True
                            self._log({"ev": "sink_reconnected", "host": self.sink.host,
                                       "port": self.sink.port})
                        except Exception as exc2:  # noqa: BLE001
                            self._log({"ev": "sink_reconnect_failed", "error": str(exc2)[:160]})
                            time.sleep(2)
                else:
                    self._errors = 0
                time.sleep(self.poll)
        finally:
            if self._fh:
                self._fh.close()


def local_ip() -> str:
    """This host's LAN address (works in a container, no DNS needed)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.2.1", 9))
        return s.getsockname()[0]
    finally:
        s.close()


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tv", default="192.168.2.80")
    p.add_argument("--group", default=None,
                   help="name (resolved by mDNS) or host:port of a kitchen-only group "
                        "(checked by groupguard)")
    p.add_argument("--delta", type=float, default=0.0,
                   help="acoustic calibration: TV latency minus group latency, seconds")
    p.add_argument("--poll", type=float, default=0.25)
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="watch the TV only; never touch any speaker")
    p.add_argument("--log", default=None)
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--bind", default=None,
                   help="address the asset server binds and advertises (default: this host)")
    p.add_argument("--measure-tone", type=float, default=None, metavar="HZ",
                   help="measurement mode: play a HZ click train (e.g. 2000) instead of the "
                        "video audio, at the same playhead (see notes/measurement-protocol.md)")
    p.add_argument("--measure-file", default="assets/click_train_2k.wav")
    p.add_argument("--measure-tracks", action="store_true",
                   help="for videos with assets/measure/<id>.m4a, play that click track instead")
    p.add_argument("--mirror-volume", action="store_true",
                   help="push the TV's volume onto the group whenever it changes on the TV")
    p.add_argument("--volume-gain", type=float, default=1.0,
                   help="group level = TV level * gain (default 1.0)")
    p.add_argument("--volume-max", type=float, default=1.0,
                   help="cap for the mirrored group level")
    p.add_argument("--use-rate", action="store_true",
                   help="use SET_PLAYBACK_RATE for fine correction. Verified to be a NO-OP on "
                        "this hardware (evidence/17); only correct for other devices")
    return p


def main():
    a = build_parser().parse_args()
    log_path = a.log or os.path.join(ROOT, "evidence", "relay-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    tv = RemoteTv(a.tv)
    bind = a.bind or local_ip()
    base = f"http://{bind}:{a.port}"

    if a.dry_run or not a.group:
        relay = Relay(tv, sink=None, audio=AudioCache(base),
                      delta=a.delta, poll=a.poll, log_path=log_path, dry_run=True)
        print(f"dry-run: watching TV only, log={log_path}")
        relay.run(a.duration)
        return

    if ":" in a.group:
        host, _, port = a.group.partition(":")
    else:
        # A group's address is announced by mDNS and changes (leader election, reboots):
        # resolve it by name at start.
        groups = groupguard.discover_groups()
        if a.group not in groups:
            print(f"group {a.group!r} not found; seen: {sorted(groups)}")
            return
        host, port = groups[a.group]
        print(f"group {a.group!r} -> {host}:{port}")
    srv = AssetServer(os.path.join(ROOT, "assets"), host=bind, port=a.port,
                      log_path=os.path.join(ROOT, "logs", "asset-server.log")).start()
    sink = GroupSink(host, int(port), name=None if ":" in a.group else a.group)
    sink.connect()
    measure_url = None
    if a.measure_tone:
        mpath = os.path.join(ROOT, a.measure_file)
        if not os.path.exists(mpath):
            print(f"measurement track missing: {mpath}\n"
                  f"  generate it: .venv/bin/python tools/make_assets.py --click-train 720 "
                  f"{int(a.measure_tone)} {a.measure_file}")
            return
        measure_url = f"{base}/{os.path.relpath(mpath, os.path.join(ROOT, 'assets'))}"
        print(f"measurement mode: {measure_url}")
    relay = Relay(tv, sink, AudioCache(base),
                  delta=a.delta, poll=a.poll, log_path=log_path, measure_url=measure_url,
                  use_rate=a.use_rate, measure_dir=a.measure_tracks,
                  mirror_volume=a.mirror_volume, volume_gain=a.volume_gain,
                  volume_max=a.volume_max)
    try:
        relay.run(a.duration)
    finally:
        try:
            sink.stop()          # never leave the kitchen playing behind us
        except Exception:  # noqa: BLE001
            pass
        sink.close()
        srv.stop()
        print(json.dumps(relay.stats))


if __name__ == "__main__":
    main()
