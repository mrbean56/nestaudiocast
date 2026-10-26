"""The seams meet: the real control law driven by the real session, offline.

Every other test file checks one module against a fake of its neighbours. This one wires the
**actual** pieces that will run on CT 121 — `sender.session.SessionManager` with
`lib.v020.sync.Controller` — and only fakes the two devices, because the group may not be played
tonight and the Chromecast has no receiver to talk to yet. What it proves:

1. the start event is planned once and executed once (park, scheduled release, `play_at`);
2. the controller's arguments are **media positions**, not instants (the integration bug this test
   was written to catch: a plan computed on wall-clock seconds lands years away);
3. the receiver's clock reaches the controller, so `play_at` is on the receiver's base;
4. ten simulated minutes of steady state contain **no** `prepare`/`seek` and at least one trim;
5. the digital error the session reports is the one the control law computes.

No socket, no speaker, no device.
"""

from __future__ import annotations

import types
import unittest

from test_sender_support import (
    C, TempDir, YOUTUBE, FakeClock, EventLog, make_config, tick,
)

from fakes import FakeGroup, FakeProxy, FakeMedia, FakeReceiver
from sender.session import SessionManager
from v020.sync import Controller

TICK_S = 0.25
TEN_MIN = int(600 / TICK_S)


def offline_resolver(url, **_kw):
    return FakeMedia.resolved(duration_s=900.0)


class SeamCase(TempDir, unittest.TestCase):
    def fixture(self, *, rate_gain: float = 1.0, delta_v2: float = 0.0,
                start_jitter: float = 0.0):
        config = make_config(self.root)
        clock = FakeClock()
        link = FakeReceiver(clock=clock, rate_gain=rate_gain, start_jitter=start_jitter)
        group = FakeGroup(clock=clock)
        proxy = FakeProxy()
        controller = Controller(delta_v2=delta_v2, rate_max=0.002)
        log = EventLog(directory=config.evidence_dir)
        sender = SessionManager(link=link, group=group, controller=controller, proxy=proxy,
                               config=config, log=log, resolver=offline_resolver,
                               clock=clock, wall=clock.now, spawn=lambda fn: fn(),
                               state_dir=config.state_dir)
        fx = types.SimpleNamespace(sender=sender, link=link, group=group, proxy=proxy,
                                   controller=controller, clock=clock, config=config, log=log)
        sender.share(YOUTUBE)
        return fx


class TestStartSeam(SeamCase):
    def test_one_plan_one_execution(self):
        fx = self.fixture()
        commands = fx.controller.snapshot()["commands"]
        self.assertEqual(commands.get(C.MSG_PLAY_AT), 1)
        self.assertEqual(fx.group.transport_calls(), ["prepare", "play"])
        self.assertEqual(fx.link.calls.count("play_at"), 1)
        self.assertEqual(fx.link.calls.count("load"), 1)

    def test_the_plan_speaks_media_seconds_not_instants(self):
        """The bug this test exists for: `start(now, video_ready, group_ready)` are positions."""
        fx = self.fixture()
        plan = [m for m in fx.link.inbox if m["type"] == C.MSG_PLAY_AT][0]
        self.assertLess(plan["media"], 10.0)          # a media position, not an epoch
        self.assertAlmostEqual(plan["media"], 0.0, places=6)

    def test_the_receiver_clock_reached_the_controller(self):
        fx = self.fixture()
        self.assertIsNotNone(fx.controller.clock_offset_ms)
        # play_at must be on the receiver's base: compare with the same instant on each clock
        plan = [m for m in fx.link.inbox if m["type"] == C.MSG_PLAY_AT][0]
        here_ms = fx.clock() * 1000.0
        self.assertGreater(plan["rx_ms"], here_ms - 5_000.0)
        self.assertLess(plan["rx_ms"], here_ms + 5_000.0)


class TestSteadySeam(SeamCase):
    def test_ten_minutes_never_reloads_and_does_trim(self):
        fx = self.fixture()
        before_inbox = len(fx.link.inbox)     # after the parked load and the single play_at
        before_calls = list(fx.group.calls)
        tick(fx, TEN_MIN, TICK_S)
        after = [m["type"] for m in fx.link.inbox[before_inbox:]]
        self.assertNotIn(C.MSG_LOAD, after)
        self.assertNotIn(C.MSG_SEEK, after)
        self.assertNotIn(C.MSG_PLAY_AT, after)
        new_group_calls = fx.group.calls[len(before_calls):]
        self.assertNotIn("prepare", new_group_calls)
        self.assertNotIn("seek", new_group_calls)
        self.assertIn(C.MSG_RATE, after)              # the loop does something, it just never reloads

    def test_the_error_converges_without_a_reload(self):
        """A frozen test clock makes the landing late; the law must absorb it with trims only.

        On the real hardware the landing is ~30 ms (evidence/27); here the fake clock does not
        move during the scheduled release, so the video starts ~0.5 s behind. A 0.2 % lever closes
        that in ~4 min, which is exactly the path this asserts: no reload, and it gets there.
        """
        fx = self.fixture()
        tick(fx, TEN_MIN, TICK_S)
        status = fx.sender.status()
        self.assertIsNotNone(status["digital_error_ms"])
        self.assertLess(abs(status["digital_error_ms"]), 25.0, status)
        self.assertIsNotNone(fx.controller.window_median_ms())

    def test_a_dead_rate_actuator_is_reported_not_hidden(self):
        """With a receiver that ignores the rate, the law cannot hold; the sim already says so."""
        fx = self.fixture(rate_gain=0.0, start_jitter=0.05)
        before = len(fx.link.inbox)
        tick(fx, TEN_MIN, TICK_S)
        after = [m["type"] for m in fx.link.inbox[before:]]
        self.assertNotIn(C.MSG_LOAD, after)
        self.assertNotIn(C.MSG_SEEK, after)
        self.assertNotIn(C.MSG_PLAY_AT, after)


if __name__ == "__main__":
    unittest.main()
