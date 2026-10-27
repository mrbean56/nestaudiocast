"""SenderLink over the real wire: TLS, CASTv2 framing, our namespace, a localhost receiver.

Skipped when pychromecast/cryptography are not installed (the contracts-only CI job), because
the point of these tests is the protocol path itself, not a stub. Nothing here touches a device,
a speaker, a group or the TV: the receiver is tests/v020/fake_receiver_net.py on 127.0.0.1.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.abspath(os.path.join(HERE, "..", "..", "lib"))
for _p in (LIB, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from v020 import chromecast as X  # noqa: E402
from v020 import contracts as C  # noqa: E402


def _deps_ok() -> bool:
    try:
        import cryptography  # noqa: F401
        import pychromecast  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


HAVE_DEPS = _deps_ok()
if HAVE_DEPS:
    from fake_receiver_net import FakeReceiverNet  # noqa: E402


def quiet(*_a, **_k):
    return None


@unittest.skipUnless(HAVE_DEPS, "pychromecast + cryptography (the wire path) not installed")
class WireCase(unittest.TestCase):
    """One fake receiver and one link per test; both always closed."""

    fake_kwargs: dict = {}

    def setUp(self):
        self.fake = FakeReceiverNet(seed=3, log=quiet, **self.fake_kwargs).start()
        self.link = X.SenderLink(self.fake.host, self.fake.app_id, self.fake.port, log=quiet)

    def tearDown(self):
        try:
            self.link.close()
        finally:
            self.fake.stop()

    def launch(self):
        return self.link.launch(timeout=5.0)

    def load(self, autoplay=True):
        return self.link.send(
            C.MSG_LOAD,
            url="http://127.0.0.1/media/s1/audio.m4a",
            content_type="audio/mp4",
            media=10.0,
            autoplay=autoplay,
        )


class TestLifecycle(WireCase):
    fake_kwargs = {"state_hz": 0.0}

    def test_launch_waits_for_hello(self):
        status = self.launch()
        self.assertEqual((status.get("applications") or [{}])[0].get("appId"), "28028CD2")
        self.assertEqual(self.link.hellos, 1)
        self.assertEqual(self.link.hello["ua"], "fake_receiver_net/0.2")
        self.assertEqual(self.link.hello["version"], "0.2.0-fake-net")
        self.assertEqual(self.link.transport, self.fake.app["transportId"])
        self.assertEqual(self.link.seq, 0)  # LAUNCH is castv2's own request counter
        self.assertTrue(self.link.is_ours())
        self.load()  # the protocol counter starts with our first
        self.assertEqual(self.link.seq, 1)  # controller message

    def test_launch_is_idempotent_when_our_app_runs(self):
        self.launch()
        transport = self.link.transport
        self.launch()  # adopts, never relaunches
        self.assertEqual(self.link.transport, transport)
        self.assertEqual(self.link.hellos, 2)

    def test_connect_attaches_to_a_running_app(self):
        self.launch()
        self.link.close()
        self.link.connect(timeout=5.0)
        self.assertTrue(self.link.is_ours())
        self.assertEqual(self.link.hellos, 2)  # a fresh CONNECT earns a fresh hello

    def test_connect_on_an_idle_device_is_lost_device(self):
        with self.assertRaises(X.LostDevice):
            self.link.connect(timeout=5.0)
        self.assertEqual(self.link.lost_reason, "no_app")

    def test_connect_on_a_foreign_app_is_lost_device(self):
        self.fake.takeover()
        with self.assertRaises(X.LostDevice):
            self.link.connect(timeout=5.0)
        self.assertTrue(self.link.lost_reason.startswith("foreign_app:"))

    def test_launch_refuses_to_clobber_a_foreign_app(self):
        self.fake.takeover()
        with self.assertRaises(X.LostDevice):
            self.launch()
        self.assertTrue(self.link.lost_reason.startswith("foreign_app:"))

    def test_restart_transport_reattaches_and_gets_a_new_hello(self):
        self.launch()
        hellos = self.link.hellos
        new_transport = self.fake.restart_transport()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and self.link.transport != new_transport:
            self.link._service(timeout=0.05)
        self.assertEqual(self.link.transport, new_transport)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and self.link.hellos == hellos:
            self.link._service(timeout=0.05)
        self.assertEqual(self.link.hellos, hellos + 1)
        self.assertTrue(self.link.is_ours())


class TestClockOnTheWire(WireCase):
    fake_kwargs = {"state_hz": 0.0, "rtt_ms": (0.0, 40.0), "asym_jitter_ms": 0.5}

    def test_residual_and_error_under_a_0_40ms_rtt(self):
        self.launch()
        residuals, errors = [], []
        for _ in range(6):
            est = self.link.ping(8)
            residuals.append(est.residual_ms)
            errors.append(abs(est.offset_ms - self.fake.clock_offset_ms))
        self.assertLessEqual(max(residuals), 2.0)
        self.assertLessEqual(max(errors), 2.0)
        self.assertEqual(self.link.ping(3).samples, 3)

    def test_forward_jump_is_flagged_not_followed(self):
        self.launch()
        before = self.link.ping(6).offset_ms
        self.fake.jump_clock(5000.0)
        after = self.link.ping(6)
        self.assertTrue(self.link.clock_flagged)
        self.assertAlmostEqual(after.offset_ms, before, places=3)
        self.assertGreater(self.link.clock_jump_ms, 4900.0)
        self.link.reset_clock()
        adopted = self.link.ping(6)
        self.assertFalse(self.link.clock_flagged)
        self.assertGreater(adopted.offset_ms, before + 4000.0)

    def test_backward_jump_is_seen_by_the_monotone_guard(self):
        self.launch()
        before = self.link.ping(6).offset_ms
        self.fake.jump_clock(-5000.0)
        self.link.ping(6)
        self.assertGreaterEqual(self.link.clock_backwards, 1)
        self.assertTrue(self.link.clock_flagged)
        self.assertAlmostEqual(self.link.clock.offset_ms, before, places=3)


class TestPresenceOnTheWire(WireCase):
    fake_kwargs = {"state_hz": 0.0, "rtt_ms": (0.0, 5.0)}

    def test_takeover_detected_within_one_poll(self):
        self.launch()
        self.load()
        self.assertTrue(self.link.is_ours())
        self.fake.takeover()
        started = time.monotonic()
        ours = self.link.is_ours()  # exactly one poll
        poll_ms = (time.monotonic() - started) * 1000.0
        self.assertFalse(ours)
        self.assertLess(poll_ms, 1000.0)
        self.assertTrue(self.link.lost_reason.startswith("foreign_app:"))
        with self.assertRaises(X.LostDevice):
            self.link.send(C.MSG_PAUSE)

    def test_takeover_detected_by_one_read_without_a_poll(self):
        self.launch()
        self.fake.takeover()
        started = time.monotonic()
        self.link.drain()  # the unsolicited RECEIVER_STATUS
        read_ms = (time.monotonic() - started) * 1000.0
        self.assertLess(read_ms, 1000.0)
        self.assertEqual(self.link.lost_reason, "foreign_app:9A1B2C3D")
        self.assertFalse(self.link.is_ours())

    def test_stop_from_another_sender(self):
        self.launch()
        self.fake.stop_by_other_sender()
        self.link.drain()
        self.assertEqual(self.link.lost_reason, "app_not_running")

    def test_idle_state_while_we_own_it(self):
        self.launch()
        self.load()
        self.fake.player_state = C.PLAYER_IDLE
        self.fake.media_loaded = False
        self.fake.emit_state()
        self.link.drain()
        self.assertEqual(self.link.lost_reason, "idle_while_ours")


class TestReconnect(WireCase):
    fake_kwargs = {"state_hz": 0.0, "rtt_ms": (0.0, 5.0)}

    def test_forced_socket_close_then_bounded_backoff(self):
        self.launch()
        self.load()
        seq_before = self.link.seq
        hellos_before = self.link.hellos
        self.fake.drop_clients()
        time.sleep(0.1)
        with self.assertRaises(X.LinkDown):
            self.link.send(C.MSG_PAUSE)
        started = time.monotonic()
        self.link.reconnect()
        elapsed_ms = (time.monotonic() - started) * 1000.0
        self.assertLess(elapsed_ms, 5000.0)
        self.assertEqual(self.link.reconnects, 1)
        self.assertTrue(self.link.is_ours())
        self.assertEqual(self.link.hellos, hellos_before + 1)
        self.assertEqual(self.link.send(C.MSG_PAUSE), seq_before + 1)  # seq never reset

    def test_reconnect_gives_up_after_the_bounded_backoff(self):
        self.launch()
        calls = []

        def refused(timeout):  # castv2's own connect retries for ~4 s; the thing
            calls.append(timeout)  # under test here is our backoff schedule
            raise X.LinkDown("connection refused (simulated)")

        self.link._open = refused
        started = time.monotonic()
        with self.assertRaises(X.LinkDown):
            self.link.reconnect(attempts=3)
        elapsed = time.monotonic() - started
        self.assertEqual(len(calls), 3)
        self.assertLess(elapsed, 2.0)  # 0.05 + 0.10 + 0.20, bounded, no runaway

    def test_reconnect_to_a_dead_device_is_bounded(self):
        self.launch()
        self.fake.stop()  # nothing listens any more
        started = time.monotonic()
        with self.assertRaises(X.LinkDown):
            self.link.reconnect(attempts=1, timeout=0.3)
        self.assertLess(time.monotonic() - started, 10.0)


class TestProtocol(WireCase):
    fake_kwargs = {"state_hz": 0.0}

    def test_latest_is_a_typed_sample(self):
        self.launch()
        self.load()
        deadline = time.monotonic() + 2.0
        sample = None
        while time.monotonic() < deadline and sample is None:
            sample = self.link.latest()
        self.assertIsInstance(sample, C.Sample)
        self.assertEqual(sample.player_state, C.PLAYER_PLAYING)
        self.assertGreaterEqual(sample.media, 10.0)
        self.assertLess(sample.media, 12.0)
        self.assertIsNotNone(sample.buffered_end)

    def test_state_transitions_round_trip(self):
        self.launch()
        self.load(autoplay=False)
        self.link._service(timeout=0.2)
        self.link.drain()
        self.assertEqual(self.link.latest().player_state, C.PLAYER_PAUSED)
        self.link.send(C.MSG_PLAY_AT, rx_ms=self.fake._rx_ms() + 200.0, media=42.0)
        self.link._service(timeout=0.2)
        state = self.link.drain()
        self.assertTrue(any(m.get("type") == C.MSG_STATE for m in state))
        self.assertEqual(self.link.latest().player_state, C.PLAYER_PLAYING)
        self.assertAlmostEqual(self.link.latest().media, 42.0, delta=0.3)
        self.link.send(C.MSG_STOP)
        self.link._service(timeout=0.2)
        self.assertEqual(self.link.latest().player_state, C.PLAYER_IDLE)

    def test_inbox_bounded_under_1000_injected_messages(self):
        self.launch()
        self.link.drain()  # drop the hello, count only the burst
        injected = 1000
        writer = threading.Thread(target=self.fake.burst_states, args=(injected,), daemon=True)
        writer.start()
        time.sleep(0.5)  # let the burst pile up, as a client that is not
        received = 0  # draining would let it
        largest = 0
        ordered = True
        last = -1.0
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and received + self.link.dropped < injected:
            batch = self.link.drain()
            largest = max(largest, len(batch))
            for msg in batch:
                if msg.get("type") != C.MSG_STATE:
                    continue
                received += 1
                if msg["media"] <= last:
                    ordered = False
                last = msg["media"]
        writer.join(timeout=5.0)
        self.assertLessEqual(largest, X.INBOX_MAX)
        self.assertTrue(ordered, "drain() must keep arrival order")
        self.assertEqual(received + self.link.dropped, injected)
        self.assertGreater(self.link.dropped, 0)


if __name__ == "__main__":
    unittest.main()


class TestStaleAppRecovery(unittest.TestCase):
    """A wedged app instance (ours, but silent) must be recycled, not adopted for ever.

    Measured on CT 121 (2026-10-26): after several sessions the app was still running, the page
    never answered the namespace, and every share failed with 'no hello' until the app was stopped.
    """

    def test_launch_relaunches_a_silent_app_once(self):
        from v020.chromecast import SenderLink

        receiver = FakeReceiverNet(mute_hello=True)
        receiver.start()
        self.addCleanup(receiver.stop)
        link = SenderLink(receiver.host, receiver.app_id, port=receiver.port, log=lambda *a, **k: None)
        self.addCleanup(link.close)
        status = link.launch(timeout=8.0)
        self.assertTrue(status)
        self.assertGreaterEqual(link.hellos, 1, "the relaunched app must greet us")


class TestStatusOnADeadLink(unittest.TestCase):
    """A status read must not keep retrying a socket that died.

    Measured on CT 121 (2026-10-27): after an app restart the device drops the TLS session, our
    link stays dead, and every later GET_STATUS times out on it — three retries burned 6.5 s and
    the share failed. The retry now reopens the link first, which turned 4/8 failing share cycles
    into 8/8 passing.
    """

    def test_a_dropped_link_is_reopened_before_the_next_read(self):
        from v020.chromecast import SenderLink

        receiver = FakeReceiverNet()
        receiver.start()
        self.addCleanup(receiver.stop)
        link = SenderLink(receiver.host, receiver.app_id, port=receiver.port, log=lambda *a, **k: None)
        self.addCleanup(link.close)
        link.launch(timeout=8.0)
        self.assertTrue(link.is_ours())

        dropped = receiver.drop_clients()
        self.assertGreaterEqual(dropped, 1, "the fake must actually drop the session")

        verdict = link.is_ours()
        self.assertTrue(verdict, "a reopened link must answer, not pile up timeouts")
        # and the session keeps working afterwards: a second verdict, and a real launch path
        self.assertTrue(link.is_ours())
        self.assertEqual(link.lost_reason, None)
