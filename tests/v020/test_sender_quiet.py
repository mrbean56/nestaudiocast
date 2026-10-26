"""Quiet hours — contracts §7 (and the Config validation that feeds them).

The window is the *allowed* window: outside it the policy applies. Boundaries are tested at
the exact minute because that is where an off-by-one hides.
"""

from __future__ import annotations

import datetime
import unittest

from test_sender_support import C, TempDir, make_config, make_sender, YOUTUBE

from sender.config import ConfigError
from sender.quiet import DECISION_OK, QuietWindow, format_hhmm, parse_hhmm


def at(hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 10, 25, hour, minute)


class TestParse(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_hhmm("00:00"), 0)
        self.assertEqual(parse_hhmm("7:05"), 7 * 60 + 5)
        self.assertEqual(parse_hhmm("23:59"), 23 * 60 + 59)
        self.assertIsNone(parse_hhmm(""))
        self.assertIsNone(parse_hhmm("   "))

    def test_invalid(self):
        for bad in ("24:00", "7:60", "0700", "7h", "-1:00", "12:0"):
            with self.assertRaises(ValueError, msg=bad):
                parse_hhmm(bad)

    def test_format_roundtrip(self):
        self.assertEqual(format_hhmm(parse_hhmm("06:30")), "06:30")
        self.assertEqual(format_hhmm(None), "")


class TestWindow(unittest.TestCase):
    def test_disabled_when_empty(self):
        window = QuietWindow("", "", "refuse")
        self.assertFalse(window.enabled)
        self.assertEqual(window.decision(at(3)), DECISION_OK)
        self.assertTrue(window.allowed(at(3)))

    def test_inside_the_window_is_allowed(self):
        window = QuietWindow("08:00", "22:00", "refuse")
        self.assertTrue(window.allowed(at(8, 0)))       # start included
        self.assertTrue(window.allowed(at(15, 30)))
        self.assertFalse(window.allowed(at(22, 0)))     # end excluded
        self.assertFalse(window.allowed(at(7, 59)))
        self.assertEqual(window.decision(at(23)), "refuse")

    def test_refuse_policy(self):
        window = QuietWindow("08:00", "22:00", "refuse")
        self.assertEqual(window.decision(at(3)), "refuse")
        self.assertEqual(window.decision(at(12)), DECISION_OK)

    def test_video_only_policy(self):
        window = QuietWindow("08:00", "22:00", "video_only")
        self.assertEqual(window.decision(at(3)), "video_only")
        self.assertEqual(window.decision(at(12)), DECISION_OK)

    def test_wraps_midnight(self):
        window = QuietWindow("22:00", "06:00", "refuse")
        self.assertTrue(window.wraps_midnight)
        self.assertTrue(window.allowed(at(23, 59)))
        self.assertTrue(window.allowed(at(0, 0)))
        self.assertTrue(window.allowed(at(5, 59)))
        self.assertFalse(window.allowed(at(6, 0)))
        self.assertFalse(window.allowed(at(21, 59)))

    def test_zero_length_window_gates_nothing(self):
        window = QuietWindow("07:00", "07:00", "refuse")
        self.assertTrue(window.allowed(at(3)))
        self.assertEqual(window.decision(at(3)), DECISION_OK)

    def test_half_a_window_is_refused(self):
        with self.assertRaises(ValueError):
            QuietWindow("08:00", "", "refuse")
        with self.assertRaises(ValueError):
            QuietWindow("", "08:00", "refuse")

    def test_bad_policy(self):
        with self.assertRaises(ValueError):
            QuietWindow("08:00", "22:00", "maybe")

    def test_describe(self):
        described = QuietWindow("08:00", "22:00", "video_only").describe(at(3))
        self.assertTrue(described["enabled"])
        self.assertEqual(described["start"], "08:00")
        self.assertEqual(described["end"], "22:00")
        self.assertEqual(described["policy"], "video_only")
        self.assertFalse(described["allowed_now"])
        self.assertEqual(described["decision"], "video_only")
        self.assertFalse(QuietWindow("", "").describe(at(3))["enabled"])

    def test_epoch_seconds_are_local_time(self):
        moment = datetime.datetime(2026, 10, 25, 3, 15).timestamp()
        window = QuietWindow("08:00", "22:00", "refuse")
        self.assertEqual(window.decision(moment), "refuse")


