"""Task E, offline: the kitchen group sink (contracts §1-§2; A6, A15, A23, A36, A37).

Nothing here opens a socket, resolves mDNS, touches a speaker or the TV. Two kinds of fake are
used, on purpose:

* ``tests/v020/fakes.FakeGroup`` — an in-memory *group* (the seam task F and the others share);
* :class:`FakeDevice` below — an in-memory *CASTv2 transport*, which is what ``CastGroup`` needs to
  exercise its estimator, its hysteresis and its session bookkeeping. It is scripted: a test hands
  it a sequence of ``MEDIA_STATUS`` payloads (a drift, a stale read, a contradiction) and the sink
  reacts exactly as it would against a real group.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LIB = os.path.join(ROOT, "lib")
for _p in (LIB, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from v020 import group as G  # noqa: E402
from v020.contracts import (  # noqa: E402
    PLAYER_IDLE, PLAYER_PAUSED, PLAYER_PLAYING, GroupSample,
)
from fakes import FakeGroup  # noqa: E402

PROBE = os.path.join(ROOT, "tools", "group_probe_v020.py")


# ------------------------------------------------------------------------------ scaffolding
class FakeClock:
    """The controller clock, under test control (a real run uses ``time.time``)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += float(dt)


class FakeDevice:
    """A scripted CASTv2 endpoint: the transport ``CastGroup`` expects, with no socket.

    ``on_media_status(device)`` synthesises a playhead (drift, noise, an outlier); otherwise the
    ``script`` list is consumed in order, and then ``status`` is reported unchanged. Every command
    is recorded in ``calls`` as ``(name, payload)`` so a test can assert what was *not* sent.
    """

    def __init__(self, clock: FakeClock, status: dict | None = None,
                 on_media_status=None) -> None:
        self.clock = clock
        self.status = dict(status or {"mediaSessionId": 4242,
                                      "playerState": PLAYER_PLAYING,
                                      "currentTime": 0.0})
        self.script: list[dict] = []
        self.on_media_status = on_media_status
        self.calls: list[tuple[str, object]] = []
        self.volume_calls: list[dict] = []
        self.stopped: list[str] = []
        self.launched: list[str] = []
        self.keepalives = 0
        self.closed = False
        self.error: Exception | None = None
        self.next_session = 4242
        self.receiver = {"volume": {"level": 0.35, "muted": False},
                         "applications": []}

    # -- transport ---------------------------------------------------------------------------
    def connect_platform(self) -> None:
        self.calls.append(("connect_platform", None))

    def connect_transport(self, transport_id: str) -> None:
        self.calls.append(("connect_transport", transport_id))

    def receiver_status(self, timeout: float = 6.0) -> dict:
        self.calls.append(("receiver_status", None))
        return json.loads(json.dumps(self.receiver))

    def launch(self, app_id: str, timeout: float = 20.0) -> dict:
        self.launched.append(app_id)
        self.receiver["applications"] = [{"appId": app_id, "displayName": "Default Media Receiver",
                                          "transportId": "tr-1"}]
        return {"applications": list(self.receiver["applications"])}

    def media_status(self, transport_id: str, timeout: float = 6.0) -> dict:
        self.calls.append(("media_status", transport_id))
        if self.error is not None:
            raise self.error
        if self.on_media_status is not None:
            return {"status": [self.on_media_status(self)]}
        if self.script:
            return {"status": [self.script.pop(0)]}
        return {"status": [dict(self.status)]}

    def load(self, transport_id: str, data: dict, timeout: float = 20.0) -> dict:
        self.calls.append(("load", json.loads(json.dumps(data))))
        self.status = {"mediaSessionId": self.next_session,
                       "playerState": PLAYER_PLAYING if data.get("autoplay") else PLAYER_PAUSED,
                       "currentTime": float(data.get("currentTime") or 0.0)}
        return {"status": [dict(self.status)]}

    def media_command(self, transport_id: str, cmd: str, media_session_id: int,
                      fields: dict | None = None, timeout: float = 8.0) -> dict:
        fields = dict(fields or {})
        self.calls.append((cmd, {"session": media_session_id, **fields}))
        if cmd == "PLAY":
            self.status["playerState"] = PLAYER_PLAYING
        elif cmd == "PAUSE":
            self.status["playerState"] = PLAYER_PAUSED
        elif cmd == "SEEK":
            self.status["currentTime"] = float(fields.get("currentTime") or 0.0)
            if fields.get("resumeState") == "PLAYBACK_PAUSE":
                self.status["playerState"] = PLAYER_PAUSED
        return {"status": [dict(self.status)]}

    def set_receiver_volume(self, level: float | None = None, muted: bool | None = None,
                            timeout: float = 6.0) -> dict:
        self.volume_calls.append({"level": level, "muted": muted})
        if level is not None:
            self.receiver["volume"]["level"] = level
        if muted is not None:
            self.receiver["volume"]["muted"] = muted
        return {"volume": dict(self.receiver["volume"])}

    def stop_app(self, session_id: str, timeout: float = 10.0) -> dict:
        self.stopped.append(session_id)
        self.receiver["applications"] = []
        self.status = {"mediaSessionId": None, "playerState": PLAYER_IDLE, "currentTime": 0.0}
        return {}

    def keepalive(self, every: float = 5.0) -> None:
        self.keepalives += 1

    def close(self) -> None:
        self.closed = True

    # -- assertions --------------------------------------------------------------------------
    def transport_calls(self) -> list[str]:
        """Commands that change what is heard (the "no reload after start" list)."""
        return [name for name, _ in self.calls if name in ("load", "PLAY", "PAUSE", "SEEK")]

    def levels_sent(self) -> list[float | None]:
        return [c["level"] for c in self.volume_calls]


