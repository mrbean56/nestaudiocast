"""The HTTP surface — contracts §5.

Every endpoint with good and bad input, the LAN allow-list, the error shape, and the three
non-JSON paths (`/`, `/receiver.html`, `/media/*`). The server listens on 127.0.0.1 on an
ephemeral port; the fakes answer behind it, so nothing is audible.
"""

from __future__ import annotations

import json
import os
import unittest

from test_sender_support import YOUTUBE, C, TempDir, make_api, make_sender

from sender.api import SenderAPI, serve_in_thread


class ApiCase(TempDir, unittest.TestCase):
    extra_allowed = ("127.0.0.1",)

    def setUp(self):
        super().setUp()
        self.fixture = make_sender(self.root)
        os.makedirs(os.path.join(self.root, "assets", "measure"), exist_ok=True)
        with open(os.path.join(self.root, "assets", "measure", "click_train_2310.m4a"), "wb") as handle:
            handle.write(b"\x00" * 512)
        self.api = make_api(self.fixture, extra_allowed=self.extra_allowed)
        self.server, _thread = serve_in_thread(self.api, port=0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    # -- helpers ---------------------------------------------------------------------------
    def call(self, method: str, path: str, body=None, *, headers=None, raw=None):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        head = dict(headers or {})
        payload = raw
        if payload is None and body is not None:
            payload = json.dumps(body).encode()
            head.setdefault("Content-Type", "application/json")
        if payload is not None:
            head.setdefault("Content-Length", str(len(payload)))
        connection.putrequest(method, path)
        for key, value in head.items():
            connection.putheader(key, value)
        connection.endheaders()
        if payload is not None:
            connection.send(payload)
        reply = connection.getresponse()
        data = reply.read()
        connection.close()
        try:
            parsed = json.loads(data) if data else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        return reply.status, parsed, data, (reply.getheader("Content-Type") or "")

    def assert_error(self, method: str, path: str, code: int, body=None, **kw):
        status, parsed, _data, _ctype = self.call(method, path, body, **kw)
        self.assertEqual(status, code, f"{method} {path}")
        self.assertIsInstance(parsed, dict)
        self.assertIn("error", parsed, f"{method} {path} must answer a JSON error")
        return parsed


class TestReadEndpoints(ApiCase):
    def test_healthz(self):
        status, parsed, _data, ctype = self.call("GET", C.PATH_HEALTH)
        self.assertEqual(status, 200)
        self.assertEqual(parsed, {"ok": True, "owner": "cast-sender"})
        self.assertIn("application/json", ctype)

    def test_status_has_the_contract_fields(self):
        status, parsed, _data, _ctype = self.call("GET", C.PATH_STATUS)
        self.assertEqual(status, 200)
        expected = {
            "state",
            "session_id",
            "video_id",
            "position_video",
            "position_group",
            "digital_error_ms",
            "p95_ms",
            "rate",
            "steady_since",
            "quiet",
            "group_owner",
            "delta_v2",
            "last_event",
            "last_error",
        }
        self.assertEqual(set(parsed), expected)

    def test_the_ui_is_one_self_contained_page(self):
        status, _parsed, data, ctype = self.call("GET", C.PATH_UI)
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        page = data.lower()
        self.assertIn(b"<html", page)
        self.assertIn(b"/api/status", page)
        for external in (b"http://", b"https://", b"cdn.", b"<script src"):
            self.assertNotIn(external, page, "the UI must not reach the network at all")

    def test_receiver_html_is_404_with_a_clear_message_until_it_exists(self):
        parsed = self.assert_error("GET", C.PATH_RECEIVER, 404)
        self.assertEqual(parsed["error"], "receiver_missing")
        self.assertIn("receiver.html", parsed["detail"])
        os.makedirs(self.fixture.config.receiver_dir, exist_ok=True)
        with open(os.path.join(self.fixture.config.receiver_dir, "receiver.html"), "w") as fh:
            fh.write("<!doctype html><title>frozen path</title>")
        status, _parsed, data, ctype = self.call("GET", C.PATH_RECEIVER)
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"frozen path", data)

    def test_unknown_path_and_wrong_methods(self):
        self.assert_error("GET", "/nope", 404)
        self.assert_error("GET", C.PATH_SHARE, 405)
        self.assert_error("POST", C.PATH_STATUS, 405, {})
        self.assert_error("DELETE", C.PATH_UI, 405)
        self.assert_error("PUT", C.PATH_CONTROL, 405, {})
        status, _parsed, _data, _ctype = self.call("HEAD", C.PATH_STATUS)
        self.assertEqual(status, 405)


