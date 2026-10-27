"""The v0.1 relay rebuilds a dead group link even when the failures are not consecutive.

Measured on CT 121 (2026-10-26 03:38 → 2026-10-27 17:33): an idle group link died, and only its
5 s keepalive touched it, so one step in twenty raised ``BrokenPipeError`` and every step in
between succeeded. ``Relay.run`` rebuilt the link after two failures *in a row*, which never came:
26 514 ``step_error`` and zero ``sink_reconnected`` in 38 h. Two failures inside
``ERROR_WINDOW_S`` are now enough.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY = os.path.join(HERE, "..", "..", "relay")
if RELAY not in sys.path:
    sys.path.insert(0, RELAY)

import cast_relay as R  # noqa: E402


class FakeSink:
    host, port = "192.168.2.61", 32221

    def __init__(self) -> None:
        self.reconnects = 0

    def reconnect(self) -> None:
        self.reconnects += 1


def bare_relay(step):
    """A Relay with only what run() needs: no TV, no group, no lock file, no log file."""
    relay = R.Relay.__new__(R.Relay)
    relay.sink = FakeSink()
    relay.dry_run = False
    relay.poll = 0.0
    relay._errors = 0
    relay._fh = None
    relay.need_start = False
    relay.events = []
    relay._log = relay.events.append
    relay.step = step
    return relay


class TestDeadLinkIsRebuilt(unittest.TestCase):
    def run_steps(self, pattern, steps):
        calls = {"n": 0}

        def step():
            i = calls["n"]
            calls["n"] += 1
            if i >= steps:
                raise KeyboardInterrupt
            if pattern(i):
                raise BrokenPipeError(32, "Broken pipe")

        relay = bare_relay(step)
        try:
            relay.run()
        except KeyboardInterrupt:
            pass
        return relay

    def test_one_failure_in_twenty_still_rebuilds_the_link(self):
        relay = self.run_steps(lambda i: i % 20 == 19, 200)
        self.assertGreaterEqual(relay.sink.reconnects, 1)
        self.assertTrue(relay.need_start)
        self.assertIn("sink_reconnected", [e.get("ev") for e in relay.events])

    def test_consecutive_failures_still_rebuild_it(self):
        relay = self.run_steps(lambda i: i in (3, 4), 10)
        self.assertEqual(relay.sink.reconnects, 1)

    def test_a_single_failure_does_not(self):
        relay = self.run_steps(lambda i: i == 5, 50)
        self.assertEqual(relay.sink.reconnects, 0)


if __name__ == "__main__":
    unittest.main()
