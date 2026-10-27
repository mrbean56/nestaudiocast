"""The session's threads never meet on a device — review 2026-10-27.

The start worker, the ticker and the HTTP thread all reach the same CASTv2 sockets. Neither
``SenderLink`` nor ``CastGroup`` has a lock of its own, so the session must guarantee that only
one thread is inside a device call at a time. Each test here wraps the fakes in a detector that
records any overlap, widens the windows with short sleeps, and runs real threads.
"""

from __future__ import annotations

import threading
import time
import unittest

from test_sender_support import YOUTUBE, C, TempDir, events_named, make_sender

import sender.session as S

#: device calls watched for overlap (everything that would touch a socket on the real seams)
LINK_CALLS = ("launch", "connect", "send", "drain", "latest", "ping", "is_ours")
GROUP_CALLS = ("connect", "prepare", "play", "pause", "seek", "stop", "set_volume", "sample")


class RealClock:
    def __call__(self) -> float:
        return time.monotonic()

    def now(self) -> float:
        return time.time()


class Overlap:
    """Wraps device methods; any two threads inside at once is a violation."""

    def __init__(self) -> None:
        self.inside = 0
        self.violations: list[str] = []
        self.guard = threading.Lock()
        self.calls: list[tuple[str, str]] = []

    def watch(self, obj, names, label: str, slow: dict | None = None) -> None:
        for name in names:
            original = getattr(obj, name, None)
            if original is None:
                continue
            setattr(obj, name, self._wrap(original, f"{label}.{name}", (slow or {}).get(name, 0.0)))

    def _wrap(self, fn, label: str, delay: float):
        def wrapped(*args, **kwargs):
            with self.guard:
                self.inside += 1
                self.calls.append((threading.current_thread().name, label))
                if self.inside > 1:
                    self.violations.append(f"{label} on {threading.current_thread().name}")
            try:
                if delay:
                    time.sleep(delay)
                return fn(*args, **kwargs)
            finally:
                with self.guard:
                    self.inside -= 1

        return wrapped


def threaded(fn):
    thread = threading.Thread(target=fn, name="cast-sender-session", daemon=True)
    thread.start()
    return thread


class ConcurrencyCase(TempDir, unittest.TestCase):
    def make(self, slow_link=None, slow_group=None):
        fx = make_sender(self.root, clock=RealClock(), spawn=threaded)
        watch = Overlap()
        watch.watch(fx.link, LINK_CALLS, "link", slow_link)
        watch.watch(fx.group, GROUP_CALLS, "group", slow_group)
        return fx, watch

    def run_ticker(self, fx, seconds: float, also_status: bool = True) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            fx.sender.tick()
            if also_status:
                fx.sender.status()
            time.sleep(0.01)

    def wait_state(self, fx, state: str, seconds: float = 5.0) -> str:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            fx.sender.tick()
            got = fx.sender.status()["state"]
            if got == state:
                return got
            time.sleep(0.02)
        return fx.sender.status()["state"]


class TestOneThreadOnTheDevices(ConcurrencyCase):
    def test_ticker_and_status_stay_off_the_link_during_a_start(self):
        fx, watch = self.make(slow_link={"launch": 0.3, "connect": 0.1})
        fx.sender.share(YOUTUBE)
        self.run_ticker(fx, 1.0)
        self.assertEqual(self.wait_state(fx, C.STATE_PLAYING), C.STATE_PLAYING)
        self.assertEqual(watch.violations, [])

    def test_a_second_share_during_a_start_waits_for_the_first(self):
        fx, watch = self.make(slow_link={"launch": 0.4})
        first = fx.sender.share(YOUTUBE)
        time.sleep(0.05)
        second = fx.sender.share("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        self.run_ticker(fx, 1.5)
        self.assertEqual(self.wait_state(fx, C.STATE_PLAYING), C.STATE_PLAYING)
        status = fx.sender.status()
        self.assertEqual(status["session_id"], second["session_id"])
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(watch.violations, [])
        # the first worker gave the devices back: one launch each, never two at once
        launches = [label for _t, label in watch.calls if label == "link.launch"]
        self.assertEqual(len(launches), 2)

    def test_status_does_no_device_io(self):
        fx, watch = self.make()
        fx.sender.share(YOUTUBE)
        self.assertEqual(self.wait_state(fx, C.STATE_PLAYING), C.STATE_PLAYING)
        before = len(watch.calls)
        for _ in range(20):
            fx.sender.status()
        self.assertEqual(watch.calls[before:], [])


class TestStopDuringStart(ConcurrencyCase):
    def test_a_stop_during_group_prepare_still_stops_the_group(self):
        fx, watch = self.make(slow_group={"prepare": 0.5})
        fx.sender.share(YOUTUBE)
        deadline = time.monotonic() + 5.0
        while "group.prepare" not in [label for _t, label in watch.calls]:
            self.assertLess(time.monotonic(), deadline, "the start never reached prepare")
            time.sleep(0.01)
        fx.sender.control(C.ACTION_STOP)  # lands while prepare() is still loading the group
        self.run_ticker(fx, 1.0)
        calls = fx.group.calls
        self.assertIn("prepare", calls)
        self.assertEqual(calls[-1], "stop", calls)
        self.assertNotIn("play", calls)
        self.assertEqual(watch.violations, [])
        self.assertTrue(events_named(fx.log.path, "stop_deferred_to_start"))

    def test_a_failed_resolution_stops_the_app_the_parallel_launch_started(self):
        fx, watch = self.make(slow_link={"launch": 0.3})

        def broken(url, **kw):
            raise RuntimeError("extraction failed")

        fx.sender._resolver = broken
        fx.sender.share(YOUTUBE)
        self.run_ticker(fx, 1.2)
        self.assertIn(fx.sender.status()["state"], (C.STATE_ENDED, C.STATE_IDLE))
        self.assertTrue(fx.link.launched)
        self.assertIn(C.MSG_IDLE, [m["type"] for m in fx.link.inbox])

    def test_a_launch_that_outlives_the_start_is_undone(self):
        original = S.RECEIVER_LAUNCH_JOIN_S
        S.RECEIVER_LAUNCH_JOIN_S = 0.2
        try:
            fx, watch = self.make(slow_link={"launch": 0.6})
            fx.sender.share(YOUTUBE)
            self.run_ticker(fx, 2.0)
        finally:
            S.RECEIVER_LAUNCH_JOIN_S = original
        self.assertTrue(fx.link.launched)
        self.assertIn(C.MSG_IDLE, [m["type"] for m in fx.link.inbox])
        self.assertIn(fx.sender.status()["state"], (C.STATE_ENDED, C.STATE_IDLE))


if __name__ == "__main__":
    unittest.main()
