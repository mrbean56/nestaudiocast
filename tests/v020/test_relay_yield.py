"""Task H — the v0.1 relay must read `state/group.owner` and yield the group to v0.2.

These tests drive the parser only (`relay/cast_relay.py:read_group_owner`): no relay process,
no speaker, no group, no Chromecast, no TV. The lock file is created, aged and malformed inside
a temporary directory, with an explicit `now=` so freshness never depends on the wall clock.

Covered (the five the task asks for, plus the contract's heartbeat rule):
  fresh lock · stale lock · malformed file · absent file · a lock owned by ourselves ·
  mtime-only heartbeat · clock-skewed `since` · and "reading never writes".
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RELAY = os.path.join(ROOT, "relay")
if RELAY not in sys.path:
    sys.path.insert(0, RELAY)

import cast_relay as R  # noqa: E402


class OwnerFile(unittest.TestCase):
    now = 1_000_000.0          # a fixed clock: freshness is a parsed value, not the real time

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "group.owner")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, doc=None, *, age_s=1.0, raw=None):
        """Write the lock and back-date its mtime by `age_s` (the heartbeat)."""
        text = raw if raw is not None else json.dumps(doc)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)
        t = self.now - age_s
        os.utime(self.path, (t, t))

    def read(self, **kw):
        return R.read_group_owner(self.path, now=self.now, **kw)

    # ---- the contract's numbers (docs/v020-contracts.md §7) ------------------------------
    def test_contract_constants(self):
        self.assertEqual(R.OWNER_SENDER, "cast-sender")
        self.assertEqual(R.OWNER_RELAY, "cast-relay")
        self.assertEqual(R.OWNER_STALE_S, 15.0)     # heartbeat 5 s, stale after 15 s

    # ---- 1. fresh lock ----------------------------------------------------------------
    def test_fresh_v020_lock_yields_the_group(self):
        self.write({"owner": "cast-sender", "pid": 4242, "since": self.now - 3.0}, age_s=1.0)
        o = self.read()
        self.assertTrue(o.present)
        self.assertTrue(o.ok)
        self.assertEqual(o.owner, "cast-sender")
        self.assertEqual(o.pid, 4242)
        self.assertEqual(o.since, self.now - 3.0)
        self.assertFalse(o.stale)
        self.assertEqual(o.age_s, 1.0)              # the heartbeat, from the mtime
        self.assertTrue(o.v020)
        self.assertFalse(o.self_owned)
        self.assertEqual(o.reason, "fresh")

    def test_staleness_boundary_is_fifteen_seconds(self):
        self.write({"owner": "cast-sender", "pid": 1, "since": self.now - 14.9}, age_s=14.9)
        self.assertTrue(self.read().v020)                       # still live
        self.write({"owner": "cast-sender", "pid": 1, "since": self.now - 15.1}, age_s=15.1)
        o = self.read()
        self.assertTrue(o.ok)
        self.assertTrue(o.stale)
        self.assertFalse(o.v020, "a lock older than 15 s must not stop the relay")

    def test_mtime_heartbeat_keeps_an_old_since_fresh(self):
        # contracts §7: the file is *refreshed* as a heartbeat; `since` may keep the creation
        # time. A rewrite is a heartbeat, so the mtime decides.
        self.write({"owner": "cast-sender", "pid": 7, "since": self.now - 600.0}, age_s=2.0)
        o = self.read()
        self.assertFalse(o.stale)
        self.assertTrue(o.v020)
        self.assertEqual(o.age_s, 2.0)

    def test_future_since_is_treated_as_fresh(self):
        # Clock skew, not ownership: staleness cannot be proven from a beat in the future, and
        # the safe direction is to leave the group alone.
        self.write({"owner": "cast-sender", "pid": 8, "since": self.now + 30.0}, age_s=60.0)
        o = self.read()
        self.assertFalse(o.stale)
        self.assertTrue(o.v020)

    # ---- 2. stale lock ---------------------------------------------------------------
    def test_stale_lock_hands_the_group_back(self):
        self.write({"owner": "cast-sender", "pid": 4242, "since": self.now - 40.0}, age_s=40.0)
        o = self.read()
        self.assertTrue(o.present)
        self.assertTrue(o.ok)
        self.assertTrue(o.stale)
        self.assertEqual(o.reason, "stale")
        self.assertFalse(o.v020, "a stale lock is a takeover opportunity, not an owner")

    # ---- 3. malformed file -----------------------------------------------------------
    def test_malformed_files_are_not_ownership(self):
        cases = {
            "not json": dict(raw="{owner: cast-sender"),
            "not an object": dict(raw='["cast-sender", 1, 0.0]'),
            "unknown owner": dict(doc={"owner": "someone-else", "pid": 1, "since": self.now}),
            "missing pid": dict(doc={"owner": "cast-sender", "since": self.now}),
            "pid is a string": dict(doc={"owner": "cast-sender", "pid": "1", "since": self.now}),
            "pid is a bool": dict(doc={"owner": "cast-sender", "pid": True, "since": self.now}),
            "missing since": dict(doc={"owner": "cast-sender", "pid": 1}),
            "since is a string": dict(doc={"owner": "cast-sender", "pid": 1, "since": "now"}),
            "empty file": dict(raw=""),
        }
        for label, kw in cases.items():
            with self.subTest(label):
                self.write(**kw)
                o = self.read()
                self.assertTrue(o.present)
                self.assertFalse(o.ok, label)
                self.assertTrue(o.stale, label)
                self.assertFalse(o.v020, label)
                self.assertFalse(o.self_owned, label)
                self.assertTrue(o.reason.startswith("malformed"), o.reason)

    def test_unreadable_path_is_not_ownership(self):
        # A directory where the file should be: the read fails, nothing is assumed.
        os.mkdir(self.path)
        o = self.read()
        self.assertFalse(o.ok)
        self.assertFalse(o.v020)
        self.assertTrue(o.reason.startswith("unreadable"), o.reason)
        os.rmdir(self.path)

    # ---- 4. absent file --------------------------------------------------------------
    def test_absent_file_means_v01_behaviour(self):
        o = self.read()
        self.assertFalse(o.present)
        self.assertFalse(o.ok)
        self.assertFalse(o.v020)
        self.assertTrue(o.stale)
        self.assertEqual(o.reason, "absent")
        self.assertFalse(os.path.exists(self.path), "reading must not create the lock")

    # ---- 5. a lock owned by ourselves ------------------------------------------------
    def test_our_own_lock_is_recognised_and_is_not_a_v020_owner(self):
        self.write({"owner": "cast-relay", "pid": 4242, "since": self.now - 2.0}, age_s=2.0)
        o = self.read(self_pid=4242)
        self.assertTrue(o.ok)
        self.assertTrue(o.self_owned)
        self.assertFalse(o.v020, "the relay must not yield to its own lock")
        self.assertEqual(o.owner, "cast-relay")
        self.assertEqual(o.reason, "fresh")

    def test_another_relay_pid_is_not_ours(self):
        self.write({"owner": "cast-relay", "pid": 4242, "since": self.now - 2.0}, age_s=2.0)
        o = self.read(self_pid=1)
        self.assertFalse(o.self_owned)
        self.assertFalse(o.v020)

    # ---- the relay is a reader, never a writer ---------------------------------------
    def test_reading_leaves_the_file_byte_identical(self):
        self.write({"owner": "cast-sender", "pid": 4242, "since": self.now - 3.0}, age_s=1.0)

        def snapshot():
            with open(self.path, "rb") as fh:
                return os.stat(self.path).st_mtime_ns, fh.read()

        before = snapshot()
        for _ in range(3):
            self.read()
        self.assertEqual(before, snapshot(), "contracts §7: only the owner writes the lock")


if __name__ == "__main__":
    unittest.main()
