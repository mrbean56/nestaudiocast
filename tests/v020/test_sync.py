"""The control law, at the command level: every assertion of task F on `lib/v020/sync.py`.

No device, no socket, no sound — only samples and commands. The physics (measured noise, drift,
landings) lives in `sim_v020.py`; the acceptance numbers it produces are asserted by
`test_sync_sim.py`. This file asserts what the law *decides*.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.abspath(os.path.join(HERE, "..", "..", "lib"))
for p in (LIB, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from v020 import contracts as C                       # noqa: E402
from v020 import sync                                 # noqa: E402
from v020.contracts import (                          # noqa: E402
    PLAYER_IDLE, PLAYER_PAUSED, PLAYER_PLAYING, Command, GroupSample, Sample,
)

DT = 0.25


def vsample(t: float, media: float, state: str = PLAYER_PLAYING, rate: float = 1.0,
            offset_ms: float = 0.0) -> Sample:
    return Sample(rx_ms=t * 1000.0 + offset_ms, media=media, rate=rate, player_state=state)


def gsample(t: float, media: float, state: str = PLAYER_PLAYING) -> GroupSample:
    return GroupSample(wall=t, media=media, player_state=state)


def drive(ctl: sync.Controller, t0: float, t1: float, err_s, dt: float = DT,
          video_state: str = PLAYER_PLAYING, group_state: str = PLAYER_PLAYING,
          offset_ms: float = 0.0) -> list[Command]:
    """Feed ticks where the video sits ``err_s`` behind the group (before DELTA_V2)."""
    out: list[Command] = []
    t = t0
    while t <= t1 + 1e-9:
        e = err_s(t) if callable(err_s) else err_s
        cmds = ctl.step(t,
                        vsample(t, t - e - ctl.delta_v2, video_state, offset_ms=offset_ms),
                        gsample(t, t, group_state))
        out.extend(cmds)
        t = round(t + dt, 9)
    return out


def kinds(cmds: list[Command]) -> list[str]:
    return [c.kind for c in cmds]


class TestStartPlan(unittest.TestCase):
    """`start` plans the one start event; it performs nothing."""

    def setUp(self):
        self.ctl = sync.Controller(delta_v2=0.265)

    def test_single_start_event(self):
        cmds = self.ctl.start(now=10.0, video_ready=0.0, group_ready=0.0, lead_s=-0.0436)
        self.assertEqual(kinds(cmds), [sync.CMD_GROUP_PLAY, C.MSG_PLAY_AT])
        play, play_at = cmds
        land = 10.0 + self.ctl.START_GUARD_S
        # the group is told to play lead_s early: evidence/27 C lands it 43.6 ms early
        self.assertAlmostEqual(play.payload["at"], land + 0.0436, places=9)
        self.assertEqual(play.payload["media"], 0.0)
        # the receiver is self-timed (§3.1): same instant, one DELTA_V2 behind the group
        self.assertAlmostEqual(play_at.payload["rx_ms"], land * 1000.0, places=6)
        self.assertAlmostEqual(play_at.payload["media"], 0.0 - 0.265, places=9)

    def test_common_start_position_is_the_higher_one(self):
        cmds = self.ctl.start(now=0.0, video_ready=4.0, group_ready=5.0)
        self.assertEqual(kinds(cmds), [sync.CMD_GROUP_PLAY, C.MSG_PLAY_AT])
        self.assertEqual(cmds[0].payload["media"], 5.0)
        self.assertAlmostEqual(cmds[1].payload["media"], 5.0 - 0.265, places=9)

    def test_a_group_parked_elsewhere_is_parked_once_more(self):
        cmds = self.ctl.start(now=0.0, video_ready=5.0, group_ready=2.0)
        self.assertEqual(kinds(cmds),
                         [sync.CMD_GROUP_SEEK, sync.CMD_GROUP_PLAY, C.MSG_PLAY_AT])
        self.assertEqual(cmds[0].payload, {"media": 5.0, "pause": True, "at": 0.0})
        self.assertEqual(cmds[1].payload["media"], 5.0)

    def test_landing_undoes_a_previous_trim(self):
        # a trim commanded in a previous regime is still on the receiver: reset it first
        ctl = sync.Controller()
        ctl.start(0.0, 0.0, 0.0)
        ctl.on_seek(40.0, 0.0)
        ctl.phase = sync.PHASE_STEADY
        ctl._event_at = -100.0
        drive(ctl, 41.0, 60.0, err_s=0.050)          # 50 ms of error, engaged
        self.assertLess(abs(ctl._u_cmd), 1e-12 + ctl.rate_max)  # a trim was commanded
        self.assertNotEqual(ctl._rate_sent, 1.0)
        cmds = ctl.start(now=100.0, video_ready=0.0, group_ready=0.0)
        self.assertEqual(cmds[0].kind, C.MSG_RATE)
        self.assertEqual(cmds[0].payload["value"], 1.0)


class TestErrorSign(unittest.TestCase):
    """§2: e = p_group - p_video - DELTA_V2, positive = the kitchen is ahead."""

    def test_error_definition(self):
        ctl = sync.Controller(delta_v2=0.0)
        self.assertAlmostEqual(ctl.error(10.5, 10.0), 0.5)
        ctl = sync.Controller(delta_v2=0.25)
        self.assertAlmostEqual(ctl.error(10.25, 10.0), 0.0)
        self.assertAlmostEqual(ctl.error(10.0, 10.0), -0.25)
        # and it is the contract's own function, not a second definition of it
        self.assertAlmostEqual(ctl.error(3.0, 1.0), C.digital_error(3.0, 1.0, 0.25))

    def test_positive_error_speeds_the_video_up(self):
        """The sign of DELTA_V2 (measurement protocol §1), end to end.

        The kitchen being *ahead* (e > 0) must produce rate > 1: the video has to catch up.
        A sign error here would make the loop run away instead of converge, which is exactly
        what the protocol's linearity check exists to catch.
        """
        ctl = sync.Controller(delta_v2=0.265)
        ctl.start(0.0, 0.0, 0.0)
        ctl.on_seek(1.0, 0.0)                        # a fresh event: steady comes 30 s later
        ctl.phase = sync.PHASE_STEADY
        ctl._event_at = 1.0 - C.STEADY_AFTER_S - 1.0
        cmds = drive(ctl, 2.0, 20.0, err_s=0.100)    # the kitchen is 100 ms ahead
        rates = [c.payload["value"] for c in cmds if c.kind == C.MSG_RATE]
        self.assertTrue(rates, "a 100 ms error must produce a trim")
        self.assertGreater(rates[0], 1.0)

    def test_negative_error_slows_the_video_down(self):
        ctl = sync.Controller()
        ctl.start(0.0, 0.0, 0.0)
        ctl.phase = sync.PHASE_STEADY
        ctl._event_at = -100.0
        cmds = drive(ctl, 1.0, 20.0, err_s=-0.100)
        rates = [c.payload["value"] for c in cmds if c.kind == C.MSG_RATE]
        self.assertTrue(rates)
        self.assertLess(rates[0], 1.0)


class TestSteadyLaw(unittest.TestCase):
    """§1/§2: 5 ms dead band, |rate - 1| ≤ RATE_MAX, bounded slew, rate and nothing else."""

    def setUp(self):
        self.ctl = sync.Controller(delta_v2=0.0)
        self.ctl.start(0.0, 0.0, 0.0)
        self.ctl.phase = sync.PHASE_STEADY
        self.ctl._event_at = -100.0
        self.ctl._steady_since = -100.0

    def test_dead_band_commands_nothing(self):
        for err in (0.0, 0.004, -0.004, 0.0049):
            cmds = drive(self.ctl, 1.0, 120.0, err_s=err)
            self.assertEqual(cmds, [], f"a {err * 1000:.1f} ms error must not trim")

    def test_just_outside_the_band_trims(self):
        cmds = drive(self.ctl, 1.0, 20.0, err_s=0.008)
        rates = [c.payload["value"] for c in cmds]
        self.assertTrue(rates)
        self.assertLess(abs(rates[0] - 1.0), self.ctl.rate_max)

    def test_rate_is_clamped(self):
        for err, sign in ((2.0, 1), (-2.0, -1)):       # two seconds of error, either way
            ctl = sync.Controller(delta_v2=0.0)
            ctl.start(0.0, 0.0, 0.0)
            ctl.phase = sync.PHASE_STEADY
            ctl._event_at = ctl._steady_since = -100.0
            cmds = drive(ctl, 1.0, 120.0, err_s=err)
            self.assertTrue(cmds)
            for c in cmds:
                self.assertLessEqual(abs(c.payload["value"] - 1.0), ctl.rate_max + 1e-12)
            # and it saturates in the right direction instead of running away
            self.assertAlmostEqual(cmds[-1].payload["value"] - 1.0, sign * ctl.rate_max,
                                   places=9)

    def test_slew_is_bounded(self):
        cmds = drive(self.ctl, 1.0, 60.0, err_s=1.0)
        sent = [c.payload["value"] for c in cmds]
        self.assertGreater(len(sent), 1)
        limit = self.ctl.SLEW_PER_S * self.ctl.RATE_MIN_INTERVAL_S + 1e-12
        for prev, nxt in zip([1.0] + sent, sent):
            self.assertLessEqual(abs(nxt - prev), limit + self.ctl.RATE_STEP_MIN)

    def test_steady_state_only_emits_rate(self):
        seen = set()
        for err in (-0.5, -0.02, 0.0, 0.02, 0.5):
            self.setUp()
            seen |= set(kinds(drive(self.ctl, 1.0, 600.0, err_s=err)))
        self.assertEqual(seen, {C.MSG_RATE})

    def test_a_ten_minute_session_never_needs_a_reload(self):
        cmds = drive(self.ctl, 1.0, 601.0, err_s=0.030)
        self.assertTrue(all(k == C.MSG_RATE for k in kinds(cmds)))
        self.assertNotIn(C.MSG_LOAD, kinds(cmds))
        self.assertNotIn(C.MSG_SEEK, kinds(cmds))
        self.assertNotIn(C.MSG_PLAY_AT, kinds(cmds))

    def test_steady_assertion_fires_if_a_reload_ever_leaks_in(self):
        ctl = self.ctl
        ctl._steady_tick = lambda now: [Command(C.MSG_PLAY_AT, {"rx_ms": 0.0, "media": 0.0})]
        with self.assertRaises(AssertionError):
            drive(ctl, 1.0, 3.0, err_s=0.020)


class TestEvents(unittest.TestCase):
    """One re-landing per event, then steady again (§1)."""

    def setUp(self):
        self.ctl = sync.Controller(delta_v2=0.265)

    def test_seek_moves_both_sides(self):
        self.ctl.start(0.0, 0.0, 0.0)
        cmds = self.ctl.on_seek(100.0, 300.0)
        self.assertEqual(kinds(cmds), [sync.CMD_GROUP_SEEK, sync.CMD_GROUP_PLAY,
                                       C.MSG_PLAY_AT])
        self.assertEqual(cmds[0].payload["media"], 300.0 + 0.265)
        self.assertTrue(cmds[0].payload["pause"])
        self.assertEqual(cmds[1].payload["media"], 300.0 + 0.265)
        self.assertEqual(cmds[2].payload["media"], 300.0)
        # both sides are anchored at the same instant: group lead aside, same rx_ms/at
        land = 100.0 + self.ctl.START_GUARD_S
        self.assertAlmostEqual(cmds[1].payload["at"], land - self.ctl.lead_s, places=9)
        self.assertAlmostEqual(cmds[2].payload["rx_ms"], land * 1000.0, places=6)
        self.assertEqual(self.ctl.phase, sync.PHASE_LANDING)

    def test_one_relanding_then_nothing(self):
        self.ctl.start(0.0, 0.0, 0.0)
        land = self.ctl.START_GUARD_S
        play_at = 0
        t = land + self.ctl.SAMPLE_AFTER_S
        while t < land + 29.0:                     # stop before steady begins (§2: 30 s)
            cmds = self.ctl.step(t, vsample(t, t - self.ctl.delta_v2),
                                 gsample(t, t + 0.200))              # 200 ms of error
            play_at += sum(1 for c in cmds if c.kind == C.MSG_PLAY_AT)
            self.assertTrue(all(c.kind == C.MSG_PLAY_AT for c in cmds))
            t = round(t + DT, 9)
        self.assertEqual(play_at, 1, "exactly one re-landing is allowed per event")
        self.assertEqual(self.ctl.relandings, 1)

    def test_a_good_landing_spends_no_relanding(self):
        self.ctl.start(0.0, 0.0, 0.0)
        t = self.ctl.START_GUARD_S + self.ctl.SAMPLE_AFTER_S
        while t < 20.0:
            self.ctl.step(t, vsample(t, t - self.ctl.delta_v2),
                          gsample(t, t + 0.002))                           # 2 ms: fine
            t = round(t + DT, 9)
        self.assertEqual(self.ctl.relandings, 0)
        self.assertEqual(self.ctl.relanding_refused, 1)
        self.assertNotEqual(self.ctl.phase, sync.PHASE_LANDING)

    def test_pause_then_resume_places_the_video_on_the_group(self):
        self.ctl.start(0.0, 0.0, 0.0)
        cmds = self.ctl.on_pause(100.0)
        self.assertEqual(kinds(cmds), [sync.CMD_GROUP_PAUSE, C.MSG_PAUSE])
        self.assertEqual(self.ctl.step(101.0, vsample(101.0, 100.0, PLAYER_PAUSED),
                                       gsample(101.0, 100.0, PLAYER_PAUSED)), [])
        cmds = self.ctl.on_resume(140.0, 120.0)
        self.assertEqual(kinds(cmds), [sync.CMD_GROUP_PLAY, C.MSG_PLAY_AT])
        land = 140.0 + self.ctl.START_GUARD_S
        self.assertEqual(cmds[0].payload["media"], 120.0)
        self.assertAlmostEqual(cmds[0].payload["at"], land - self.ctl.lead_s, places=9)
        self.assertAlmostEqual(cmds[1].payload["media"], 120.0 - 0.265, places=9)
        self.assertAlmostEqual(cmds[1].payload["rx_ms"], land * 1000.0, places=6)

    def test_end_stops_both_and_goes_quiet(self):
        self.ctl.start(0.0, 0.0, 0.0)
        cmds = self.ctl.on_end(200.0)
        self.assertEqual(kinds(cmds), [sync.CMD_GROUP_STOP, C.MSG_IDLE])
        self.assertEqual(self.ctl.phase, sync.PHASE_ENDED)
        self.assertEqual(self.ctl.step(200.25, vsample(200.25, 200.0, PLAYER_IDLE),
                                       gsample(200.25, 200.0, PLAYER_IDLE)), [])

    def test_no_command_while_paused(self):
        self.ctl.start(0.0, 0.0, 0.0)
        self.ctl.on_pause(100.0)
        self.assertEqual(drive(self.ctl, 101.0, 160.0, err_s=0.500,
                               video_state=PLAYER_PAUSED, group_state=PLAYER_PAUSED), [])


class TestObservation(unittest.TestCase):
    """steady(), window_median_ms() and the two-read hysteresis (§2, A36)."""

    def setUp(self):
        self.ctl = sync.Controller(delta_v2=0.0)
        self.ctl.start(0.0, 0.0, 0.0)
        land = self.ctl.START_GUARD_S
        self.ctl._landing_at = land
        self.ctl.phase = sync.PHASE_SETTLING
        self.ctl._event_at = land

    def test_steady_needs_thirty_seconds(self):
        drive(self.ctl, 1.0, 25.0, err_s=0.0)
        self.assertFalse(self.ctl.steady(25.0))
        drive(self.ctl, 25.25, 45.0, err_s=0.0)
        self.assertEqual(self.ctl.phase, sync.PHASE_STEADY)
        drive(self.ctl, 45.25, 95.0, err_s=0.002)
        self.assertTrue(self.ctl.steady(95.0))

    def test_steady_is_false_while_a_trim_is_fresh(self):
        drive(self.ctl, 1.0, 100.0, err_s=0.0)
        drive(self.ctl, 100.25, 110.0, err_s=0.050)       # a trim goes out
        self.assertGreater(self.ctl._rate_at, 100.0)
        self.assertFalse(self.ctl.steady(self.ctl._rate_at + 1.0),
                         "a trim 1 s ago is not steady (§2)")
        self.assertTrue(self.ctl.steady(self.ctl._rate_at + 3.0))

    def test_steady_is_false_when_a_side_is_not_playing(self):
        drive(self.ctl, 1.0, 60.0, err_s=0.0)
        drive(self.ctl, 60.25, 62.0, err_s=0.0, group_state=PLAYER_PAUSED)
        self.assertFalse(self.ctl.steady(62.0))

    def test_one_stale_non_playing_read_is_ignored(self):
        drive(self.ctl, 1.0, 60.0, err_s=0.0)
        t = 60.25
        self.ctl.step(t, vsample(t, t), gsample(t, t, PLAYER_PAUSED))    # one stale read
        drive(self.ctl, t + DT, t + 5.0, err_s=0.0)
        self.assertTrue(self.ctl.steady(t + 5.0))
        self.assertEqual(self.ctl._np["group"], 0)

    def test_two_non_playing_reads_are_a_stop(self):
        drive(self.ctl, 1.0, 60.0, err_s=0.0)
        t = 60.25
        for _ in range(2):
            self.ctl.step(t, vsample(t, t), gsample(t, t, PLAYER_PAUSED))
            t = round(t + DT, 9)
        self.assertEqual(self.ctl._np["group"], 2)
        self.assertFalse(self.ctl.steady(61.0))

    def test_window_median_is_the_last_five_seconds(self):
        drive(self.ctl, 1.0, 60.0, err_s=0.0)
        self.ctl.phase = sync.PHASE_STEADY
        self.ctl._event_at = -100.0
        self.ctl._steady_window.clear()
        self.ctl._rate_at = -1e9
        for i in range(20):                              # 5 s of 4 Hz samples at +12 ms
            now = 100.0 - 4.75 + i * DT
            self.ctl.step(now, vsample(now, now + 0.012), gsample(now, now + 0.024))
        med = self.ctl.window_median_ms()
        self.assertIsNotNone(med)
        self.assertAlmostEqual(med, 12.0, delta=1.5)

    def test_a_stale_outlier_read_is_rejected(self):
        drive(self.ctl, 1.0, 60.0, err_s=0.0)
        before = self.ctl.estimated_error()
        t = 61.0
        for stale in (0.150, -0.156):
            self.ctl.step(t, vsample(t, t + stale), gsample(t, t))
            t = round(t + DT, 9)
        self.assertGreaterEqual(self.ctl.rejected, 2)
        self.assertAlmostEqual(self.ctl.estimated_error(), before, places=3)

    def test_snapshot_carries_the_status_fields(self):
        drive(self.ctl, 1.0, 60.0, err_s=0.0)
        snap = self.ctl.snapshot()
        for key in ("phase", "digital_error_ms", "rate", "steady", "commands"):
            self.assertIn(key, snap)
        self.assertLessEqual(abs(snap["rate"] - 1.0), self.ctl.rate_max)


if __name__ == "__main__":
    unittest.main()