class Harness(G.CastGroup):
    """A CastGroup whose three-network seams and clock are replaced; guard result is switchable."""

    def __init__(self, clock: FakeClock, device: FakeDevice, found: dict | None = None,
                 guard_ok: bool = True, name: str = "Cuisine",
                 host: str | None = None, port: int | None = None,
                 members: list[dict] | None = None) -> None:
        super().__init__(name=name, host=host, port=port, log=lambda *a, **k: None)
        self.clock = clock
        self.device = device
        self.found = dict(found) if found is not None else {name: ("192.168.2.61", 32221)}
        self.guard_ok = guard_ok
        self.members = members if members is not None else [
            {"name": "Cuisine Fenetre Vert", "id": "u1", "audio_only": True,
             "level": 0.25, "muted": False}]
        self.discoveries = 0
        self.guard_calls: list[tuple[str, int]] = []
        self.links: list[tuple[str, int]] = []
        self.now = clock                        # seam: the controller clock
        self.sleep = clock.advance              # seam: a fake clock, sleeping advances it

    def _discover(self, timeout: float = G.DISCOVER_TIMEOUT_S):
        self.discoveries += 1
        return dict(self.found)

    def _guard(self, host: str, port: int):
        self.guard_calls.append((host, port))
        if not self.guard_ok:
            raise G.UnsafeGroup(f"group {host}:{port} contains the TV")
        return [dict(m) for m in self.members]

    def _open_link(self, host: str, port: int):
        self.links.append((host, port))
        self.device.closed = False
        return self.device


def build(clock: FakeClock | None = None, device: FakeDevice | None = None,
          **kw) -> Harness:
    clock = clock or FakeClock()
    return Harness(clock, device or FakeDevice(clock), **kw)


def sample_at(wall: float, media: float, state: str = PLAYER_PLAYING) -> GroupSample:
    return GroupSample(wall=wall, media=media, player_state=state, session_id=4242)


