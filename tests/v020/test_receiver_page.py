"""Static checks on receiver/receiver.html — the honesty test for the page.

The receiver page only proves anything when it runs on a Chromecast, and tonight it could not
(the device does not list our unpublished app: evidence/41-preflight-unavailable.json). These
checks are what keeps the page from drifting away from docs/v020-contracts.md §3 while no device
is available: the namespace, every message type, the clock convention, and the promise that the
page loads nothing from the network but the CAF framework.

They are deliberately static: no browser, no dependency, no network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
LIB = os.path.join(ROOT, "lib")
LOADER_PATH = os.path.join(ROOT, "receiver", "receiver.html")  # the frozen URL: a stable loader
PAGE_PATH = os.path.join(ROOT, "receiver", "receiver.js")  # the page itself (versioned)
SERVE_PATH = os.path.join(ROOT, "receiver", "serve.py")

if LIB not in sys.path:
    sys.path.insert(0, LIB)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from v020 import contracts as C  # noqa: E402

CAF_LOADER = "https://www.gstatic.com/cast/sdk/libs/caf_receiver/v3/cast_receiver_framework.js"


def page() -> str:
    with open(PAGE_PATH, encoding="utf-8") as fh:
        return fh.read()


def markup() -> str:
    with open(LOADER_PATH, encoding="utf-8") as fh:
        return fh.read()


class TestPageExists(unittest.TestCase):
    def test_page_is_served_at_the_frozen_path(self):
        # the URL in the Cast console cannot change without a 15 min propagation
        self.assertTrue(os.path.isfile(LOADER_PATH), "receiver/receiver.html is missing")
        self.assertTrue(os.path.isfile(PAGE_PATH), "receiver/receiver.js is missing")
        self.assertEqual(C.PATH_RECEIVER, "/receiver.html")

    def test_the_frozen_page_is_a_loader_that_can_change_itself(self):
        """The device caches receiver.html; only a *stable* loader keeps updates reboot-free."""
        with open(LOADER_PATH, encoding="utf-8") as fh:
            src = fh.read()
        # paths that change with the build: the device caches by path and ignores ?queries
        self.assertIn("/b/' + Date.now() + '/build.txt", src)
        self.assertIn("/r/' + encodeURIComponent(build) + '/receiver.js", src)
        self.assertIn(CAF_LOADER, src)  # the SDK must still be loaded by the page
        self.assertLess(len(src), 8000, "the loader must stay small and stable")

    def test_dev_server_serves_that_exact_path(self):
        with open(SERVE_PATH, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('"/receiver.html"', src)
        self.assertIn('"/media/"', src)
        self.assertIn('"/healthz"', src)
        # it must never bind anything but the LAN address it is given, and never :8099
        self.assertIn("--bind", src)
        self.assertIn("192.168.2.", src)


class TestMseReader(unittest.TestCase):
    """The page reads the sidx itself (lab note C6); the reader has its own harness."""

    MSE = os.path.join(ROOT, "receiver", "mse.js")
    SELFTEST = os.path.join(ROOT, "receiver", "mse_selftest.js")

    def test_reader_exists_and_is_wired(self):
        self.assertTrue(os.path.isfile(self.MSE))
        with open(self.MSE, encoding="utf-8") as fh:
            reader = fh.read()
        self.assertIn("parseSidx", reader)
        self.assertIn("addSourceBuffer", reader)
        self.assertIn("appendBuffer", reader)
        with open(PAGE_PATH, encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn("NacMse", page)
        self.assertIn("mpdTracks", page)
        self.assertIn("mse_failed", page)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_reader_parses_real_sidx_bytes(self):
        proc = subprocess.run(
            [shutil.which("node"), self.SELFTEST], capture_output=True, text=True, timeout=180
        )
        self.assertEqual(proc.returncode, 0, proc.stdout[-800:] + proc.stderr[-400:])
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["failed"], 0, [c for c in doc["results"] if not c["ok"]])


class TestProtocolSurface(unittest.TestCase):
    def test_namespace(self):
        src = page()
        self.assertIn(C.NS_RECEIVER, src)
        # the namespace is a named constant, not a literal scattered through the page
        self.assertRegex(src, r"var NS = ['\"]%s['\"]" % re.escape(C.NS_RECEIVER))

    def test_every_message_type_is_handled_or_sent(self):
        src = page()
        for msg_type in C.CONTROLLER_MESSAGES + C.RECEIVER_MESSAGES:
            self.assertRegex(
                src,
                r"""['"]%s['"]""" % re.escape(msg_type),
                "message type %r does not appear in receiver.html" % msg_type,
            )

    def test_controller_messages_are_all_dispatched(self):
        src = page()
        for msg_type in C.CONTROLLER_MESSAGES:
            self.assertRegex(
                src,
                r"case\s+['\"]%s['\"]\s*:" % re.escape(msg_type),
                "no case for controller message %r" % msg_type,
            )

    def test_every_player_state_can_be_reported(self):
        src = page()
        for state in C.PLAYER_STATES:
            self.assertIn(
                "'%s'" % state if state != "idle" else "'idle'",
                src,
                "player_state %r is never produced" % state,
            )

    def test_state_cadence_is_4hz(self):
        src = page()
        m = re.search(r"STATE_PERIOD_MS\s*=\s*(\d+)", src)
        self.assertIsNotNone(m, "STATE_PERIOD_MS is not a named constant")
        self.assertEqual(int(m.group(1)), int(round(1000.0 / C.STATE_HZ)))

    def test_every_message_carries_seq(self):
        src = page()
        # one envelope builder, and it always sets seq
        self.assertIn("function envelope(", src)
        self.assertRegex(src, r"var msg = \{ type: type, seq: seq \}")
        # state/hello/ended/error go through the same builder, never through a bare object
        for sender in ("'hello'", "'state'", "'ended'", "'error'", "'clock_reply'", "'intent'"):
            self.assertIn(sender, src)


