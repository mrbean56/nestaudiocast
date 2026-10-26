"""The gate's digital-error instrument (measure/analyze_v020.py), proved offline.

No socket, no device, no speaker: every input here is synthetic, except the two archived v0.1
files, which are read as files. The tests pin the *decisions* the hardware gate will rely on:
a steady window over ±20 ms fails the run, a load/seek inside steady state fails it, the v0.1
log shapes are mapped explicitly, and a landing report cannot be mistaken for a series.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LIB = os.path.join(ROOT, "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

from v020 import contracts as C  # noqa: E402


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = _load(os.path.join(ROOT, "measure", "analyze_v020.py"), "analyze_v020")


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def contract_session(t0=1000.0, seconds=240.0, bias_ms=0.0, drift_ms_per_10min=0.0,
                     noise_ms=3.0, reload_at=None, seed=11):
    """A v0.2 session log: share, load, play_at, first_frame, then 4 Hz steady samples."""
    rng = np.random.default_rng(seed)
    rows = [{"wall": t0, "type": "share"},
            {"wall": t0 + 0.5, "type": "load", "side": "group", "media": 0.0},
            {"wall": t0 + 0.8, "type": "play_at", "side": "video", "media": 0.0},
            {"wall": t0 + 2.0, "type": "first_frame"}]
    t = 2.0
    while t < seconds:
        if reload_at is not None and abs(t - reload_at) < 0.2:
            rows.append({"wall": t0 + t, "type": "load", "side": "group", "media": t})
        e = bias_ms + drift_ms_per_10min * t / 600.0 + float(rng.normal(0, noise_ms))
        g = 100.0 + t
        rows.append({"wall": t0 + t, "type": "sample", "group_media": round(g, 4),
                     "video_media": round(g - e / 1000.0 - 0.265, 4), "delta_v2": 0.265,
                     "group_state": "playing", "video_state": "playing", "rate": 1.0})
        t += 0.25
    return rows


class TestContractAlignment(unittest.TestCase):
    def test_constants_come_from_the_contracts(self):
        self.assertEqual(A.STEADY_AFTER_S, C.STEADY_AFTER_S)
        self.assertEqual(A.WINDOW_S, C.STEADY_WINDOW_S)
        self.assertEqual(A.TOL_MS, C.DIGITAL_TOL_MS)

    def test_digital_error_sign(self):
        # contracts §2: e = p_group - p_video - DELTA_V2, positive = the kitchen is ahead
        rep = {"wall": 0.0, "type": "sample", "group_media": 10.5, "video_media": 10.0,
               "delta_v2": 0.0, "group_state": "playing", "video_state": "playing"}
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "s.jsonl")
            write_jsonl(p, [rep])
            samples, _events, _notes = A.map_events(A.read_rows(p)[1], "v0.2-contract")
            samples[0]["delta_cli"] = 0.0
            A._steady_flags(samples, [])
            self.assertAlmostEqual(samples[0]["e_ms"], 500.0)


class TestSteadyWindows(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = self.td.name

    def tearDown(self):
        self.td.cleanup()

    def _path(self, rows, name="log.jsonl"):
        p = os.path.join(self.dir, name)
        write_jsonl(p, rows)
        return p

    def test_clean_session_passes(self):
        rep, code = A.analyze_file(self._path(contract_session(bias_ms=2.0)))
        self.assertEqual(code, A.EXIT_OK, rep["verdict"])
        self.assertEqual(rep["format"], "v0.2-contract")
        self.assertGreater(rep["counts"]["windows"], 30)
        self.assertLessEqual(max(abs(w["median_ms"]) for w in rep["windows"]), 20.0)
        # p95 and max are reported next to the median, never instead of it
        for w in rep["windows"]:
            self.assertGreaterEqual(w["max_abs_ms"], w["p95_abs_ms"])
            self.assertGreaterEqual(w["p95_abs_ms"], 0.0)

    def test_window_over_tolerance_fails_with_exit_1(self):
        rep, code = A.analyze_file(self._path(contract_session(bias_ms=35.0, noise_ms=1.5)))
        self.assertEqual(code, A.EXIT_TOL, rep["verdict"])
        self.assertTrue(rep["violations"]["tolerance"])
        worst = max(abs(w["median_ms"]) for w in rep["windows"])
        self.assertGreater(worst, 20.0)

    def test_hidden_reload_inside_steady_fails_with_exit_3(self):
        p = self._path(contract_session(seconds=300, reload_at=150.0))
        rep, code = A.analyze_file(p)
        self.assertEqual(code, A.EXIT_TRANSPORT, rep["verdict"])
        v = rep["violations"]["forbidden_transport"]
        self.assertEqual(len(v), 1)
        self.assertGreaterEqual(v[0]["since_previous_transport_s"], A.STEADY_AFTER_S)
        self.assertAlmostEqual(v[0]["t"], 150.0, delta=0.5)

    def test_a_reload_at_the_start_is_not_a_violation(self):
        rows = contract_session(seconds=120)
        rows.insert(1, {"wall": 1000.3, "type": "seek", "side": "group", "media": 1.0})
        rep, code = A.analyze_file(self._path(rows))
        self.assertEqual(rep["violations"]["forbidden_transport"], [])
        self.assertIn(code, (A.EXIT_OK, A.EXIT_TOL, A.EXIT_UNDECIDED))

    def test_no_steady_window_is_undecided_not_a_pass(self):
        rep, code = A.analyze_file(self._path(contract_session(seconds=35)))
        self.assertEqual(code, A.EXIT_UNDECIDED, rep["verdict"])
        self.assertEqual(rep["counts"]["windows"], 0)

    def test_non_playing_samples_break_steadiness(self):
        rows = contract_session(seconds=120)
        for r in rows:
            if r["type"] == "sample" and 60.0 < r["wall"] - 1000.0 < 70.0:
                r["group_state"] = "paused"
        rep, _ = A.analyze_file(self._path(rows))
        for w in rep["windows"]:
            self.assertFalse(w["t_start"] < 60.0 + 2.0 and w["t_end"] > 60.0,
                             "a window straddles the pause")

    def test_rate_command_holds_steadiness_for_two_seconds(self):
        rows = contract_session(seconds=180)
        for r in rows:
            if r["type"] == "sample" and abs(r["wall"] - 1100.0) < 0.02:
                idx = rows.index(r)
        rows.insert(idx, {"wall": 1100.0, "type": "rate", "side": "video", "value": 1.001})
        rep, _ = A.analyze_file(self._path(rows))
        self.assertTrue(any(w["t_start"] > 100.0 for w in rep["windows"]))


class TestDrift(unittest.TestCase):
    def test_drift_is_the_slope_of_the_five_second_medians(self):
        rows = contract_session(seconds=600, drift_ms_per_10min=40.0, noise_ms=2.0)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "d.jsonl")
            write_jsonl(p, rows)
            rep, _ = A.analyze_file(p)
        longest = rep["drift"]["longest"]
        self.assertIsNotNone(longest)
        self.assertAlmostEqual(longest["drift_ms_per_10min"], 40.0, delta=6.0)
        self.assertLess(longest["sigma_ms_per_10min"], 10.0)
        self.assertTrue(longest["trend_counts"])

    def test_flat_session_reports_no_counting_trend(self):
        rows = contract_session(seconds=600, noise_ms=4.0)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "f.jsonl")
            write_jsonl(p, rows)
            rep, _ = A.analyze_file(p)
        self.assertFalse(rep["drift"]["longest"]["trend_counts"])

    def test_steadiest_window_is_the_least_dispersed(self):
        rows = contract_session(seconds=300, noise_ms=6.0)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "s.jsonl")
            write_jsonl(p, rows)
            rep, _ = A.analyze_file(p)
        full = [w for w in rep["windows"] if w["full"]]
        self.assertEqual(rep["steadiest_window"]["stdev_ms"], min(w["stdev_ms"] for w in full))


class TestFormats(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = self.td.name

    def tearDown(self):
        self.td.cleanup()

    def test_v01_relay_shape_mapped_explicitly(self):
        rows = [{"wall": 100.0, "ev": "content_changed", "from": None, "to": "VID"},
                {"wall": 101.0, "ev": "content_ready", "video": "VID"},
                {"wall": 110.0, "ev": "sink_started", "mode": "load", "pos": 10.0}]
        t = 112.0
        while t < 260.0:
            rows.append({"wall": t, "ev": "sample", "tv_t": t - 100.0, "grp_t": t - 100.0,
                         "err_ms": 4.0, "tv_state": "PLAYING", "grp_state": "PLAYING",
                         "action": "in_sync", "rate": 1})
            t += 0.25
        rows.append({"wall": 260.0, "ev": "sample", "tv_t": 160.0, "grp_t": 160.0,
                     "err_ms": 120.0, "tv_state": "PLAYING", "grp_state": "PLAYING",
                     "action": "realign lead_adj=+0.01", "rate": 1})
        rows.append({"wall": 260.25, "ev": "sample", "tv_t": 160.25, "grp_t": 160.25,
                     "err_ms": 4.0, "tv_state": "PLAYING", "grp_state": "PLAYING",
                     "action": "in_sync", "rate": 1})
        p = os.path.join(self.dir, "v01.jsonl")
        write_jsonl(p, rows)
        rep, code = A.analyze_file(p)
        self.assertEqual(rep["format"], "v0.1-relay")
        self.assertTrue(rep["delta_source"].startswith("in-log"))
        self.assertGreater(rep["counts"]["steady_samples"], 400)
        self.assertLess(rep["counts"]["steady_samples"],
                        len([r for r in rows if r.get("ev") == "sample"]))
        # the realign sample is both a reading and a seek event: it is inside steady state
        self.assertEqual(code, A.EXIT_TRANSPORT, rep["verdict"])
        self.assertEqual(rep["violations"]["forbidden_transport"][0]["kind"],
                         "sample.action=realign lead_adj=+0.01")

    def test_landing_report_is_incomplete_not_a_pass(self):
        doc = {"when": "2026-10-25 17:00", "land_tol_ms": 30,
               "cases": [{"case": "initial play", "reloads": None, "timeout": True},
                         {"case": "seek 300s", "reloads": None, "timeout": True}],
               "mean_reloads": None}
        p = os.path.join(self.dir, "39.json")
        json.dump(doc, open(p, "w"))
        rep, code = A.analyze_file(p)
        self.assertEqual(rep["format"], "v0.1-landing")
        self.assertEqual(code, A.EXIT_UNDECIDED)
        self.assertEqual(len(rep["windows"]), 2)

    def test_landing_report_with_a_breaching_median_fails(self):
        doc = {"land_tol_ms": 30, "cases": [{"case": "initial play", "err_median": 40.2,
                                             "err_max": 47.9}]}
        p = os.path.join(self.dir, "38.json")
        json.dump(doc, open(p, "w"))
        rep, code = A.analyze_file(p, tol_ms=20.0)
        self.assertEqual(code, A.EXIT_TOL)
        self.assertFalse(rep["windows"][0]["within_tol"])

    def test_video_log_supplies_the_video_position(self):
        rows = contract_session(seconds=180, bias_ms=3.0)
        gpath = os.path.join(self.dir, "g.jsonl")
        vpath = os.path.join(self.dir, "v.jsonl")
        with open(gpath, "w") as fh:
            for r in rows:
                if r.get("type") == "sample":
                    r = {k: v for k, v in r.items() if k != "video_media"}
                fh.write(json.dumps(r) + "\n")
        with open(vpath, "w") as fh:
            for r in rows:
                if r.get("type") == "sample":
                    fh.write(json.dumps({"rx_ms": r["wall"] * 1000.0,
                                         "media": r["video_media"],
                                         "player_state": "playing"}) + "\n")
        rep, code = A.analyze_file(gpath, video_log=vpath)
        self.assertEqual(code, A.EXIT_OK, rep["verdict"])
        self.assertGreater(rep["counts"]["video_paired"], 100)

    def test_action_log_adds_transport_commands(self):
        rows = contract_session(seconds=120, bias_ms=1.0)
        p = os.path.join(self.dir, "a.jsonl")
        write_jsonl(p, rows)
        actions = os.path.join(self.dir, "actions.jsonl")
        write_jsonl(actions, [{"wall": 1000.0, "action": "share"},
                              {"wall": 1055.0, "action": "pause"}])
        rep, _ = A.analyze_file(p, action_log=actions)
        self.assertEqual(rep["counts"]["actions_merged"], 2)
        kinds = {l["kind"] for l in rep["latencies"]}
        self.assertIn("action:share", kinds)
        self.assertIn("action:pause", kinds)

    def test_non_json_input_is_rejected(self):
        p = os.path.join(self.dir, "bad.jsonl")
        open(p, "w").write("nope\n")
        with self.assertRaises(ValueError):
            A.read_rows(p)

    def test_empty_input_is_rejected(self):
        p = os.path.join(self.dir, "empty.jsonl")
        open(p, "w").write("")
        with self.assertRaises(ValueError):
            A.read_rows(p)

    def test_unknown_event_shape_is_rejected(self):
        p = os.path.join(self.dir, "odd.jsonl")
        write_jsonl(p, [{"foo": 1}])
        with self.assertRaises(ValueError):
            A.read_rows(p)


class TestCli(unittest.TestCase):
    def test_exit_codes_through_main(self):
        rows = contract_session(bias_ms=2.0)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "log.jsonl")
            out = os.path.join(td, "out.json")
            write_jsonl(p, rows)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = A.main([p, "--json", out])
            self.assertEqual(code, A.EXIT_OK)
            self.assertTrue(os.path.exists(out))
            rep = json.load(open(out))
            self.assertEqual(rep["tool"], "analyze_v020")
            self.assertEqual(rep["exit_code"], A.EXIT_OK)

    def test_missing_input_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(A.main(["/nonexistent/session.jsonl"]), A.EXIT_INPUT)

    def test_schema_and_help_are_available(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(A.main(["--print-schema"]), 0)
        self.assertIn("precondition", A.PRECONDITION.lower())


class TestRedaction(unittest.TestCase):
    def test_googlevideo_url_is_redacted(self):
        url = "https://rr3---sn-abc.googlevideo.com/videoplayback?expire=1&sig=SECRET"
        out = A.redact(url)
        self.assertTrue(out.startswith("https://rr3---sn-abc.googlevideo.com/"))
        self.assertNotIn("SECRET", out)


if __name__ == "__main__":
    unittest.main()