def synthetic(clock: FakeClock, seconds: float, hz: float, slope: float = 1.0,
              noise_ms: float = 8.0, seed: int = 7, base: float = 5.0) -> list[GroupSample]:
    """A playhead that drifts at ``slope`` with Gaussian noise (seeded: the test is repeatable)."""
    rnd = random.Random(seed)
    out = []
    for i in range(int(seconds * hz)):
        wall = clock.t + i / hz
        media = base + (wall - clock.t) * slope + rnd.gauss(0.0, noise_ms / 1000.0)
        out.append(sample_at(wall, media))
    return out


# --------------------------------------------------------------------------- the estimator
class TestPlayheadEstimator(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.series = synthetic(self.clock, seconds=2.0, hz=4.0)

    def feed(self, est, samples):
        return [est.add(s) for s in samples]

    def test_five_sample_median_and_one_stale_read_rejected(self):
        est = G.PlayheadEstimator()
        verdicts = self.feed(est, self.series[:5])
        self.assertEqual(verdicts[0], G.VERDICT_RESUMED)
        self.assertTrue(all(v == G.VERDICT_OK for v in verdicts[1:]))
        before = est.playhead(self.series[4].wall)
        self.assertIsNotNone(before)

        stale = sample_at(self.series[4].wall + 0.25, self.series[4].media + 0.25 + 0.156)
        self.assertEqual(est.add(stale), G.VERDICT_OUTLIER)
        self.assertEqual(est.rejections, 1)
        self.assertEqual(est.jumps, 0)
        after = est.playhead(self.series[4].wall)
        self.assertIsNotNone(after)
        self.assertAlmostEqual(after, before, delta=0.002)   # one stale read moves nothing

        # the very next good read is accepted again: the window never latched on the outlier
        good = sample_at(stale.wall + 0.25, stale.media + 0.25 - 0.156)
        self.assertEqual(est.add(good), G.VERDICT_OK)

    def test_two_contradicting_reads_in_a_row_are_a_real_jump(self):
        est = G.PlayheadEstimator()
        self.feed(est, self.series[:5])
        wall = self.series[4].wall
        first = sample_at(wall + 0.25, self.series[4].media + 1.75)
        second = sample_at(wall + 0.50, self.series[4].media + 2.00)
        self.assertEqual(est.add(first), G.VERDICT_OUTLIER)
        self.assertEqual(est.add(second), G.VERDICT_JUMP)
        self.assertEqual(est.jumps, 1)
        self.assertEqual(est.rejections, 1)
        self.assertAlmostEqual(est.playhead(second.wall), second.media, delta=0.005)

    def test_residual_on_a_synthetic_drift(self):
        est = G.PlayheadEstimator()
        samples = synthetic(self.clock, seconds=60.0, hz=4.0, slope=1.0003, noise_ms=8.0)
        verdicts = self.feed(est, samples)
        self.assertEqual(est.rejections, 0, "8 ms of noise must never look like a stale read")
        self.assertEqual(verdicts[0], G.VERDICT_RESUMED)

        # the window's own residual (A15: sigma ~10 ms on the real group)
        self.assertIsNotNone(est.residual_ms())
        self.assertGreater(est.residual_ms(), 3.0)
        self.assertLess(est.residual_ms(), 20.0)

        # a single window cannot measure a rate: the fit is clamped to +-2 % of real time
        self.assertLessEqual(abs(est.slope() - 1.0), G.MAX_FIT_SLOPE_DEV)
        # the rate itself is recovered from the whole series, and the drift from 5 s medians
        series = G.least_squares([(s.wall, s.media) for s in samples])
        self.assertAlmostEqual(series["slope"], 1.0003, delta=5e-5)
        medians = G.window_medians(samples, 5.0)
        self.assertGreaterEqual(len(medians), 10)
        drift = G.drift(medians)
        self.assertAlmostEqual(drift["drift_ms_per_10min"], 180.0, delta=40.0)
        self.assertIsNotNone(drift["se_ms_per_10min"])
        self.assertTrue(drift["significant"], "a 180 ms/10 min drift must clear 2 sigma")

    def test_a_flat_series_reports_no_significant_drift(self):
        est = G.PlayheadEstimator()
        samples = synthetic(self.clock, seconds=60.0, hz=4.0, slope=1.0, noise_ms=8.0, seed=11)
        self.feed(est, samples)
        drift = G.drift(G.window_medians(samples, 5.0))
        self.assertLess(abs(drift["drift_ms_per_10min"]), 30.0)
        self.assertFalse(drift["significant"])

    def test_drift_on_the_probes_ten_minute_window(self):
        """The probe's real window: 10 min of 5 s medians resolves a 180 ms/10 min drift."""
        ten = synthetic(self.clock, seconds=600.0, hz=4.0, slope=1.0003, noise_ms=8.0, seed=3)
        medians = G.window_medians(ten, 5.0)
        drift = G.drift(medians)
        self.assertGreaterEqual(drift["n"], 100)
        self.assertAlmostEqual(drift["drift_ms_per_10min"], 180.0, delta=8.0)
        self.assertLess(drift["se_ms_per_10min"], 5.0)
        self.assertTrue(drift["significant"])

    def test_paused_reads_do_not_extrapolate(self):
        est = G.PlayheadEstimator()
        self.feed(est, self.series[:5])
        frozen = self.series[4].media
        verdict = est.add(sample_at(self.series[4].wall + 10.0, frozen, PLAYER_PAUSED))
        self.assertEqual(verdict, G.VERDICT_NOT_PLAYING)
        # 10 s later the paused playhead must still read the frozen position, not wall+offset
        self.assertAlmostEqual(est.playhead(self.series[4].wall + 10.0), frozen, delta=1e-6)

    def test_resume_starts_a_new_window(self):
        est = G.PlayheadEstimator()
        self.feed(est, self.series[:5])
        est.add(sample_at(self.series[4].wall + 1.0, self.series[4].media, PLAYER_PAUSED))
        resumed = sample_at(self.series[4].wall + 30.0, self.series[4].media + 30.0)
        self.assertEqual(est.add(resumed), G.VERDICT_RESUMED)
        self.assertEqual(est.jumps, 0, "a pause is not a jump")
        self.assertAlmostEqual(est.playhead(resumed.wall), resumed.media, delta=0.005)

    def test_empty_estimator_says_nothing(self):
        est = G.PlayheadEstimator()
        self.assertIsNone(est.playhead(1000.0))
        self.assertIsNone(est.residual_ms())
        self.assertEqual(est.stats()["points"], 0)

    def test_a_seek_gap_is_not_eaten_by_the_median(self):
        """A seek moves the group by seconds: the window must not average the two timelines."""
        est = G.PlayheadEstimator()
        self.feed(est, self.series[:5])
        est.reset()
        after = sample_at(self.series[4].wall + 1.0, 300.0)
        self.assertEqual(est.add(after), G.VERDICT_RESUMED)
        self.assertAlmostEqual(est.playhead(after.wall), 300.0, delta=0.005)


class TestRestartGate(unittest.TestCase):
    def setUp(self):
        self.gate = G.RestartGate()

    def test_one_non_playing_read_is_not_a_restart(self):
        self.gate.expect_playing(0.0)
        self.assertFalse(self.gate.observe(2.5, PLAYER_PAUSED))   # one stale read (A36)
        self.assertTrue(self.gate.observe(2.75, PLAYER_IDLE))     # two in a row (A36)

    def test_a_playing_read_resets_the_streak(self):
        self.gate.expect_playing(0.0)
        self.assertFalse(self.gate.observe(2.5, PLAYER_PAUSED))
        self.assertFalse(self.gate.observe(2.75, PLAYER_PLAYING))
        self.assertFalse(self.gate.observe(3.0, PLAYER_PAUSED))
        self.assertTrue(self.gate.observe(3.25, PLAYER_PAUSED))

    def test_no_read_counts_during_the_settle_window(self):
        self.gate.expect_playing(10.0)
        self.assertFalse(self.gate.observe(10.5, PLAYER_IDLE))
        self.assertFalse(self.gate.observe(11.5, PLAYER_IDLE))
        self.assertEqual(self.gate.streak, 0, "the ~0.6 s a LOAD needs is not a fault (A21)")
        self.assertFalse(self.gate.observe(12.5, PLAYER_IDLE))
        self.assertTrue(self.gate.observe(12.75, PLAYER_IDLE))

    def test_a_parked_group_is_never_restarted(self):
        self.gate.expect_parked()
        for i in range(5):
            self.assertFalse(self.gate.observe(100.0 + i, PLAYER_PAUSED))
        self.assertEqual(self.gate.streak, 0)


# ------------------------------------------------------------------------------- the sink
class TestConnect(unittest.TestCase):
    def test_resolves_the_group_by_name_at_every_start(self):
        g = build()
        g.connect()
        self.assertEqual((g.host, g.port), ("192.168.2.61", 32221))
        self.assertEqual(g.discoveries, 1)
        self.assertEqual(g.guard_calls, [("192.168.2.61", 32221)])
        self.assertEqual(g.links, [("192.168.2.61", 32221)])
        self.assertTrue(g.device.calls[0][0] == "connect_platform")
        self.assertEqual(g.device.receiver["volume"]["level"], 0.35)  # baseline recorded
        self.assertEqual(g.volume, 0.35)

    def test_a_new_port_is_used_at_the_next_start(self):
        """A Cast group's port is dynamic (A19): the name, not a cached endpoint, is the address."""
        g = build()
        g.connect()
        g.found = {"Cuisine": ("192.168.2.61", 32444)}
        g.connect()
        self.assertEqual((g.host, g.port), ("192.168.2.61", 32444))
        self.assertEqual(g.links, [("192.168.2.61", 32221), ("192.168.2.61", 32444)])
        self.assertEqual(g.guard_calls[-1], ("192.168.2.61", 32444))

    def test_an_unsafe_group_is_refused_before_any_link_is_opened(self):
        g = build(guard_ok=False)
        with self.assertRaises(G.UnsafeGroup):
            g.connect()
        self.assertEqual(g.links, [], "the guard must run before our own link exists")
        self.assertEqual(g.device.calls, [])

    def test_an_unknown_group_is_an_error_not_a_guess(self):
        g = build(found={})
        with self.assertRaises(G.GroupNotFound):
            g.connect()
        self.assertEqual(g.links, [])

    def test_a_configured_endpoint_is_kept_when_mdns_is_silent(self):
        g = build(found={}, host="192.168.2.61", port=32221)
        g.connect()
        self.assertEqual(g.guard_calls, [("192.168.2.61", 32221)])

    def test_warns_when_every_member_is_muted(self):
        """A37: a group whose members are muted plays happily and is silent — say it once."""
        g = build(members=[{"name": "Cuisine Frigo Jaune", "id": "u2", "audio_only": True,
                            "level": 0.55, "muted": True},
                           {"name": "Cuisine Meuble Bleu", "id": "u3", "audio_only": True,
                            "level": 0.55, "muted": True}])
        logs: list[str] = []
        g.log = logs.append
        g.connect()
        self.assertTrue(any("WARNING group members are silent" in line for line in logs), logs)

    def test_says_nothing_when_the_members_are_audible(self):
        g = build()
        logs: list[str] = []
        g.log = logs.append
        g.connect()
        self.assertFalse(any("silent" in line for line in logs), logs)

    def test_reconnect_re_guards_and_drops_the_old_link(self):
        g = build()
        g.connect()
        first = g.device
        g.guard_ok = False
        with self.assertRaises(G.UnsafeGroup):
            g.reconnect()
        self.assertTrue(first.closed, "a refused reconnect must not keep the old link")
        self.assertIsNone(g.link)
        self.assertEqual(len(g.guard_calls), 2, "the guard runs again on every reconnect (A6)")

    def test_reconnect_adopts_only_our_own_session(self):
        g = build()
        g.connect()
        self.assertTrue(g.prepare("http://192.168.2.21:7640/media/s/audio.m4a", "audio/mp4", 12.0))
        self.assertEqual(g.session, 4242)
        g.reconnect()
        self.assertEqual(g.session, 4242, "the group still runs the session we created")
        # someone else's session must never be adopted, let alone stopped
        g.device.status["mediaSessionId"] = 999
        g.reconnect()
        self.assertIsNone(g.session)


class TestTransport(unittest.TestCase):
    URL = "http://192.168.2.21:7640/media/sid/audio.m4a"

    def prepared(self, **kw) -> Harness:
        g = build(**kw)
        g.connect()
        self.assertTrue(g.prepare(self.URL, "audio/mp4", 12.5))
        return g

    def test_prepare_parks_in_pause_and_never_starts(self):
        g = self.prepared()
        loads = [payload for name, payload in g.device.calls if name == "load"]
        self.assertEqual(len(loads), 1)
        self.assertFalse(loads[0]["autoplay"], "prepare() must not start the audio")
        self.assertAlmostEqual(loads[0]["currentTime"], 12.5)
        self.assertEqual(g.state, PLAYER_PAUSED)
        self.assertNotIn("PLAY", g.device.transport_calls())

    def test_prepare_twice_reparks_with_a_seek_not_a_reload(self):
        g = self.prepared()
        self.assertTrue(g.prepare(self.URL, "audio/mp4", 40.0))
        self.assertEqual(g.device.transport_calls().count("load"), 1)
        self.assertIn("SEEK", g.device.transport_calls())
        seeks = [payload for name, payload in g.device.calls if name == "SEEK"]
        self.assertAlmostEqual(seeks[-1]["currentTime"], 40.0)
        self.assertEqual(seeks[-1]["resumeState"], "PLAYBACK_PAUSE")
        self.assertFalse(g.device.status["playerState"] == PLAYER_PLAYING)

    def test_one_play_and_no_reload_after_the_start(self):
        """Acceptance: no gaps after start — one LOAD, one PLAY, no SEEK in steady state."""
        g = self.prepared()
        g.play()
        calls = g.device.transport_calls()
        self.assertEqual(calls.count("load"), 1)
        self.assertEqual(calls.count("PLAY"), 1)
        self.assertEqual(calls[calls.index("PLAY"):], ["PLAY"])
        self.assertEqual(g.device.keepalives, 0)
        g.sample()
        self.assertEqual(g.device.keepalives, 1, "every poll keeps the link alive (A23)")

    def test_commands_need_a_session_we_created(self):
        g = build()
        g.connect()
        with self.assertRaises(G.ForeignSession):
            g.play()

    def test_stop_stops_our_session(self):
        g = self.prepared()
        g.stop()
        self.assertEqual(g.device.stopped, ["tr-1"])
        self.assertIsNone(g.session)

    def test_stop_never_touches_a_foreign_session(self):
        g = self.prepared()
        g.device.status["mediaSessionId"] = 999          # another sender took the group over
        g.stop()
        self.assertEqual(g.device.stopped, [])
        self.assertIsNone(g.session)

    def test_stop_does_nothing_when_we_created_nothing(self):
        g = build()
        g.connect()                                      # a session is already running here
        g.device.status["mediaSessionId"] = 777
        g.stop()
        self.assertEqual(g.device.stopped, [])

    def test_a_foreign_session_is_noticed_and_ours_is_forgotten(self):
        g = self.prepared()
        g.device.status["mediaSessionId"] = 999
        g.sample()
        self.assertIsNone(g.session)
        with self.assertRaises(G.ForeignSession):
            g.pause()


class TestVolume(unittest.TestCase):
    def connected(self) -> Harness:
        g = build()
        g.connect()
        return g

    def test_zero_is_muted_never_sent(self):
        g = self.connected()
        g.set_volume(0.0)
        self.assertEqual(g.device.volume_calls[-1], {"level": None, "muted": True})
        self.assertNotIn(0.0, [lvl for lvl in g.device.levels_sent() if lvl is not None])
        self.assertEqual(g.volume, 0.35, "the level on the device is left untouched (A37)")

    def test_mirror_sends_a_level_and_unmutes(self):
        g = self.connected()
        g.set_volume(0.22, muted=False)
        self.assertEqual(g.device.volume_calls[-1], {"level": 0.22, "muted": False})
        self.assertEqual(g.volume, 0.22)

    def test_mirror_is_a_front_not_a_resend(self):
        g = self.connected()
        g.set_volume(0.22, muted=False)
        sent = len(g.device.volume_calls)
        g.set_volume(0.221, muted=False)
        self.assertEqual(len(g.device.volume_calls), sent, "a mirror only acts on a change")

    def test_a_mute_is_idempotent(self):
        g = self.connected()
        g.set_volume(0.0)
        sent = len(g.device.volume_calls)
        g.set_volume(0.0)
        self.assertEqual(len(g.device.volume_calls), sent)


class TestObservation(unittest.TestCase):
    URL = "http://192.168.2.21:7640/media/sid/audio.m4a"

    def playing(self) -> Harness:
        g = build()
        g.connect()
        self.assertTrue(g.prepare(self.URL, "audio/mp4", 0.0))
        g.play()
        g.clock.advance(G.SETTLE_S + 0.1)
        return g

    def test_sample_reports_a_reading_and_the_estimator_tracks_it(self):
        g = self.playing()
        readings = []
        for _ in range(6):
            g.device.status["currentTime"] += 0.25
            reading = g.sample()
            self.assertIsNotNone(reading)
            readings.append(reading)
            g.clock.advance(0.25)
        self.assertEqual(readings[-1].player_state, PLAYER_PLAYING)
        self.assertEqual(readings[-1].session_id, 4242)
        estimate = g.playhead(readings[-1].wall)
        self.assertIsNotNone(estimate)
        self.assertAlmostEqual(estimate, readings[-1].media, delta=0.02)

    def test_one_non_playing_read_does_not_ask_for_a_restart(self):
        g = self.playing()
        g.device.status["playerState"] = PLAYER_PAUSED
        self.assertIsNotNone(g.sample())          # the read is reported...
        self.assertFalse(g.restart_needed())      # ...but it is not a restart decision (A36)
        g.clock.advance(0.25)
        g.sample()
        self.assertTrue(g.restart_needed())       # two in a row are a fact (A36)

    def test_a_stale_read_is_rejected_by_sample(self):
        g = self.playing()
        for _ in range(6):
            g.device.status["currentTime"] += 0.25
            g.sample()
            g.clock.advance(0.25)
        g.device.status["currentTime"] += 0.25 + 0.150      # a 150 ms stale MEDIA_STATUS
        self.assertIsNone(g.sample())
        self.assertEqual(g.estimator.rejections, 1)
        g.device.status["currentTime"] += 0.25 - 0.150
        self.assertIsNotNone(g.sample(), "the next good read is accepted")

    def test_two_read_errors_ask_for_a_reconnect(self):
        g = self.playing()
        g.device.error = OSError("link dropped")
        self.assertIsNone(g.sample())
        self.assertFalse(g.needs_reconnect())
        self.assertIsNone(g.sample())
        self.assertTrue(g.needs_reconnect())
        self.assertEqual(g.stats()["link_errors"], 2)

    def test_sample_without_a_transport_says_nothing(self):
        g = build()
        g.connect()
        self.assertIsNone(g.sample())

    def test_prepare_parks_the_estimator_on_the_parked_position(self):
        g = build()
        g.connect()
        self.assertTrue(g.prepare(self.URL, "audio/mp4", 12.5))
        self.assertAlmostEqual(g.playhead(g.clock()), 12.5, delta=1e-9)

    def test_pause_freezes_the_playhead(self):
        """A paused playhead does not advance: extrapolating it invents an error no loop can see."""
        g = self.playing()
        for _ in range(6):
            g.device.status["currentTime"] += 0.25
            g.sample()
            g.clock.advance(0.25)
        g.pause()
        frozen = g.playhead(g.clock())
        self.assertIsNotNone(frozen)
        g.clock.advance(5.0)
        self.assertAlmostEqual(g.playhead(g.clock()), frozen, delta=1e-9)


class TestInterfaceParity(unittest.TestCase):
    """The fake group and the real sink must present the same surface (both fill the contract)."""

    def test_fake_group_and_cast_group_agree(self):
        real = {name for name in dir(G.CastGroup) if not name.startswith("_")}
        fake = {name for name in dir(FakeGroup) if not name.startswith("_")}
        self.assertTrue({"connect", "reconnect", "prepare", "play", "pause", "seek",
                         "set_volume", "stop", "sample", "playhead", "close"} <= real & fake)

    def test_fake_group_drives_the_estimator_too(self):
        """A fake group is enough to exercise the playhead path: no device, no socket."""
        clock = FakeClock()
        g = FakeGroup(clock=clock)
        g.connect()
        g.prepare("http://x/y.m4a", "audio/mp4", 30.0)
        g.play()
        self.assertEqual(g.transport_calls(), ["prepare", "play"])
        clock.advance(1.0)
        reading = g.sample()
        self.assertAlmostEqual(reading.media, 31.0, delta=0.01)


class TestProbeScript(unittest.TestCase):
    """The probe is the gate's measurement: its gates are tested, its measurement is not run."""

    def run_probe(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, PROBE, *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=120)

    def test_dry_run_is_the_default_and_constructs_no_transport(self):
        proc = self.run_probe()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"mode": "dry-run"', proc.stdout)
        self.assertIn("transport_constructed: 0", proc.stdout)
        self.assertIn("NEEDS AN AUDIO WINDOW", proc.stdout)
        self.assertEqual(json.loads(proc.stdout.split("\n\n")[0])["transport_constructed"], 0)

    def test_dry_run_plan_has_the_eight_landings_and_the_minutes(self):
        plan = json.loads(self.run_probe("--dry-run", "--minutes", "3").stdout.split("\n\n")[0])
        steps = " ".join(step["what"] for step in plan["steps"])
        self.assertIn("8 landings", steps)
        self.assertIn("3.0 min", steps)
        self.assertIn("groupguard", steps)
        self.assertEqual(plan["refuses"], ["without --mute", "without --i-have-an-audio-window",
                                           "any group that fails groupguard"])
        self.assertTrue(plan["needs_audio_window"])

    def test_refuses_to_run_without_mute(self):
        proc = self.run_probe("--run", "--i-have-an-audio-window", "--url", "http://x/y.m4a")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--mute", proc.stderr)

    def test_refuses_to_run_without_an_explicit_audio_window(self):
        proc = self.run_probe("--run", "--mute", "--url", "http://x/y.m4a")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--i-have-an-audio-window", proc.stderr)

    def test_refuses_to_run_without_media(self):
        proc = self.run_probe("--run", "--mute", "--i-have-an-audio-window")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--url", proc.stderr)

    def test_redacts_the_media_url(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import group_probe_v020 as probe
        red = probe.redact("https://rr3---sn-abc.googlevideo.com/videoplayback?expire=1&sig=x")
        self.assertEqual(red, "https://rr3---sn-abc.googlevideo.com/videop…")
        self.assertNotIn("expire", red)


if __name__ == "__main__":
    unittest.main()