class TestConfigFeedsQuiet(unittest.TestCase):
    def test_config_builds_the_window(self):
        from test_sender_support import make_config
        import tempfile
        root = tempfile.mkdtemp(prefix="cast-sender-quiet-")
        config = make_config(root, quiet_start="08:00", quiet_end="22:00",
                             quiet_policy="video_only")
        self.assertEqual(config.quiet.policy, "video_only")
        self.assertEqual(config.quiet.start, 8 * 60)

    def test_bad_quiet_config_is_refused(self):
        import tempfile
        root = tempfile.mkdtemp(prefix="cast-sender-quiet-")
        with self.assertRaises(ConfigError):
            make_config(root, quiet_start="8h")
        with self.assertRaises(ConfigError):
            make_config(root, quiet_start="08:00")
        with self.assertRaises(ConfigError):
            make_config(root, quiet_policy="silent")


class TestSharePolicy(TempDir, unittest.TestCase):
    """A quiet share must not touch the group — the point of §7."""

    def _fixture(self, start: str, end: str, policy: str):
        config = make_config(self.root, quiet_start=start, quiet_end=end,
                             quiet_policy=policy)
        return make_sender(self.root, config=config)

    def test_refuse_touches_nothing(self):
        fixture = self._fixture("00:00", "00:01", "refuse")     # closed 23h59 of the day
        reply = fixture.sender.share(YOUTUBE)
        self.assertEqual(reply["state"], C.STATE_QUIET_REFUSED)
        self.assertTrue(reply["session_id"].startswith("s"))
        self.assertEqual(fixture.group.calls, [])
        self.assertFalse(fixture.link.launched)
        self.assertFalse(fixture.proxy.sessions)
        self.assertFalse(fixture.sender.status()["group_owner"])
        fixture.sender.close()

    def test_video_only_never_loads_the_group(self):
        fixture = self._fixture("00:00", "00:01", "video_only")
        reply = fixture.sender.share(YOUTUBE)
        self.assertEqual(reply["state"], "resolving")
        self.assertEqual(fixture.sender.status()["state"], C.STATE_PLAYING)
        self.assertTrue(fixture.link.launched)          # the video does play
        self.assertEqual(fixture.group.calls, [])       # the group is never touched
        self.assertFalse(fixture.proxy.sessions == {})
        fixture.sender.close()

    def test_open_window_starts_normally(self):
        fixture = self._fixture("00:00", "23:59", "refuse")
        fixture.sender.share(YOUTUBE)
        self.assertEqual(fixture.sender.status()["state"], C.STATE_PLAYING)
        self.assertEqual(fixture.group.transport_calls(), ["prepare", "play"])
        fixture.sender.close()

    def test_a_running_session_is_not_stopped_by_the_window(self):
        fixture = make_sender(self.root)
        fixture.sender.share(YOUTUBE)
        self.assertEqual(fixture.sender.status()["state"], C.STATE_PLAYING)
        # the window opens (a quiet policy is now what a *new* share would meet); the running
        # session keeps playing and is not stopped (§7: the rule is about starting)
        fixture.sender._quiet = __import__("sender.quiet", fromlist=["QuietWindow"]).QuietWindow(
            "00:00", "00:01", "refuse")
        for _ in range(4):
            fixture.clock.advance(0.25)
            fixture.sender.tick()
        self.assertEqual(fixture.sender.status()["state"], C.STATE_PLAYING)
        self.assertEqual(fixture.group.calls.count("stop"), 0)
        fixture.sender.close()


if __name__ == "__main__":
    unittest.main()
