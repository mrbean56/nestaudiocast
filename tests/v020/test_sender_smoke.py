"""The service boots offline with fakes and answers every endpoint — brief G's "done when".

Two levels:

* ``build_service(offline=True)`` on an ephemeral port: status, UI, share, control, stop;
* the real entry point ``sender.service`` in ``--offline --once`` mode, which is exactly the
  command the lab note records, so the claim "the service boots" is reproducible.

The test also pins the safety property: in offline mode the seams are the in-memory fakes, so
no packet can reach a speaker, the Cuisine group or the Chromecast.
"""

from __future__ import annotations

import http.client
import json
import os
import unittest

from test_sender_support import YOUTUBE, C, TempDir

from sender.api import serve_in_thread
from sender.service import build_service


def _get(port: int, path: str, method: str = "GET", body: dict | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers, payload = {}, None
    if body is not None:
        payload = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
    connection.request(method, path, body=payload, headers=headers)
    reply = connection.getresponse()
    data = reply.read()
    connection.close()
    try:
        return reply.status, json.loads(data) if data else None
    except json.JSONDecodeError:
        return reply.status, None


class TestOfflineService(TempDir, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from test_sender_support import make_config

        self.config = make_config(self.root)
        self.service = build_service(
            self.config, offline=True, log=None or _log(self.root), state_dir=self.config.state_dir
        )
        self.addCleanup(self.service.close)
        self.server, _thread = serve_in_thread(self.service.api, port=0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def test_the_seams_are_the_fakes(self):
        from fakes import FakeGroup, FakeProxy, FakeReceiver

        self.assertIsInstance(self.service.link, FakeReceiver)
        self.assertIsInstance(self.service.group, FakeGroup)
        self.assertIsInstance(self.service.proxy, FakeProxy)
        self.assertTrue(self.service.offline)

    def test_every_endpoint_answers(self):
        status, body = _get(self.port, C.PATH_HEALTH)
        self.assertEqual((status, body), (200, {"ok": True, "owner": "cast-sender"}))
        status, body = _get(self.port, C.PATH_STATUS)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], C.STATE_IDLE)
        self.assertIsNone(body["session_id"])
        self.assertIsNone(body["group_owner"])
        status, _body = _get(self.port, C.PATH_UI)
        self.assertEqual(status, 200)
        status, body = _get(self.port, C.PATH_RECEIVER)
        self.assertEqual(status, 404)  # no receiver page in this checkout

    def test_a_share_runs_the_whole_machine(self):
        status, body = _get(self.port, C.PATH_SHARE, "POST", {"url": YOUTUBE})
        self.assertEqual(status, 202)
        self.assertEqual(body["state"], C.STATE_RESOLVING)
        session_id = body["session_id"]
        # the worker thread resolves and starts; give it a moment without sleeping long
        doc = _wait_for(self.port, lambda d: d["state"] == C.STATE_PLAYING)
        self.assertEqual(doc["state"], C.STATE_PLAYING)
        self.assertEqual(doc["session_id"], session_id)
        self.assertEqual(doc["group_owner"], "cast-sender")
        status, body = _get(self.port, C.PATH_CONTROL, "POST", {"action": "pause"})
        self.assertEqual(body, {"ok": True, "state": C.STATE_PAUSED})
        status, body = _get(self.port, C.PATH_CONTROL, "POST", {"action": "stop"})
        self.assertIn(body["state"], (C.STATE_ENDED, C.STATE_IDLE))
        # `ended` is visible for ENDED_LINGER_S, then the machine goes idle (contracts §5)
        doc = _wait_for(self.port, lambda d: d["state"] == C.STATE_IDLE, deadline=8.0)
        self.assertEqual(doc["state"], C.STATE_IDLE)
        self.assertFalse(os.path.exists(os.path.join(self.config.state_dir, "group.owner")))

    def test_boot_cleanup_is_run_by_the_service(self):
        record = os.path.join(self.config.state_dir, "session.json")
        from sender.group_lock import write_session_record

        write_session_record(
            record,
            {
                "owner": "cast-sender",
                "pid": 1,
                "session_id": "sold",
                "sid": "s9",
                "group_prepared": True,
                "receiver_launched": True,
                "mutes": [],
            },
        )
        result = self.service.boot_cleanup()
        self.assertEqual(result["actions"], ["group_stopped", "receiver_stopped", "proxy_closed"])
        self.assertFalse(os.path.exists(record))

    def test_the_lock_file_lives_where_the_contract_says(self):
        _get(self.port, C.PATH_SHARE, "POST", {"url": YOUTUBE})
        _wait_for(self.port, lambda d: d["state"] == C.STATE_PLAYING)
        path = os.path.join(self.config.state_dir, "group.owner")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
        self.assertEqual(sorted(record), ["owner", "pid", "since"])
        self.assertEqual(record["owner"], "cast-sender")


class TestEntryPoint(TempDir, unittest.TestCase):
    """The real CLI in offline mode, which is the command the lab note records."""

    def test_once_mode_answers_status(self):
        from sender.service import main

        state = os.path.join(self.root, "state")
        evidence = os.path.join(self.root, "evidence")
        code = main(
            [
                "--offline",
                "--once",
                "--bind",
                "127.0.0.1",
                "--port",
                "0",
                "--state-dir",
                state,
                "--evidence-dir",
                evidence,
            ]
        )
        self.assertEqual(code, 0)
        logs = [name for name in os.listdir(evidence) if name.endswith(".jsonl")]
        self.assertTrue(logs, "the service must write its JSONL log")

    def test_offline_is_the_environment_default_for_tests(self):
        from sender.service import parse_args, resolve_offline

        original = os.environ.get("SENDER_OFFLINE")
        try:
            os.environ["SENDER_OFFLINE"] = "1"
            self.assertTrue(resolve_offline(parse_args([])))
            os.environ["SENDER_OFFLINE"] = ""
            self.assertFalse(resolve_offline(parse_args([])))
            self.assertTrue(resolve_offline(parse_args(["--offline"])))
        finally:
            if original is None:
                os.environ.pop("SENDER_OFFLINE", None)
            else:
                os.environ["SENDER_OFFLINE"] = original

    def test_cleanup_mode_undoes_our_record(self):
        """What ExecStopPost runs: it must act on our record and say what it did."""
        import contextlib
        import io

        from sender.group_lock import write_session_record
        from sender.service import main

        state = os.path.join(self.root, "state")
        record = os.path.join(state, "session.json")
        write_session_record(
            record,
            {
                "owner": "cast-sender",
                "pid": 1,
                "session_id": "sold",
                "sid": "s9",
                "group_prepared": True,
                "receiver_launched": True,
                "mutes": [],
            },
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(
                [
                    "--offline",
                    "--cleanup",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--state-dir",
                    state,
                    "--evidence-dir",
                    os.path.join(self.root, "evidence"),
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())["cleanup"]
        self.assertEqual(payload["actions"], ["group_stopped", "receiver_stopped", "proxy_closed"])
        self.assertFalse(os.path.exists(record))

    def test_cleanup_mode_does_nothing_without_a_record(self):
        from sender.service import main

        code = main(
            [
                "--offline",
                "--cleanup",
                "--bind",
                "127.0.0.1",
                "--port",
                "0",
                "--state-dir",
                os.path.join(self.root, "state"),
                "--evidence-dir",
                os.path.join(self.root, "evidence"),
            ]
        )
        self.assertEqual(code, 0)


def _wait_for(port: int, predicate, deadline: float = 5.0) -> dict:
    """Poll /api/status until ``predicate`` holds; a real thread needs real time."""
    import time

    end = time.time() + deadline
    status, doc = _get(port, C.PATH_STATUS)
    while not predicate(doc) and time.time() < end:
        time.sleep(0.02)
        status, doc = _get(port, C.PATH_STATUS)
    return doc


def _log(root: str):
    from sender.log import EventLog

    log = EventLog(directory=os.path.join(root, "evidence"))
    return log


if __name__ == "__main__":
    unittest.main()
