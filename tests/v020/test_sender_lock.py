"""Group ownership and boot cleanup — contracts §7 and §8.

Three mechanisms are tested against the real filesystem, because that is where they fail:
``O_EXCL`` creation, staleness read from the mtime (the record keeps its three documented keys),
and a cleanup that must touch nothing it did not record.
"""

from __future__ import annotations

import json
import os
import time
import unittest

from test_sender_support import TempDir, read_log

from sender.group_lock import (
    GroupLock, boot_cleanup, read_session_record, session_record_path, write_session_record,
)
from sender.log import EventLog
from v020.contracts import OWNER_RELAY, OWNER_SENDER


class LockCase(TempDir, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.root, "state", "group.owner")
        self.log = EventLog(path=os.path.join(self.root, "log.jsonl"))
        self.addCleanup(self.log.close)
        self.lock = GroupLock(self.path, log=self.log, pid=4242)

    def age(self, seconds: float) -> None:
        """Backdate the heartbeat, which is the file's mtime."""
        stamp = time.time() - seconds
        os.utime(self.path, (stamp, stamp))

    def body(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)


class TestAcquire(LockCase):
    def test_creates_the_three_documented_keys(self):
        self.assertTrue(self.lock.acquire())
        self.assertEqual(self.body(), {"owner": OWNER_SENDER, "pid": 4242,
                                       "since": self.lock.read()["since"]})
        self.assertEqual(sorted(self.body()), ["owner", "pid", "since"])
        self.assertEqual(self.lock.owner_name(), OWNER_SENDER)

    def test_idempotent_for_the_same_owner(self):
        self.assertTrue(self.lock.acquire())
        before = os.stat(self.path).st_mtime
        time.sleep(0.01)
        self.assertTrue(self.lock.acquire())
        self.assertGreaterEqual(os.stat(self.path).st_mtime, before)

    def test_a_fresh_foreign_lock_is_respected(self):
        GroupLock(self.path, owner="cast-sender", pid=9999).acquire()
        contender = GroupLock(self.path, pid=4242, log=self.log)
        self.assertFalse(contender.acquire())
        self.assertEqual(self.body()["pid"], 9999)
        self.assertTrue([e for e in read_log(self.log.path) if e["event"] == "lock_contended"])

    def test_a_stale_lock_is_taken_over_and_logged(self):
        GroupLock(self.path, owner="cast-sender", pid=9999).acquire()
        self.age(20.0)                                   # > OWNER_STALE_S (15 s)
        self.assertTrue(self.lock.acquire())
        self.assertEqual(self.body()["pid"], 4242)
        takeover = [e for e in read_log(self.log.path) if e["event"] == "lock_takeover"]
        self.assertEqual(len(takeover), 1)
        self.assertEqual(takeover[0]["reason"], "stale")
        self.assertGreaterEqual(takeover[0]["age_s"], 15.0)

    def test_a_fresh_relay_lock_is_preempted(self):
        """§7: the v0.2 owner always wins, because its lock comes from an explicit share."""
        relay = GroupLock(self.path, owner=OWNER_RELAY, pid=1234)
        self.assertTrue(relay.acquire())
        self.assertTrue(self.lock.acquire())
        self.assertEqual(self.body()["owner"], OWNER_SENDER)
        takeover = [e for e in read_log(self.log.path) if e["event"] == "lock_takeover"]
        self.assertEqual(takeover[0]["reason"], "owner_wins")

    def test_no_takeover_just_below_the_threshold(self):
        GroupLock(self.path, owner="cast-sender", pid=9999).acquire()
        self.age(14.0)
        self.assertFalse(self.lock.acquire())

    def test_release_only_when_ours(self):
        self.assertTrue(self.lock.acquire())
        other = GroupLock(self.path, pid=7777, log=self.log)
        self.assertFalse(other.release())
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(self.lock.release())
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue([e for e in read_log(self.log.path)
                         if e["event"] == "lock_release_skipped"])

    def test_heartbeat_refreshes_and_notices_a_loss(self):
        self.assertTrue(self.lock.acquire())
        self.age(12.0)
        self.assertTrue(self.lock.heartbeat())
        self.assertLess(self.lock.age(), 1.0)
        # someone else takes it while our heartbeat is running: we must notice, not fight
        self.age(20.0)
        self.assertTrue(GroupLock(self.path, pid=5555).acquire())
        self.assertFalse(self.lock.heartbeat())
        self.assertTrue([e for e in read_log(self.log.path) if e["event"] == "lock_lost"])

    def test_heartbeat_thread_keeps_the_file_fresh(self):
        lock = GroupLock(self.path, pid=4243, log=self.log, heartbeat_s=0.05)
        self.assertTrue(lock.acquire())
        self.age(9.0)
        lock.start_heartbeat()
        try:
            deadline = time.time() + 2.0
            while time.time() < deadline and (lock.age() or 99) > 1.0:
                time.sleep(0.02)
            self.assertLess(lock.age(), 1.0)
        finally:
            lock.stop_heartbeat()
            lock.release()