class TestClock(unittest.TestCase):
    def test_rx_ms_uses_performance_timeorigin(self):
        src = page()
        self.assertIn("performance.timeOrigin + performance.now()", src)

    def test_rx_ms_and_currenttime_are_sampled_in_the_same_statement(self):
        # contracts §3: a pair that straddles a frame boundary looks like a rate error
        src = page()
        m = re.search(
            r"var rx_ms = performance\.timeOrigin \+ performance\.now\(\), "
            r"media = [^;]+;",
            src,
        )
        self.assertIsNotNone(
            m, "rx_ms and the media position must be read in one statement, as contracts §3 says"
        )

    def test_play_at_schedules_one_play_at_the_target_time(self):
        src = page()
        # the release is computed from the target and the receiver clock, not from a burst of seeks
        self.assertIn("target - rxNow()", src)
        # one scheduled release, from the clock — not a burst of seeks
        self.assertRegex(src, r"setTimeout\(function \(\) \{ attempt\(\); \}, Math\.max\(0, wait\)\)")


class TestSelfContained(unittest.TestCase):
    """Markup lives in the frozen loader page; logic lives in receiver.js."""

    def test_only_external_url_is_the_caf_loader(self):
        src = markup()
        urls = re.findall(r"""https?://[^\s"'<>)\\]+""", src)
        external = [u for u in urls if not u.startswith(("http://192.168.2.", "http://localhost"))]
        self.assertEqual(external, [CAF_LOADER], "unexpected external URL(s): %r" % external)

    def test_no_other_remote_resource(self):
        src = markup()
        self.assertEqual(
            len(re.findall(r"<script[^>]+src=", src)), 1, "exactly one script src: the CAF loader"
        )
        self.assertNotRegex(src, r"<link[^>]+href=", "no external stylesheet")
        self.assertNotRegex(src, r"@import|fonts\.googleapis|fonts\.gstatic")
        for tracker in ("gtag(", "googletagmanager", "google-analytics", "analytics.js", "ga("):
            self.assertNotIn(tracker, src)

    def test_caf_loader_is_the_v3_receiver_framework(self):
        self.assertIn(CAF_LOADER, markup())

    def test_idle_page_names_the_app_and_says_ready(self):
        src = markup()
        idle = re.search(r'<div id="idle">(.*?)</div>\s*</div>', src, re.S)
        self.assertIsNotNone(idle, "no idle block")
        block = idle.group(1)
        self.assertIn("VideoNetCaster", block)
        self.assertIn("ready", block)

    def test_no_secret_or_token_is_embedded(self):
        src = page() + markup()
        for marker in ("googlevideo", "api_key", "apikey", "Bearer ", "token="):
            self.assertNotIn(marker, src)

    def test_senders_are_answered_individually(self):
        src = page()
        # sendCustomMessage(namespace, senderId, msg): a reply must go to the sender that asked
        self.assertIn("sendCustomMessage(NS, senderId", src)
        self.assertIn("getSenders()", src)


NODE = shutil.which("node")
SELFTEST = os.path.join(ROOT, "receiver", "selftest.js")


@unittest.skipUnless(NODE, "node is not installed; the offline page harness needs it")
class TestPageLogicOffline(unittest.TestCase):
    """Drive the page's own <script> with the CAF SDK stubbed (receiver/selftest.js).

    The device cannot run an unpublished app until its serial is registered, so this is the only
    place the handlers are actually executed. It checks protocol behaviour, never rendering.
    """

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run([NODE, SELFTEST], capture_output=True, text=True, timeout=180)
        cls.rc, cls.stdout, cls.stderr = proc.returncode, proc.stdout, proc.stderr
        cls.result = json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else None

    def test_harness_produced_results(self):
        self.assertIsNotNone(self.result, "selftest.js printed nothing usable: %r" % self.stderr[-500:])
        self.assertGreaterEqual(self.result["checks"], 30)

    def test_every_page_check_passes(self):
        self.assertIsNotNone(self.result)
        failed = [r for r in self.result["results"] if not r["ok"]]
        self.assertEqual(failed, [], "receiver.html failed: %r" % failed)
        self.assertEqual(self.rc, 0)


if __name__ == "__main__":
    unittest.main()
