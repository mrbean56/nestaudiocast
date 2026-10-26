"""The contracts are frozen: this test fails if the surface drifts.

It checks names, signatures and constants — never implementations, so it stays valid while every
task fills the stubs in.
"""

from __future__ import annotations

import inspect
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.abspath(os.path.join(HERE, "..", "..", "lib"))
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from v020 import chromecast, group, media_proxy, resolve, sync  # noqa: E402
from v020 import contracts as C  # noqa: E402


class TestConstants(unittest.TestCase):
    def test_receiver_namespace_and_messages(self):
        self.assertEqual(C.NS_RECEIVER, "urn:x-cast:com.beansden.nestaudiocast.v1")
        self.assertEqual(
            set(C.CONTROLLER_MESSAGES),
            {"clock", "load", "play_at", "pause", "seek", "rate", "stop", "idle"},
        )
        self.assertEqual(
            set(C.RECEIVER_MESSAGES), {"hello", "clock_reply", "state", "intent", "ended", "error"}
        )

    def test_frozen_url_and_port(self):
        # registered in the Cast console: changing it costs a 15 min propagation
        self.assertEqual(C.PATH_RECEIVER, "/receiver.html")
        self.assertEqual(C.HTTP_PORT_DEFAULT, 7640)
        self.assertEqual(C.CONFIG_DEFAULTS["APP_ID"], "28028CD2")
        self.assertEqual(C.CONFIG_DEFAULTS["CHROMECAST"], "192.168.2.80")
        self.assertEqual(C.CONFIG_DEFAULTS["GROUP"], "Cuisine")

    def test_states_and_actions(self):
        self.assertEqual(
            set(C.SESSION_STATES),
            {"idle", "resolving", "starting", "playing", "paused", "ended", "quiet_refused"},
        )
        self.assertEqual(set(C.ACTIONS), {"play", "pause", "seek", "volume", "stop"})
        self.assertEqual(set(C.QUIET_POLICIES), {"refuse", "video_only"})

    def test_timeline(self):
        self.assertAlmostEqual(C.digital_error(10.5, 10.0, 0.0), 0.5)
        self.assertAlmostEqual(C.digital_error(10.0, 10.0, 0.25), -0.25)
        self.assertEqual(C.DIGITAL_TOL_MS, 20.0)
        self.assertEqual(C.STEADY_AFTER_S, 30.0)


class TestSignatures(unittest.TestCase):
    """Every seam exposes the documented callables with the documented parameters."""

    def _params(self, fn):
        return list(inspect.signature(fn).parameters)

    def test_resolver(self):
        self.assertEqual(self._params(resolve.resolve), ["url", "max_height", "timeout"])
        self.assertTrue(callable(resolve.redact))
        self.assertTrue(callable(resolve.video_id_of))

    def test_proxy(self):
        for name in ("open_session", "close_session", "mpd_for", "audio_url_for", "refresh", "stats"):
            self.assertTrue(callable(getattr(media_proxy.MediaProxy, name)), name)

    def test_sender_link(self):
        for name in ("launch", "connect", "close", "send", "drain", "ping", "latest", "is_ours"):
            self.assertTrue(callable(getattr(chromecast.SenderLink, name)), name)
        self.assertTrue(callable(chromecast.probe))
        self.assertTrue(issubclass(chromecast.LostDevice, RuntimeError))

    def test_group(self):
        for name in (
            "connect",
            "reconnect",
            "prepare",
            "play",
            "pause",
            "seek",
            "set_volume",
            "stop",
            "sample",
            "playhead",
            "close",
        ):
            self.assertTrue(callable(getattr(group.CastGroup, name)), name)
        self.assertTrue(issubclass(group.UnsafeGroup, RuntimeError))

    def test_controller(self):
        for name in (
            "start",
            "step",
            "on_seek",
            "on_pause",
            "on_resume",
            "on_end",
            "error",
            "steady",
            "window_median_ms",
        ):
            self.assertTrue(callable(getattr(sync.Controller, name)), name)


class TestFakes(unittest.TestCase):
    """The fakes must satisfy the same surface, otherwise nobody is unblocked by them."""

    def setUp(self):
        from fakes import FakeGroup, FakeMedia, FakeProxy, FakeReceiver

        self.FakeGroup, self.FakeProxy = FakeGroup, FakeProxy
        self.FakeMedia, self.FakeReceiver = FakeMedia, FakeReceiver

    def test_fake_group_refuses_unsafe(self):
        from fakes import FakeGroup
        from fakes.fake_group import UnsafeGroup

        g = FakeGroup(unsafe=True)
        with self.assertRaises(UnsafeGroup):
            g.connect()

    def test_fake_receiver_rate_gain_idempotent(self):
        r = self.FakeReceiver(rate_gain=0.0)
        r.send(C.MSG_LOAD, url="http://x/y", content_type="video/mp4", media=0.0, autoplay=True)
        r.send(C.MSG_RATE, value=1.002)
        self.assertAlmostEqual(r.applied_rate, 1.0)  # echoes 1.002 and does nothing
        self.assertAlmostEqual(r.rate, 1.002)

    def test_fake_proxy_roundtrip(self):
        p = self.FakeProxy()
        sid = p.open_session(self.FakeMedia.resolved())
        self.assertIn("/media/", p.audio_url_for(sid))
        self.assertIn("<MPD", p.mpd_for(sid))
        p.close_session(sid)
        self.assertEqual(p.stats()["sessions"], 0)


if __name__ == "__main__":
    unittest.main()