class TestBootCleanup(TempDir, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = session_record_path(os.path.join(self.root, "state"))
        self.log = EventLog(path=os.path.join(self.root, "log.jsonl"))
        self.addCleanup(self.log.close)

    def _seams(self):
        from test_sender_support import seams
        link, group, proxy, _resolver = seams()
        return link, group, proxy

    def _record(self, **overrides) -> dict:
        record = {
            "owner": OWNER_SENDER,
            "pid": 4242,
            "session_id": "s1234567890",
            "sid": "s1",
            "video_id": "QzomK1fdSUg",
            "mode": "normal",
            "group_prepared": True,
            "receiver_launched": True,
            "started_at": 1000.0,
            "mutes": [],
        }
        record.update(overrides)
        write_session_record(self.path, record)
        return record

    def test_nothing_recorded_touches_nothing(self):
        link, group, proxy = self._seams()
        result = boot_cleanup(self.path, group=group, link=link, proxy=proxy, log=self.log)
        self.assertEqual(result["actions"], [])
        self.assertEqual(group.calls, [])
        self.assertEqual(link.calls, [])
        self.assertTrue([e for e in read_log(self.log.path)
                         if e["event"] == "boot_cleanup_nothing_recorded"])

    def test_our_record_is_undone_in_order(self):
        self._record()
        link, group, proxy = self._seams()
        proxy.sessions["s1"] = object()
        result = boot_cleanup(self.path, group=group, link=link, proxy=proxy, log=self.log)
        self.assertEqual(result["actions"], ["group_stopped", "receiver_stopped", "proxy_closed"])
        self.assertEqual(group.calls, ["stop"])
        self.assertEqual(link.calls, ["stop"])
        self.assertNotIn("s1", proxy.sessions)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue([e for e in read_log(self.log.path)
                         if e["event"] == "boot_cleanup_done"])

    def test_only_our_mutes_are_undone(self):
        self._record(mutes=[
            {"name": "Cuisine", "level": 0.35, "muted": False, "by": OWNER_SENDER},
            {"name": "Cuisine", "level": 0.5, "muted": True, "by": OWNER_RELAY},
        ])
        link, group, proxy = self._seams()
        result = boot_cleanup(self.path, group=group, link=link, proxy=proxy, log=self.log)
        self.assertEqual(result["actions"][-1], "mute_undone:Cuisine")
        self.assertEqual(group.calls.count("volume"), 1)
        self.assertEqual((group.volume, group.muted), (0.35, False))

    def test_a_foreign_record_is_left_alone(self):
        self._record(owner=OWNER_RELAY)
        link, group, proxy = self._seams()
        result = boot_cleanup(self.path, group=group, link=link, proxy=proxy, log=self.log)
        self.assertEqual(result["actions"], [])
        self.assertEqual(group.calls, [])
        self.assertEqual(link.calls, [])
        self.assertTrue(os.path.exists(self.path))       # never deleted by the wrong owner
        self.assertEqual(read_session_record(self.path)["owner"], OWNER_RELAY)

    def test_a_session_that_never_prepared_the_group_is_not_stopped(self):
        self._record(group_prepared=False, receiver_launched=False, sid=None)
        link, group, proxy = self._seams()
        result = boot_cleanup(self.path, group=group, link=link, proxy=proxy, log=self.log)
        self.assertEqual(result["actions"], [])
        self.assertEqual(group.calls, [])

    def test_a_broken_seam_is_reported_not_raised(self):
        self._record()

        class Broken:
            def stop(self):
                raise RuntimeError("no route to host")

            def send(self, *_a, **_k):
                raise RuntimeError("closed")

            def close_session(self, _sid):
                raise RuntimeError("gone")

        result = boot_cleanup(self.path, group=Broken(), link=Broken(), proxy=Broken(),
                              log=self.log)
        self.assertEqual(result["actions"], ["group_stop_failed:RuntimeError",
                                            "receiver_stop_failed:RuntimeError",
                                            "proxy_close_failed:RuntimeError"])
        self.assertEqual(len([e for e in read_log(self.log.path)
                              if e["event"] == "boot_cleanup_error"]), 3)


if __name__ == "__main__":
    unittest.main()
