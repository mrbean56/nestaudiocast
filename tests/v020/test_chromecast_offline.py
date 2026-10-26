"""SenderLink without a socket: the estimator, the validators, the bounded inbox, presence.

Everything here runs on a bare python (no pychromecast, no TLS): the wire tests are in
tests/v020/test_chromecast_net.py. Contract §3 and A23 are what these check.
"""

from __future__ import annotations

import json
import os
import random
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.abspath(os.path.join(HERE, "..", "..", "lib"))
for _p in (LIB, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from v020 import chromecast as X  # noqa: E402
from v020 import contracts as C  # noqa: E402


def _state(media: float, rx_ms: float, ps: str = C.PLAYER_PLAYING) -> dict:
    return {"namespace": C.NS_RECEIVER,
            "payload": {"type": C.MSG_STATE, "seq": 1, "rx_ms": rx_ms, "media": media,
                        "rate": 1.0, "player_state": ps, "buffered_end": media + 30.0,
                        "dropped": 0}}


def _status(app_id: str | None, transport: str = "transport-1") -> dict:
    apps = [] if app_id is None else [{"appId": app_id, "sessionId": "s1",
                                       "transportId": transport}]
    return {"namespace": X.NS_CAST_RECEIVER,
            "payload": {"type": "RECEIVER_STATUS",
                        "status": {"applications": apps, "volume": {"level": 0.5}}}}


class TestClockEstimator(unittest.TestCase):
    def test_min_rtt_sample_is_selected_not_the_mean(self):
        # one clean round trip, four congested ones: averaging would drag the offset by ~11 ms.
        samples = [(1000.0, 1250.0, 1002.0)]
        samples += [(1000.0, 1258.0, 1040.0) for _ in range(4)]
        est, stats = X.estimate_clock(samples)
        self.assertAlmostEqual(est.offset_ms, 249.0, places=3)
        self.assertAlmostEqual(est.rtt_ms, 2.0, places=6)
        self.assertEqual(est.samples, 5)
        self.assertAlmostEqual(stats["spread_ms"], 11.0, places=3)
        self.assertAlmostEqual(est.residual_ms, 1.4826 * 11.0, places=3)
        self.assertAlmostEqual(stats["bound_ms"], 1.0, places=6)

    def test_residual_under_a_0_40ms_rtt_distribution(self):
        # 0-40 ms RTT, ±1 ms of path asymmetry (the fake's model): the residual, which is the
        # disagreement between pings, must stay under the 2 ms criterion of the brief.
        rng = random.Random(11)
        residuals, errors = [], []
        for _ in range(300):
            truth = 1_234_567.0
            samples = []
            for _ in range(8):
                rtt = rng.uniform(0.0, 40.0)
                d_in = rtt * 0.5 + rng.uniform(-0.5, 0.5)
                samples.append((1000.0, 1000.0 + d_in + truth, 1000.0 + rtt))
            est, _ = X.estimate_clock(samples)
            residuals.append(est.residual_ms)
            errors.append(abs(est.offset_ms - truth))
        self.assertLessEqual(max(residuals), 2.0)
        self.assertLessEqual(max(errors), 0.5)

    def test_rejects_an_empty_sample_set(self):
        with self.assertRaises(ValueError):
            X.estimate_clock([])


class TestSendValidation(unittest.TestCase):
    """A wrong message must never reach the wire."""

    def setUp(self):
        self.link = X.SenderLink("127.0.0.1", "28028CD2", 8009, log=lambda *a, **k: None)

    def test_unknown_type(self):
        with self.assertRaises(ValueError) as ctx:
            self.link.send("volume")
        self.assertIn("unknown controller message type", str(ctx.exception))

    def test_reserved_fields_are_owned_by_the_link(self):
        for field in ("type", "seq"):
            with self.assertRaises(ValueError):
                self.link.send("clock", **{field: 1})

    def test_required_fields(self):
        with self.assertRaises(ValueError):
            self.link.send("play_at", media=1.0)          # no rx_ms
        with self.assertRaises(ValueError):
            self.link.send("seek")                        # no media
        with self.assertRaises(ValueError):
            self.link.send("load", url="u", media=0.0)    # no content_type

    def test_rate_range(self):
        with self.assertRaises(ValueError):
            self.link.send("rate", value=3.0)
        self.assertEqual(len(self.link.inbox), 0)

    def test_no_link_is_link_down_not_lost_device(self):
        with self.assertRaises(X.LinkDown):
            self.link.send("clock")
        self.assertFalse(self.link.is_ours())


class TestBoundedInbox(unittest.TestCase):
    """A23: the v0.1 inbox grew forever. 256 max, oldest dropped, drops counted."""

    def setUp(self):
        self.link = X.SenderLink("127.0.0.1", "28028CD2", 8009, log=lambda *a, **k: None)

    def test_1000_injected_messages(self):
        for i in range(1000):
            self.link._on_message(_state(media=float(i), rx_ms=1_000.0 + i))
        self.assertEqual(len(self.link.inbox), X.INBOX_MAX)
        self.assertEqual(self.link.dropped, 1000 - X.INBOX_MAX)
        media = [m["media"] for m in self.link.inbox]
        self.assertEqual(media, sorted(media))                 # still in arrival order
        self.assertEqual(media[0], float(1000 - X.INBOX_MAX))  # oldest dropped, newest kept
        self.assertEqual(media[-1], 999.0)

    def test_drain_empties_and_returns_in_order(self):
        for i in range(10):
            self.link._on_message(_state(media=float(i), rx_ms=1_000.0 + i))
        drained = self.link.drain()
        self.assertEqual([m["media"] for m in drained], [float(i) for i in range(10)])
        self.assertEqual(list(self.link.inbox), [])


class TestClockGuards(unittest.TestCase):
    def setUp(self):
        self.link = X.SenderLink("127.0.0.1", "28028CD2", 8009, log=lambda *a, **k: None)

    def test_backwards_timestamp_is_flagged_not_followed(self):
        self.link._on_message(_state(media=10.0, rx_ms=5_000.0))
        self.assertEqual(self.link.last_state.media, 10.0)
        self.link._on_message(_state(media=99.0, rx_ms=500.0))   # page reloaded under us
        self.assertEqual(self.link.clock_backwards, 1)
        self.assertEqual(self.link.last_state.media, 10.0)       # not followed

    def test_hello_rebases_the_clock(self):
        self.link.clock = C.ClockEstimate(offset_ms=1234.0, residual_ms=0.2, rtt_ms=0.4,
                                          samples=5, measured_at=0.0)
        self.link._last_rx_ms = 9_000.0
        self.link._on_message({"namespace": C.NS_RECEIVER,
                               "payload": {"type": C.MSG_HELLO, "seq": 0, "rx_ms": 5.0,
                                           "ua": "fake", "version": "0.2.0",
                                           "codecs": {}}})
        self.assertEqual(self.link.hellos, 1)
        self.assertIsNone(self.link.clock)
        self.assertIsNone(self.link._last_rx_ms)
        self.assertEqual(self.link.receiver_ms(0.0), None)


class TestPresence(unittest.TestCase):
    """§3.3, without a socket: what the link concludes from what it reads."""

    def setUp(self):
        self.link = X.SenderLink("127.0.0.1", "28028CD2", 8009, log=lambda *a, **k: None)
        self.link._owns = True
        self.link.transport = "transport-1"

    def test_foreign_app_in_a_status_broadcast(self):
        self.link._on_message(_status("9A1B2C3D"))
        self.assertEqual(self.link.lost_reason, "foreign_app:9A1B2C3D")
        self.assertFalse(self.link.is_ours())

    def test_stop_from_another_sender(self):
        self.link._on_message(_status(None))
        self.assertEqual(self.link.lost_reason, "app_not_running")

    def test_idle_state_while_we_believe_we_own_it(self):
        self.link._media_loaded = True
        self.link._on_message(_state(media=0.0, rx_ms=1_000.0, ps=C.PLAYER_IDLE))
        self.assertEqual(self.link.lost_reason, "idle_while_ours")

    def test_our_own_app_keeps_it_ours(self):
        self.link._on_message(_status("28028CD2"))
        self.assertIsNone(self.link.lost_reason)

    def test_lost_device_blocks_commands(self):
        self.link._on_message(_status("9A1B2C3D"))

        class _InertLink:                  # a live socket would be needed; the check is first
            closed = False
            inbox: list = []

            def keepalive(self, every):
                pass

            def pump(self, timeout):
                return 0

        self.link.link = _InertLink()
        self.link.closed = False
        self.link.transport = "transport-1"
        with self.assertRaises(X.LostDevice):
            self.link.send("pause")


class _Eureka(BaseHTTPRequestHandler):
    body = {
        "name": "Salon",
        "version": "1.68.0",
        "build_version": "271279",
        "device_info": {"model_name": "Chromecast", "manufacturer": "Google",
                        "capabilities": {"a": 1}},
        "uptime": 123.4,
        "ip_address": "192.168.2.80",
        "bssid": "aa:bb:cc:dd:ee:ff",
        "ssid": "home",
        "wpa_configured": True,
        "secret_token": "must-not-leave-the-device",
    }

    def do_GET(self):  # noqa: N802 (stdlib naming)
        if not self.path.startswith("/setup/eureka_info"):
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(self.body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # noqa: D102
        pass


class TestProbe(unittest.TestCase):
    """probe() is read-only: one GET, a whitelist, and it never raises."""

    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _Eureka)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_reads_eureka_info_and_drops_the_rest(self):
        res = X.probe("127.0.0.1", port=self.port, timeout=2.0)
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["name"], "Salon")
        self.assertEqual(res["model_name"], "Chromecast")
        self.assertTrue(res["read_only"])
        for leaked in ("secret_token", "ssid", "bssid", "wpa_configured"):
            self.assertNotIn(leaked, res)

    def test_unreachable_device_is_an_answer(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()                       # nothing listens there any more
        res = X.probe("127.0.0.1", port=port, timeout=0.5)
        self.assertIn("error", res)
        self.assertNotIn("name", res)


if __name__ == "__main__":
    unittest.main()
