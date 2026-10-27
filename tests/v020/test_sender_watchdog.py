"""A deaf link is noticed and repaired — measured on the Salon, 2026-10-27.

244 s into a silent 10-minute session the controller stopped hearing the page (no state, no
clock reply) while the page went on playing to 623 s; the service kept reporting its last
reading and never reconnected. The session now watches the page's 4 Hz state: no new one for
``RECEIVER_SILENT_S`` → one bounded reconnect, or the session ends (§3.3: never relaunch).
"""

from __future__ import annotations

import unittest

from test_sender_support import YOUTUBE, C, TempDir, events_named, make_sender

import sender.session as S

TICK = 0.25


class Deafen:
    """Freezes the fake receiver's state stream, as the real link did."""

    def __init__(self, link) -> None:
        self.link = link
        self.frozen = None
        self.reconnects = 0
        self.fail = None
        self._latest = link.latest
        link.latest = self.latest
        link.reconnect = self.reconnect

    def deafen(self) -> None:
        self.frozen = self._latest()

    def latest(self):
        return self.frozen if self.frozen is not None else self._latest()

    def reconnect(self, attempts=None, timeout=5.0):
        self.reconnects += 1
        if self.fail is not None:
            raise self.fail
        self.frozen = None


class WatchdogCase(TempDir, unittest.TestCase):
    def started(self):
        fx = make_sender(self.root)
        fx.sender.share(YOUTUBE)
        self.run_for(fx, 2.0)
        self.assertEqual(fx.sender.status()["state"], C.STATE_PLAYING)
        return fx

    def run_for(self, fx, seconds: float) -> None:
        for _ in range(int(seconds / TICK)):
            fx.clock.advance(TICK)
            fx.sender.tick()


class TestSilence(WatchdogCase):
    def test_a_deaf_link_is_reconnected_and_the_session_goes_on(self):
        fx = self.started()
        deaf = Deafen(fx.link)
        self.run_for(fx, 10.0)
        self.assertEqual(deaf.reconnects, 0, "a talking page must not be reconnected")
        deaf.deafen()
        self.run_for(fx, S.RECEIVER_SILENT_S + 1.0)
        self.assertEqual(deaf.reconnects, 1)
        self.assertEqual(fx.sender.status()["state"], C.STATE_PLAYING)
        self.assertTrue(events_named(fx.log.path, "receiver_silent"))
        self.assertTrue(events_named(fx.log.path, "receiver_reconnected"))
        self.run_for(fx, 10.0)
        self.assertEqual(deaf.reconnects, 1, "once heard again, no further reconnect")

    def test_a_link_that_cannot_be_rebuilt_ends_the_session(self):
        fx = self.started()
        deaf = Deafen(fx.link)
        deaf.fail = RuntimeError("no hello")
        deaf.deafen()
        self.run_for(fx, S.RECEIVER_SILENT_S + 1.0)
        self.assertEqual(deaf.reconnects, 1)
        self.assertEqual(fx.sender.status()["state"], C.STATE_IDLE)
        lost = events_named(fx.log.path, "device_lost")
        self.assertTrue(lost and lost[-1]["why"].startswith("receiver_silent"), lost)

    def test_a_paused_page_still_talks_and_is_left_alone(self):
        fx = self.started()
        deaf = Deafen(fx.link)
        fx.sender.control(C.ACTION_PAUSE)
        self.run_for(fx, 20.0)
        self.assertEqual(deaf.reconnects, 0)


class TestClockBackoff(WatchdogCase):
    def test_a_failed_clock_refresh_is_not_retried_on_every_tick(self):
        fx = self.started()
        calls = {"n": 0}

        def broken(n=5):
            calls["n"] += 1
            raise RuntimeError("no clock_reply")

        fx.link.ping = broken
        self.run_for(fx, S.CLOCK_REFRESH_S + 1.0)
        first = calls["n"]
        self.assertGreaterEqual(first, 1)
        self.run_for(fx, S.CLOCK_RETRY_S * 3 - 1.0)
        self.assertLessEqual(calls["n"] - first, 3, calls)


if __name__ == "__main__":
    unittest.main()
