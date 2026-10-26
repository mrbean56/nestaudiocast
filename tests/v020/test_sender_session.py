"""The session state machine — contracts §1, §3.3, §5, §7, §8.

The acceptance test of brief G lives here: **a 10-minute simulated session emits zero
``load``/``seek`` after the start event**, even when the controller asks for one on every tick.
The guard is in the session, so the property holds whatever the control law does.
"""

from __future__ import annotations

import os
import unittest

from test_sender_support import (
    C, TempDir, YOUTUBE, events_named, make_config, make_sender, read_log, tick,
)

from sender.offline import HostileController, TrimController
from sender.session import ControlError, ShareError

TEN_MIN_TICKS = 2400
TICK_S = 0.25


class SessionCase(TempDir, unittest.TestCase):
    def started(self, **kw):
        fixture = make_sender(self.root, **kw)
        reply = fixture.sender.share(YOUTUBE)
        self.assertEqual(reply["state"], C.STATE_RESOLVING)
        return fixture

    def events(self, fixture, name):
        return events_named(fixture.log.path, name)


class TestStart(SessionCase):
    def test_a_share_starts_a_session(self):
        fixture = self.started()
        status = fixture.sender.status()
        self.assertEqual(status["state"], C.STATE_PLAYING)
        self.assertTrue(status["session_id"].startswith("s"))
        self.assertEqual(status["video_id"], "QzomK1fdSUg")
        # the group is parked then released, in that order
        self.assertEqual(fixture.group.transport_calls(), ["prepare", "play"])
        self.assertTrue(fixture.link.launched)
        self.assertEqual(fixture.link.calls.count("load"), 1)
        self.assertEqual(fixture.link.calls.count("play_at"), 1)
        self.assertEqual(len(fixture.proxy.sessions), 1)
        self.assertTrue(os.path.isfile(fixture.lock_path))
        self.assertTrue(os.path.isfile(fixture.record_path))
        self.assertEqual(self.events(fixture, "session_started")[0]["state"], "playing")

    def test_steady_since_appears_after_thirty_seconds(self):
        fixture = self.started()
        self.assertIsNone(fixture.sender.status()["steady_since"])
        tick(fixture, 100, TICK_S)                       # 25 s: still not steady
        self.assertIsNone(fixture.sender.status()["steady_since"])
        tick(fixture, 40, TICK_S)                        # 35 s
        steady = fixture.sender.status()["steady_since"]
        self.assertIsInstance(steady, float)
        started_wall = fixture.clock.wall
        self.assertAlmostEqual(steady, started_wall + C.STEADY_AFTER_S, delta=2.0)

    def test_status_has_every_field_of_the_contract(self):
        fixture = self.started()
        status = fixture.sender.status()
        for key in ("state", "session_id", "video_id", "position_video", "position_group",
                    "digital_error_ms", "p95_ms", "rate", "steady_since", "quiet",
                    "group_owner", "delta_v2", "last_event", "last_error"):
            self.assertIn(key, status)
        self.assertEqual(status["group_owner"], "cast-sender")
        self.assertEqual(status["delta_v2"], 0.0)
        self.assertFalse(status["quiet"]["enabled"])
        self.assertEqual(status["last_event"], "session_started")

    def test_a_bad_url_is_refused_before_anything_starts(self):
        fixture = make_sender(self.root)
        for bad in ("", "   ", "youtube.com/watch?v=x", "ftp://host/x", None, 12):
            with self.assertRaises(ShareError, msg=repr(bad)):
                fixture.sender.share(bad)
        self.assertEqual(fixture.group.calls, [])
        self.assertFalse(fixture.link.launched)
        self.assertEqual(fixture.sender.status()["state"], C.STATE_IDLE)

    def test_ids_only_are_logged_never_urls(self):
        fixture = self.started()
        with open(fixture.log.path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertIn("QzomK1fdSUg", raw)
        self.assertNotIn("youtube.com/watch", raw)
        self.assertNotIn("token=REDACTED", raw)


class TestNoReloadAfterStart(SessionCase):
    """Brief G's acceptance criterion, in code."""

    def _run(self, controller):
        fixture = make_sender(self.root, controller=controller)
        fixture.sender.share(YOUTUBE)
        marks = {name: fixture.link.calls.count(name) for name in ("load", "seek", "play_at")}
        group_marks = fixture.group.calls.count("prepare"), fixture.group.calls.count("seek")
        self.assertEqual(fixture.sender.status()["state"], C.STATE_PLAYING)
        for _ in range(TEN_MIN_TICKS):
            fixture.clock.advance(TICK_S)
            fixture.sender.tick()
        after = fixture.link.calls
        return {
            "state": fixture.sender.status()["state"],
            "load": after.count("load") - marks["load"],
            "seek": after.count("seek") - marks["seek"],
            "play_at": after.count("play_at") - marks["play_at"],
            "group_prepare": fixture.group.calls.count("prepare") - group_marks[0],
            "group_seek": fixture.group.calls.count("seek") - group_marks[1],
            "rate": after.count("rate"),
            "fixture": fixture,
        }

    def test_ten_minutes_with_a_hostile_controller(self):
        controller = HostileController()
        result = self._run(controller)
        fixture = result["fixture"]
        self.assertEqual(result["state"], C.STATE_PLAYING)
        self.assertGreater(controller.asked_loads, 2000)      # it really did ask, every tick
        self.assertEqual(result["load"], 0)
        self.assertEqual(result["seek"], 0)
        self.assertEqual(result["play_at"], 0)
        self.assertEqual(result["group_prepare"], 0)
        self.assertEqual(result["group_seek"], 0)
        refusals = self.events(fixture, "reload_refused")
        self.assertTrue(refusals, "a refused reload must be logged, not swallowed")
        self.assertIn("only rate trims", refusals[0]["reason"])
        self.assertGreaterEqual(refusals[0]["total"], 1)

    def test_ten_minutes_with_a_trimming_controller(self):
        result = self._run(TrimController(always_trim=True))
        self.assertEqual(result["load"], 0)
        self.assertEqual(result["seek"], 0)
        self.assertGreater(result["rate"], 100)                # trims do pass: not a blanket ban
        self.assertEqual(self.events(result["fixture"], "reload_refused"), [])

    def test_the_group_playhead_advances_with_the_video(self):
        fixture = make_sender(self.root)
        fixture.sender.share(YOUTUBE)
        for _ in range(40):                                   # 10 s
            fixture.clock.advance(TICK_S)
            fixture.sender.tick()
        status = fixture.sender.status()
        self.assertAlmostEqual(status["position_video"], 10.0, delta=0.6)
        self.assertAlmostEqual(status["position_group"], 10.0, delta=0.6)
        self.assertLess(abs(status["digital_error_ms"]), 20.0)
        self.assertIsNotNone(status["p95_ms"])


class TestControl(SessionCase):
    def test_pause_and_resume(self):
        fixture = self.started()
        reply = fixture.sender.control("pause")
        self.assertEqual(reply, {"ok": True, "state": C.STATE_PAUSED})
        self.assertIn("pause", fixture.group.calls)
        self.assertIn(C.MSG_PAUSE, fixture.link.calls)
        reply = fixture.sender.control("play")
        self.assertEqual(reply["state"], C.STATE_PLAYING)
        self.assertEqual(fixture.group.calls[-1], "play")
        # a resume is allowed one re-landing (§1), and it happened
        self.assertEqual(fixture.link.calls.count("play_at"), 2)
        self.assertTrue(self.events(fixture, "transition_open"))

    def test_pause_twice_is_idempotent(self):
        fixture = self.started()
        fixture.sender.control("pause")
        pause_calls = fixture.group.calls.count("pause")
        self.assertEqual(fixture.sender.control("pause")["state"], C.STATE_PAUSED)
        self.assertEqual(fixture.group.calls.count("pause"), pause_calls)

    def test_seek_moves_both_sides_once(self):
        fixture = self.started()
        reply = fixture.sender.control("seek", position=123.5)
        self.assertEqual(reply["state"], C.STATE_PLAYING)
        self.assertIn("seek", fixture.group.calls)
        self.assertIn(C.MSG_SEEK, fixture.link.calls)
        self.assertAlmostEqual(fixture.group.media, 123.5)

    def test_volume(self):
        fixture = self.started()
        self.assertEqual(fixture.sender.control("volume", level=0.4)["ok"], True)
        self.assertEqual((fixture.group.volume, fixture.group.muted), (0.4, None))
        self.assertEqual(fixture.sender.control("volume", muted=True)["ok"], True)
        self.assertEqual(fixture.group.muted, True)
        self.assertEqual(fixture.group.volume, 0.4)            # a mute never changes the level

    def test_a_level_of_zero_is_refused(self):
        """A37: muting is how we silence; a zeroed level stays silent after an unmute."""
        fixture = self.started()
        with self.assertRaises(ControlError) as caught:
            fixture.sender.control("volume", level=0.0)
        self.assertEqual(caught.exception.code, 400)
        self.assertNotEqual(fixture.group.volume, 0.0)

    def test_volume_validation(self):
        fixture = self.started()
        with self.assertRaises(ControlError):
            fixture.sender.control("volume")
        with self.assertRaises(ControlError):
            fixture.sender.control("volume", level=1.5)
        with self.assertRaises(ControlError):
            fixture.sender.control("volume", muted="yes")

    def test_a_mute_without_a_known_level_says_so(self):
        fixture = self.started()
        with self.assertRaises(ControlError) as caught:
            fixture.sender.control("volume", muted=True)
        self.assertEqual(caught.exception.code, 409)
        self.assertIn("no volume getter", str(caught.exception))

    def test_unknown_action_and_bad_position(self):
        fixture = self.started()
        with self.assertRaises(ControlError):
            fixture.sender.control("explode")
        with self.assertRaises(ControlError) as caught:
            fixture.sender.control("seek")
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(ControlError):
            fixture.sender.control("seek", position=-3)

    def test_control_without_a_session(self):
        fixture = make_sender(self.root)
        for action in ("play", "pause", "seek", "volume"):
            with self.assertRaises(ControlError, msg=action):
                fixture.sender.control(action, position=1.0, level=0.2)
        self.assertEqual(fixture.sender.control("stop"), {"ok": True, "state": C.STATE_IDLE})

    def test_stop_phases_everything_out(self):
        fixture = self.started()
        reply = fixture.sender.control("stop")
        self.assertEqual(reply["state"], C.STATE_ENDED)
        self.assertIn("stop", fixture.group.calls)
        self.assertIn("stop", fixture.link.calls)              # the idle page
        self.assertEqual(fixture.proxy.sessions, {})
        self.assertFalse(os.path.exists(fixture.lock_path))
        self.assertFalse(os.path.exists(fixture.record_path))
        tick(fixture, 9, 0.25)
        self.assertEqual(fixture.sender.status()["state"], C.STATE_IDLE)
        self.assertEqual(fixture.sender.status()["session_id"], None)


class TestSessionEdges(SessionCase):
    def test_a_new_share_stops_the_old_session_first(self):
        fixture = self.started()
        first = fixture.sender.status()["session_id"]
        second = fixture.sender.share("https://www.youtube.com/watch?v=K0uxjdcZWyk")
        self.assertNotEqual(first, second["session_id"])
        self.assertEqual(fixture.group.calls.count("stop"), 1)   # the old one was stopped
        self.assertEqual(len(fixture.proxy.sessions), 1)         # its media session is gone
        self.assertEqual(fixture.sender.status()["state"], C.STATE_PLAYING)
        ended = [e for e in self.events(fixture, "session_ended")]
        self.assertEqual(ended[0]["reason"], "new_share")

    def test_a_foreign_takeover_ends_the_session_without_relaunching(self):
        fixture = self.started()
        before = {name: fixture.link.calls.count(name) for name in ("load", "play_at", "seek")}
        fixture.link.takeover()
        fixture.clock.advance(0.25)
        fixture.sender.tick()
        status = fixture.sender.status()
        self.assertEqual(status["state"], C.STATE_IDLE)
        self.assertIsNone(status["session_id"])
        self.assertIn("stop", fixture.group.calls)
        self.assertEqual(status["last_error"], "device_lost: foreign_app")
        self.assertTrue(events_named(fixture.log.path, "device_lost"))
        self.assertFalse(os.path.exists(fixture.lock_path))
        # §3.3: never relaunch by ourselves — no command of any kind for the next 10 s
        tick(fixture, 40, 0.25)
        after = {name: fixture.link.calls.count(name) for name in ("load", "play_at", "seek")}
        self.assertEqual(after, before)

    def test_two_idle_reads_of_our_own_receiver_end_the_session(self):
        fixture = self.started()
        fixture.link.state = C.PLAYER_IDLE                     # the app went away quietly
        tick(fixture, 1)
        self.assertEqual(fixture.sender.status()["state"], C.STATE_PLAYING)
        tick(fixture, 1)
        self.assertEqual(fixture.sender.status()["state"], C.STATE_IDLE)

    def test_the_video_ending_ends_the_session(self):
        fixture = self.started()
        fixture.link.end_naturally()
        fixture.clock.advance(0.25)
        fixture.sender.tick()
        self.assertEqual(fixture.sender.status()["state"], C.STATE_ENDED)
        self.assertIn("stop", fixture.group.calls)
        self.assertEqual(self.events(fixture, "session_ended")[0]["reason"], "video_ended")
        tick(fixture, 9)
        self.assertEqual(fixture.sender.status()["state"], C.STATE_IDLE)

    def test_an_intent_is_mirrored_on_the_group(self):
        fixture = self.started()
        fixture.link.outbox.append({"type": C.MSG_INTENT, "seq": 9, "action": "pause"})
        fixture.clock.advance(0.25)
        fixture.sender.tick()
        self.assertEqual(fixture.sender.status()["state"], C.STATE_PAUSED)
        self.assertIn("pause", fixture.group.calls)
        fixture.link.outbox.append({"type": C.MSG_INTENT, "seq": 10, "action": "seek",
                                    "media": 42.0})
        tick(fixture, 1)
        self.assertAlmostEqual(fixture.group.media, 42.0)

    def test_a_resolve_failure_ends_cleanly_and_says_why(self):
        fixture = make_sender(self.root)

        def broken(url, **_kw):
            raise RuntimeError("private video")

        fixture.sender._resolver = broken
        fixture.sender.share(YOUTUBE)
        status = fixture.sender.status()
        self.assertEqual(status["state"], C.STATE_ENDED)
        self.assertIn("resolve_failed", status["last_error"])
        self.assertEqual(fixture.group.calls, [])
        self.assertFalse(os.path.exists(fixture.lock_path))
        self.assertFalse(os.path.exists(fixture.record_path))
        fixture.sender.tick()

    def test_a_stub_resolver_is_reported_as_such(self):
        fixture = make_sender(self.root)

        def stub(url, **_kw):
            raise NotImplementedError("task B")

        fixture.sender._resolver = stub
        fixture.sender.share(YOUTUBE)
        self.assertIn("resolver_unavailable", fixture.sender.status()["last_error"])

    def test_a_stub_controller_still_starts(self):
        """The service must run today: a Controller that raises is handled, not fatal."""
        fixture = make_sender(self.root, controller=_StubController())
        fixture.sender.share(YOUTUBE)
        self.assertEqual(fixture.sender.status()["state"], C.STATE_PLAYING)
        self.assertEqual(fixture.link.calls.count("play_at"), 1)
        self.assertTrue(events_named(fixture.log.path, "controller_unavailable"))
        tick(fixture, 20)                                     # stepping a stub must not raise
        self.assertEqual(fixture.sender.status()["state"], C.STATE_PLAYING)

    def test_group_lock_is_released_on_failure(self):
        fixture = make_sender(self.root)
        fixture.group.unsafe = True                           # connect raises UnsafeGroup
        fixture.sender.share(YOUTUBE)
        self.assertIn("group_failed", fixture.sender.status()["last_error"])
        self.assertFalse(os.path.exists(fixture.lock_path))
        self.assertEqual(fixture.proxy.sessions, {})
        self.assertIn("stop", fixture.link.calls)

    def test_a_quiet_refusal_keeps_a_running_session(self):
        fixture = self.started()
        session_id = fixture.sender.status()["session_id"]
        from sender.quiet import QuietWindow
        fixture.sender._quiet = QuietWindow("00:00", "00:01", "refuse")
        reply = fixture.sender.share("https://www.youtube.com/watch?v=K0uxjdcZWyk")
        self.assertEqual(reply["state"], C.STATE_QUIET_REFUSED)
        status = fixture.sender.status()
        self.assertEqual(status["session_id"], session_id)     # still the running one
        self.assertEqual(status["state"], C.STATE_PLAYING)
        self.assertEqual(fixture.group.calls.count("stop"), 0)


class _StubController:
    """Every hook raises NotImplementedError, like lib/v020/sync.py before task F."""

    def start(self, *_a, **_k):
        raise NotImplementedError("task F")

    def step(self, *_a, **_k):
        raise NotImplementedError("task F")

    def on_pause(self, *_a, **_k):
        raise NotImplementedError("task F")

    def on_seek(self, *_a, **_k):
        raise NotImplementedError("task F")

    def on_resume(self, *_a, **_k):
        raise NotImplementedError("task F")


if __name__ == "__main__":
    unittest.main()
