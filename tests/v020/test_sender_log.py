"""The JSONL log — contracts §9 and the redaction rule of §4.

A googlevideo URL is a short-lived token: the test that matters is that a raw one never reaches
the file, whatever shape it is nested in.
"""

from __future__ import annotations

import json
import os
import unittest

from test_sender_support import TempDir, read_log

from sender.log import EventLog, NullLog, redact_value

TOKEN = "https://rr1---sn-fake.googlevideo.com/videoplayback?expire=99&token=SECRET-TOKEN"


class TestRedaction(unittest.TestCase):
    def test_url_becomes_scheme_host_six_chars(self):
        redacted = redact_value(TOKEN)
        # resolve.redact(): scheme://host/<first 6 chars of the path>…
        self.assertEqual(redacted, "https://rr1---sn-fake.googlevideo.com/videop…")
        self.assertNotIn("SECRET-TOKEN", redacted)

    def test_nested_structures(self):
        value = {"a": [{"url": TOKEN}], "b": {"keep": 3.5, "deep": ("x", TOKEN)}}
        out = redact_value(value)
        self.assertNotIn("SECRET-TOKEN", json.dumps(out))
        self.assertEqual(out["b"]["keep"], 3.5)

    def test_a_url_named_field_is_redacted_even_without_a_scheme(self):
        out = redact_value("dQw4w9WgXcQ" * 4, "audio_url")
        self.assertTrue(out.startswith("<redacted:"))

    def test_plain_values_survive(self):
        self.assertEqual(redact_value("QzomK1fdSUg"), "QzomK1fdSUg")
        self.assertEqual(redact_value(12), 12)
        self.assertIsNone(redact_value(float("nan")))


class TestEventLog(TempDir, unittest.TestCase):
    def test_one_json_object_per_line(self):
        path = os.path.join(self.root, "log.jsonl")
        log = EventLog(path=path)
        log.event("alpha", n=1)
        log.event("beta", n=2, url=TOKEN)
        log.close()
        events = read_log(path)
        self.assertEqual([e["event"] for e in events], ["alpha", "beta"])
        self.assertEqual(events[1]["n"], 2)
        for event in events:
            self.assertIn("t", event)
            self.assertIn("mono", event)

    def test_urls_never_reach_the_file(self):
        path = os.path.join(self.root, "log.jsonl")
        log = EventLog(path=path)
        log.event("media", url=TOKEN, nested={"audio_url": TOKEN})
        log.close()
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        self.assertNotIn("SECRET-TOKEN", raw)
        self.assertIn("googlevideo.com", raw)

    def test_last_event_and_callable(self):
        path = os.path.join(self.root, "log.jsonl")
        log = EventLog(path=path)
        log("from_the_seam", detail="x")          # seams take a plain callable
        self.assertEqual(log.last_event, "from_the_seam")
        self.assertEqual(log.last["detail"], "x")
        self.assertEqual(log.count, 1)
        log.close()

    def test_appends(self):
        path = os.path.join(self.root, "log.jsonl")
        first = EventLog(path=path)
        first.event("one")
        first.close()
        second = EventLog(path=path)
        second.event("two")
        second.close()
        self.assertEqual([e["event"] for e in read_log(path)], ["one", "two"])

    def test_default_path_is_dated_evidence(self):
        log = EventLog(directory=os.path.join(self.root, "evidence"))
        self.assertEqual(os.path.dirname(log.path), os.path.join(self.root, "evidence"))
        self.assertRegex(os.path.basename(log.path), r"^sender-\d{4}-\d{2}-\d{2}\.jsonl$")
        log.close()


class TestNullLog(unittest.TestCase):
    def test_remembers_without_writing(self):
        log = NullLog()
        log.event("x", url=TOKEN)
        log("y")
        self.assertEqual(log.last_event, "y")
        self.assertEqual(log.records[0]["url"], redact_value(TOKEN))
        self.assertEqual(log.count, 2)


if __name__ == "__main__":
    unittest.main()