class TestShare(ApiCase):
    def test_a_good_share_answers_202_resolving(self):
        status, parsed, _data, _ctype = self.call("POST", C.PATH_SHARE, {"url": YOUTUBE, "start": 12.5})
        self.assertEqual(status, 202)
        self.assertEqual(set(parsed), {"session_id", "state"})
        self.assertTrue(parsed["session_id"].startswith("s"))
        self.assertEqual(parsed["state"], C.STATE_RESOLVING)
        status, status_doc, _data, _ctype = self.call("GET", C.PATH_STATUS)
        self.assertEqual(status_doc["session_id"], parsed["session_id"])
        self.assertIn(status_doc["state"], (C.STATE_PLAYING, C.STATE_STARTING, C.STATE_RESOLVING))

    def test_measure_is_accepted(self):
        status, parsed, _data, _ctype = self.call(
            "POST", C.PATH_SHARE, {"url": YOUTUBE, "measure": True}
        )
        self.assertEqual(status, 202)
        self.assertEqual(parsed["state"], C.STATE_RESOLVING)

    def test_bad_bodies(self):
        self.assert_error("POST", C.PATH_SHARE, 400, {})
        self.assert_error("POST", C.PATH_SHARE, 400, {"start": 1})
        self.assert_error("POST", C.PATH_SHARE, 400, {"url": 12})
        self.assert_error("POST", C.PATH_SHARE, 400, {"url": "ftp://host/x"})
        self.assert_error("POST", C.PATH_SHARE, 400, {"url": "https://h/x", "start": -1})
        self.assert_error("POST", C.PATH_SHARE, 400, {"url": "https://h/x", "measure": "yes"})
        self.assert_error("POST", C.PATH_SHARE, 400, {"url": "https://h/x", "Start": 1})
        self.assert_error(
            "POST", C.PATH_SHARE, 400, raw=b"{oops", headers={"Content-Type": "application/json"}
        )
        self.assert_error(
            "POST", C.PATH_SHARE, 400, raw=b"[]", headers={"Content-Type": "application/json"}
        )
        self.assert_error(
            "POST", C.PATH_SHARE, 400, raw=b"", headers={"Content-Type": "application/json"}
        )
        self.assert_error(
            "POST",
            C.PATH_SHARE,
            415,
            raw=b'{"url":"https://h/x"}',
            headers={"Content-Type": "text/plain"},
        )
        self.assert_error(
            "POST",
            C.PATH_SHARE,
            413,
            raw=b"{" + b"x" * 70000 + b"}",
            headers={"Content-Type": "application/json"},
        )

    def test_a_quiet_share_answers_202_quiet_refused(self):
        from sender.quiet import QuietWindow

        self.fixture.sender._quiet = QuietWindow("00:00", "00:01", "refuse")
        status, parsed, _data, _ctype = self.call("POST", C.PATH_SHARE, {"url": YOUTUBE})
        self.assertEqual(status, 202)
        self.assertEqual(parsed["state"], C.STATE_QUIET_REFUSED)
        self.assertEqual(self.fixture.group.calls, [])
        status, doc, _data, _ctype = self.call("GET", C.PATH_STATUS)
        self.assertEqual(doc["state"], C.STATE_QUIET_REFUSED)
        self.assertEqual(doc["quiet"]["decision"], "refuse")

    def test_an_oversized_body_does_not_poison_the_next_request(self):
        self.assert_error(
            "POST",
            C.PATH_SHARE,
            413,
            raw=b"{" + b"x" * 70000 + b"}",
            headers={"Content-Type": "application/json"},
        )
        status, _parsed, _data, _ctype = self.call("GET", C.PATH_HEALTH)
        self.assertEqual(status, 200)


class TestControl(ApiCase):
    def test_the_transport_matrix(self):
        self.call("POST", C.PATH_SHARE, {"url": YOUTUBE})
        for body, state in (
            ({"action": "pause"}, C.STATE_PAUSED),
            ({"action": "play"}, C.STATE_PLAYING),
            ({"action": "seek", "position": 30.0}, C.STATE_PLAYING),
            ({"action": "volume", "level": 0.25}, C.STATE_PLAYING),
            ({"action": "volume", "muted": True}, C.STATE_PLAYING),
            ({"action": "volume", "level": 0.3, "muted": False}, C.STATE_PLAYING),
        ):
            status, parsed, _data, _ctype = self.call("POST", C.PATH_CONTROL, body)
            self.assertEqual(status, 200, body)
            self.assertEqual(parsed, {"ok": True, "state": state})
        self.assertEqual(self.fixture.group.volume, 0.3)
        self.assertEqual(self.fixture.group.muted, False)
        status, parsed, _data, _ctype = self.call("POST", C.PATH_CONTROL, {"action": "stop"})
        self.assertEqual(status, 200)
        self.assertIn(parsed["state"], (C.STATE_ENDED, C.STATE_IDLE))
        status, parsed, _data, _ctype = self.call("POST", C.PATH_CONTROL, {"action": "stop"})
        self.assertEqual(parsed, {"ok": True, "state": C.STATE_IDLE})  # idempotent

    def test_bad_control_bodies(self):
        self.call("POST", C.PATH_SHARE, {"url": YOUTUBE})
        self.assert_error("POST", C.PATH_CONTROL, 400, {"action": "explode"})
        self.assert_error("POST", C.PATH_CONTROL, 400, {})
        self.assert_error("POST", C.PATH_CONTROL, 400, {"action": "seek"})
        self.assert_error("POST", C.PATH_CONTROL, 400, {"action": "seek", "position": "9"})
        self.assert_error("POST", C.PATH_CONTROL, 400, {"action": "volume"})
        self.assert_error("POST", C.PATH_CONTROL, 400, {"action": "volume", "level": 0})
        self.assert_error("POST", C.PATH_CONTROL, 400, {"action": "volume", "level": 2})
        self.assert_error("POST", C.PATH_CONTROL, 400, {"action": "volume", "muted": 1})
        self.assert_error("POST", C.PATH_CONTROL, 400, {"action": "pause", "level": 0.2})
        self.assert_error("POST", C.PATH_CONTROL, 400, {"action": "pause", "position": 3})
        self.assert_error("POST", C.PATH_CONTROL, 400, {"action": "stop", "muted": True})

    def test_control_without_a_session_is_409(self):
        self.assert_error("POST", C.PATH_CONTROL, 409, {"action": "play"})
        self.assert_error("POST", C.PATH_CONTROL, 409, {"action": "seek", "position": 1})
        self.assert_error("POST", C.PATH_CONTROL, 409, {"action": "volume", "level": 0.2})


