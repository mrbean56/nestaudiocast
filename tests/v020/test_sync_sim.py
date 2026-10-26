"""Task F's 'done when', machine-checked against `sim_v020.py`.

The simulation carries the measured noise (evidence/17, 19, 27); this file is the gate that says
whether the control law meets its acceptance criteria on it. It also pins the numbers down: if a
change to `lib/v020/sync.py` makes the law worse, one of these tests fails with the new number
instead of the lab note going quietly stale.
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

import sim_v020 as S  # noqa: E402

from v020 import contracts as C  # noqa: E402
from v020 import sync  # noqa: E402


def case(name: str, **overrides) -> dict:
    opt = S.options_for(name, **overrides)
    return S.run_case(name, opt)


class TestNominalCases(unittest.TestCase):
    """Every nominal case: p95 ≤ 10 ms, no reload while steady, convergence ≤ 10 s."""

    @classmethod
    def setUpClass(cls):
        cls.results = {
            name: case(name)
            for name in ("cold_start", "drift_only", "seek", "pause_resume", "stale_reads", "wrong_lead")
        }

    def test_each_case_passes_its_criteria(self):
        for name, res in self.results.items():
            with self.subTest(case=name):
                self.assertEqual(res["verdict"]["verdict"], "PASS", res["verdict"]["checks"])

    def test_steady_p95_under_ten_ms(self):
        for name, res in self.results.items():
            with self.subTest(case=name):
                p95 = res["window_medians_ms"]["p95"]
                self.assertIsNotNone(p95)
                self.assertLessEqual(p95, S.TARGET_P95_MS)

    def test_no_load_or_seek_or_relanding_once_steady(self):
        for name, res in self.results.items():
            with self.subTest(case=name):
                after = res["after_steady"]
                self.assertNotIn(C.MSG_LOAD, after)
                self.assertNotIn(C.MSG_SEEK, after)
                self.assertNotIn(sync.CMD_GROUP_SEEK, after)
                self.assertNotIn(C.MSG_PLAY_AT, after, "a second landing after steady")
                self.assertEqual(set(after), {C.MSG_RATE})

    def test_convergence_within_ten_seconds(self):
        for name, res in self.results.items():
            with self.subTest(case=name):
                self.assertIsNotNone(res["convergence_max_s"], res["convergence_s"])
                self.assertLessEqual(res["convergence_max_s"], S.CONVERGE_S)

    def test_ten_minute_session_needs_no_transport_command(self):
        res = self.results["cold_start"]
        self.assertGreaterEqual(res["steady_samples"], 2000)  # 10 min at 4 Hz
        # the whole session: the start event, its single re-landing, then trims only
        self.assertLessEqual(set(res["commands"]), {sync.CMD_GROUP_PLAY, C.MSG_PLAY_AT, C.MSG_RATE})
        self.assertEqual(res["commands"].get(sync.CMD_GROUP_PLAY), 1)
        self.assertLessEqual(res["commands"].get(C.MSG_PLAY_AT, 0), 2)

    def test_every_read_the_law_saw_was_steady_enough_to_judge(self):
        res = self.results["cold_start"]
        self.assertGreaterEqual(res["window_medians_ms"]["n"], 100)  # 600 s / 5 s ≈ 120
        self.assertLessEqual(res["window_medians_ms"]["max_abs"], 15.0)


class TestDriftOnly(unittest.TestCase):
    """The drift criterion has its own case: a trim above RATE_MAX/4 is not allowed."""

    @classmethod
    def setUpClass(cls):
        cls.res = case("drift_only")

    def test_never_trims_above_a_quarter_of_the_budget(self):
        limit = self.res["rate"]["rate_max"] / 4.0
        self.assertLessEqual(self.res["rate"]["max_abs_u_settled"], limit + 1e-12)
        self.assertLessEqual(self.res["rate"]["max_abs_u"], limit + 1e-12)

    def test_drift_is_really_in_the_run(self):
        # −12.4 ms/10 min over a 15 min session: ~19 ms of drift, and it must not be visible
        self.assertLessEqual(self.res["window_medians_ms"]["max_abs"], S.TARGET_P95_MS)
        self.assertGreaterEqual(self.res["window_medians_ms"]["n"], 170)  # 900 s / 5 s


class TestLandingPolicy(unittest.TestCase):
    """Park-and-release vs LOAD, 40 draws each: the reason v0.2 may never LOAD to start."""

    @classmethod
    def setUpClass(cls):
        cls.park = case("landing_park")
        cls.load = case("landing_load")

    def test_park_lands_inside_the_perceptual_budget(self):
        self.assertLessEqual(self.park["landing_ms"]["p95"], 100.0)
        self.assertEqual(self.park["landing_ms"]["over_150ms_pct"], 0.0)

    def test_load_blows_the_perceptual_budget(self):
        self.assertGreater(self.load["landing_ms"]["p50"], 1.5 * S.PERCEPTUAL_MS)
        self.assertGreater(self.load["landing_ms"]["p95"], 2 * S.PERCEPTUAL_MS)
        self.assertGreater(self.load["landing_ms"]["over_150ms_pct"], 40.0)

    def test_the_single_relanding_absorbs_either_landing(self):
        for name, res in (("park", self.park), ("load", self.load)):
            with self.subTest(landing=name):
                self.assertLessEqual(res["after_relanding_ms"]["p95"], S.TARGET_P95_MS)


class TestFailureIsVisible(unittest.TestCase):
    """A case that cannot meet the criteria must say so, with its number."""

    def test_pessimistic_case_fails_with_its_number(self):
        res = case("pessimistic")
        self.assertFalse(res["verdict"]["ok"])
        self.assertEqual(res["verdict"]["verdict"], "FAIL (expected)")
        # rate gain 0.5 + 40 ms start dispersion: the gap stays far above 10 ms for minutes
        self.assertGreater(res["window_medians_ms"]["p95"], 3 * S.TARGET_P95_MS)
        self.assertGreater(res["convergence_max_s"], S.CONVERGE_S)
        failed = {k for k, ok in res["verdict"]["checks"].items() if not ok}
        self.assertIn("steady_p95_le_10ms", failed)
        self.assertIn("convergence_le_10s", failed)
        # and it never cheated: no reload while steady
        self.assertNotIn(C.MSG_PLAY_AT, res["after_steady"])

    def test_clock_residual_costs_what_it_costs(self):
        # the loop can only be as good as its clock: a 12 ms residual is a 12 ms floor
        res = case("clock_residual")
        self.assertLessEqual(abs(res["window_medians_ms"]["mean_signed"]), 20.0)
        self.assertGreater(res["window_medians_ms"]["p95"], S.TARGET_P95_MS)
        # while what the loop *sees* is back to ~0: it is not unstable, it is misinformed
        self.assertLessEqual(res["window_medians_estimate_ms"]["p95"], S.TARGET_P95_MS)

    def test_a_dead_rate_actuator_is_reported_not_hidden(self):
        res = case("rate_dead")
        # –12.4 ms/10 min with no working actuator: inside the 10 ms target by a hair at
        # 10 min, and only because the landing is right — this is the margin task C reports on
        self.assertGreater(res["rate"]["n"], 0, "the law still asks, the device does nothing")
        self.assertGreater(res["window_medians_ms"]["max_abs"], 5.0)
        self.assertGreater(abs(res["window_medians_ms"]["mean_signed"]), 2.0)


class TestStaleReadsDoNotCauseAReload(unittest.TestCase):
    """A36: one stale PAUSED read must never cost a restart."""

    def test_no_group_command_from_the_loop(self):
        res = case("stale_reads")
        # exactly one group_play, the start event's; nothing else ever touches the group
        self.assertEqual(res["commands"].get(sync.CMD_GROUP_PLAY), 1)
        for kind in (
            sync.CMD_GROUP_PAUSE,
            sync.CMD_GROUP_SEEK,
            sync.CMD_GROUP_STOP,
            C.MSG_PAUSE,
            C.MSG_SEEK,
            C.MSG_LOAD,
        ):
            self.assertNotIn(kind, res["commands"], kind)
        self.assertEqual(set(res["after_steady"]), {C.MSG_RATE})
        self.assertEqual(res["verdict"]["verdict"], "PASS")


class TestDeterminism(unittest.TestCase):
    """The evidence file must be regenerable to the same numbers."""

    def test_same_seed_same_numbers(self):
        a = case("seek", repeats=1)
        b = case("seek", repeats=1)
        for key in ("window_medians_ms", "rate", "convergence_s", "after_steady"):
            self.assertEqual(a[key], b[key])

    def test_a_different_seed_moves_the_numbers_but_not_the_verdict(self):
        base = case("cold_start")
        other = case("cold_start", seed=7)
        self.assertEqual(base["verdict"]["verdict"], other["verdict"]["verdict"])
        self.assertEqual(set(base["after_steady"]), set(other["after_steady"]))


if __name__ == "__main__":
    unittest.main()


class TestImperfectVideoSide(unittest.TestCase):
    """Review 2026-10-26: the cases above assume a perfect video side. These do not."""

    def test_start_spread_and_group_scatter_are_absorbed(self):
        for name in ("video_start_40", "video_start_80", "group_landing_80"):
            with self.subTest(case=name):
                res = case(name)
                self.assertEqual(res["verdict"]["verdict"], "PASS", res["verdict"]["checks"])

    def test_read_noise_is_an_open_finding_not_a_hidden_one(self):
        for name in ("video_noise_10", "video_realistic"):
            with self.subTest(case=name):
                res = case(name)
                self.assertEqual(res["verdict"]["verdict"], "FAIL (expected)")
                failed = {k for k, ok in res["verdict"]["checks"].items() if not ok}
                # it fails on convergence only: the steady error holds and nothing reloads
                self.assertEqual(failed, {"convergence_le_10s"})
                self.assertLessEqual(res["window_medians_ms"]["p95"], S.TARGET_P95_MS)