class TestMediaAndMeasure(ApiCase):
    def test_the_manifest_is_served_for_an_open_session(self):
        self.call("POST", C.PATH_SHARE, {"url": YOUTUBE})
        sid = next(iter(self.fixture.proxy.sessions))
        status, _parsed, data, ctype = self.call("GET", f"{C.PATH_MEDIA_PREFIX}{sid}/manifest.mpd")
        self.assertEqual(status, 200)
        self.assertIn("dash+xml", ctype)
        self.assertIn(b"<MPD", data)

    def test_a_revoked_sid_is_410(self):
        self.call("POST", C.PATH_SHARE, {"url": YOUTUBE})
        sid = next(iter(self.fixture.proxy.sessions))
        self.call("POST", C.PATH_CONTROL, {"action": "stop"})
        parsed = self.assert_error("GET", f"{C.PATH_MEDIA_PREFIX}{sid}/manifest.mpd", 410)
        self.assertEqual(parsed["error"], "session_gone")

    def test_byte_ranges_say_what_is_missing(self):
        self.call("POST", C.PATH_SHARE, {"url": YOUTUBE})
        sid = next(iter(self.fixture.proxy.sessions))
        parsed = self.assert_error("GET", f"{C.PATH_MEDIA_PREFIX}{sid}/audio.m4a", 501)
        self.assertEqual(parsed["error"], "media_stream_unavailable")
        # contracts §10.4: the proxy may expose serve_http(...) or mount itself via handle(...)
        self.assertIn("byte endpoint", parsed["detail"])

    def test_a_proxy_that_speaks_http_is_used(self):
        def serve_http(method, path, headers):
            return (
                206,
                {"Content-Type": "audio/mp4", "Content-Range": "bytes 0-3/8"},
                b"\x00\x01\x02\x03",
            )

        self.fixture.proxy.serve_http = serve_http
        self.call("POST", C.PATH_SHARE, {"url": YOUTUBE})
        sid = next(iter(self.fixture.proxy.sessions))
        status, _parsed, data, _ctype = self.call("GET", f"{C.PATH_MEDIA_PREFIX}{sid}/0")
        self.assertEqual(status, 206)
        self.assertEqual(data, b"\x00\x01\x02\x03")

    def test_the_measure_asset(self):
        status, _parsed, data, ctype = self.call("GET", "/assets/measure/click_train_2310.m4a")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "audio/mp4")
        self.assertEqual(len(data), 512)
        with open(os.path.join(self.root, "assets", "measure", "notes.txt"), "w") as handle:
            handle.write("not a measurement asset")
        self.assert_error("GET", "/assets/measure/notes.txt", 403)
        self.assert_error("GET", "/assets/measure/missing.m4a", 404)
        self.assert_error("GET", "/assets/measure/..%2Fsecret.m4a", 400)
        self.assert_error("POST", "/assets/measure/click_train_2310.m4a", 405, {})


class TestLanOnly(ApiCase):
    extra_allowed = ()  # nothing but 192.168.2.0/24

    def test_a_loopback_client_is_not_a_lan_client(self):
        parsed = self.assert_error("GET", C.PATH_HEALTH, 403)
        self.assertEqual(parsed["error"], "forbidden")
        self.assertIn("192.168.2.0/24", parsed["detail"])

    def test_the_allow_list_itself(self):
        api = SenderAPI(
            self.fixture.sender,
            config=self.fixture.config,
            proxy=self.fixture.proxy,
            extra_allowed=("127.0.0.1", "::1"),
        )
        for allowed in ("192.168.2.1", "192.168.2.21", "192.168.2.255", "127.0.0.1", "::1"):
            self.assertTrue(api.allowed(allowed), allowed)
        for refused in ("10.0.0.5", "192.168.3.1", "8.8.8.8", "", "not-an-ip", "::ffff:10.0.0.5"):
            self.assertFalse(api.allowed(refused), refused)
        self.assertTrue(api.allowed("::ffff:192.168.2.18"))  # IPv4-mapped IPv6


if __name__ == "__main__":
    unittest.main()
