"""resolve() against a recorded extraction — no network, no live YouTube (task B, 3).

The fixture is one real yt-dlp answer (``fixtures/ytdlp_recorded.json``) with every URL parameter
that is a token replaced by a placeholder; it keeps the shapes that matter: an HLS variant that
must be refused, a 1080p and a 720p avc1, a 140 AAC and an Opus track.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.abspath(os.path.join(HERE, "..", "..", "lib"))
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from v020 import resolve as R  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "ytdlp_recorded.json")


def recorded() -> dict:
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


class Extractor:
    """Stands in for ``resolve._ytdlp_extract``: records calls, returns a fixed answer."""

    def __init__(
        self, info: dict | None = None, *, exc: BaseException | None = None, delay: float = 0.0
    ):
        self.info = info if info is not None else recorded()
        self.exc = exc
        self.delay = delay
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, timeout: float) -> dict:
        self.calls.append((url, timeout))
        if self.delay:
            time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return copy.deepcopy(self.info)


class Base(unittest.TestCase):
    def setUp(self):
        self._saved = R._ytdlp_extract

    def tearDown(self):
        R._ytdlp_extract = self._saved

    def use(self, extractor) -> Extractor:
        R._ytdlp_extract = extractor
        return extractor


# --- video_id_of: exact, never a prefix (A33) -----------------------------------------------


class TestVideoId(unittest.TestCase):
    def test_accepted_forms(self):
        cases = {
            "ai_gI0Jbr_E": "ai_gI0Jbr_E",
            "https://www.youtube.com/watch?v=ai_gI0Jbr_E": "ai_gI0Jbr_E",
            "http://youtube.com/watch?v=ai_gI0Jbr_E&list=PL123&index=2": "ai_gI0Jbr_E",
            "https://m.youtube.com/watch?app=desktop&v=ai_gI0Jbr_E": "ai_gI0Jbr_E",
            "https://music.youtube.com/watch?v=ai_gI0Jbr_E&feature=share": "ai_gI0Jbr_E",
            "https://youtu.be/ai_gI0Jbr_E": "ai_gI0Jbr_E",
            "https://youtu.be/ai_gI0Jbr_E?t=42": "ai_gI0Jbr_E",
            "https://www.youtube.com/shorts/ai_gI0Jbr_E": "ai_gI0Jbr_E",
            "https://www.youtube.com/embed/ai_gI0Jbr_E?autoplay=1": "ai_gI0Jbr_E",
            "https://www.youtube.com/live/ai_gI0Jbr_E": "ai_gI0Jbr_E",
            "https://www.youtube.com/shorts/ai_gI0Jbr_E/": "ai_gI0Jbr_E",
            "https://www.youtube-nocookie.com/embed/ai_gI0Jbr_E": "ai_gI0Jbr_E",
            "  https://youtu.be/ai_gI0Jbr_E  ": "ai_gI0Jbr_E",
        }
        for text, expected in cases.items():
            self.assertEqual(R.video_id_of(text), expected, text)

    def test_prefix_traps_rejected(self):
        """The A33 lesson: a predicate that accepts a prefix passes on a wrong string."""
        traps = [
            "ai_gI0Jbr_E.m4a",  # the exact failure of A33
            "ai_gI0Jbr_Ex",  # 12 chars
            "ai_gI0Jbr_",  # 10 chars
            "https://www.youtube.com/watch?v=ai_gI0Jbr_Ex",
            "https://www.youtube.com/watch?v=ai_gI0Jbr_E.m4a",
            "https://youtu.be/ai_gI0Jbr_E/extra",  # id then more path
            "https://youtu.be/ai_gI0Jbr_Ex",
            "https://www.youtube.com/watch?v=ai_gI0Jbr_E&v=abcdefghijk",  # two v=
            "https://www.youtube.com/watch?v=",
            "https://www.youtube.com/playlist?list=PL123",
            "https://example.com/watch?v=ai_gI0Jbr_E",
            "",
            "   ",
            None,
        ]
        for trap in traps:
            with self.assertRaises(R.ResolveError, msg=repr(trap)):
                R.video_id_of(trap)  # type: ignore[arg-type]


class TestRedact(unittest.TestCase):
    def test_token_never_survives(self):
        url = (
            "https://rr4---sn-cxaaj5o5q5-t0ay.googlevideo.com/videoplayback?expire=1793000373"
            "&ei=Va_eaqaf&ip=203.0.113.7&id=o-AH&itag=140&sig=SECRETSIGNATURE&lsig=X"
        )
        out = R.redact(url)
        self.assertEqual(out, "https://rr4---sn-cxaaj5o5q5-t0ay.googlevideo.com/videop…")
        for leak in ("expire", "sig", "SECRET", "203.0.113", "videoplayback"):
            self.assertNotIn(leak, out)

    def test_unparsable(self):
        self.assertEqual(R.redact("not a url"), "<unparsable-url>")


# --- selection ------------------------------------------------------------------------------


class TestResolve(Base):
    def test_best_avc1_and_aac_from_one_extraction(self):
        extractor = self.use(Extractor())
        media = R.resolve("https://www.youtube.com/watch?v=ai_gI0Jbr_E")
        self.assertEqual(len(extractor.calls), 1, "both tracks must come from one extraction")
        self.assertEqual(media.video_id, "ai_gI0Jbr_E")
        self.assertEqual(media.title, "Pink Floyd - Pulse")
        self.assertAlmostEqual(media.duration_s, 8630.0)
        # 1080p avc1 137, not the HLS variant 270 (same height, higher tbr, no byte range)
        self.assertEqual(media.video.itag, 137)
        self.assertEqual(media.video.height, 1080)
        self.assertEqual(media.video.codecs, "avc1.640028")
        self.assertEqual(media.video.content_type, "video/mp4")
        self.assertEqual(media.audio.itag, 140)
        self.assertEqual(media.audio.codecs, "mp4a.40.2")
        self.assertEqual(media.audio.content_type, "audio/mp4")
        self.assertTrue(media.video.url.startswith("https://"))
        self.assertIn("itag=137", media.video.url)
        self.assertIn("itag=140", media.audio.url)

    def test_max_height_caps_the_video_track(self):
        self.use(Extractor())
        media = R.resolve("ai_gI0Jbr_E", max_height=720)
        self.assertEqual(media.video.height, 720)
        self.assertEqual(media.video.itag, 136)
        self.assertEqual(media.audio.itag, 140)  # audio untouched by the cap

    def test_extractor_ranges_are_kept_and_absent_by_default(self):
        info = recorded()
        for fmt in info["formats"]:
            if fmt["format_id"] == "137":
                fmt["init_range"] = [0, 741]
                fmt["index_range"] = [742, 19901]
        self.use(Extractor(info))
        media = R.resolve("ai_gI0Jbr_E")
        self.assertEqual(media.video.init_range, (0, 741))
        self.assertEqual(media.video.index_range, (742, 19901))
        self.use(Extractor())
        media = R.resolve("ai_gI0Jbr_E")
        self.assertIsNone(media.video.init_range)  # the proxy discovers them instead
        self.assertIsNone(media.video.index_range)

    def test_expiry_is_the_earliest_source_token(self):
        info = recorded()
        for fmt in info["formats"]:
            if fmt["format_id"] in ("137", "140"):
                fmt["url"] = fmt["url"].replace("expire=1793000571", "expire=1793000571")
        info["formats"][0]["url"] = info["formats"][0]["url"].replace(
            "expire=1793000571", "expire=1793000000"
        )
        self.use(Extractor(info))
        media = R.resolve("ai_gI0Jbr_E")
        self.assertAlmostEqual(media.expires_at, 1793000000 - 60)
        self.assertGreaterEqual(media.fetched_at, time.time() - 5)

    def test_timeout_is_forwarded_and_enforced(self):
        extractor = self.use(Extractor(delay=0.6))
        with self.assertRaises(R.ResolveError) as caught:
            R.resolve("ai_gI0Jbr_E", timeout=0.2)
        self.assertIn("timed out", str(caught.exception))
        self.assertEqual(extractor.calls[0][1], 0.2)

    def test_live_and_upcoming_refused(self):
        for live in (
            {"is_live": True, "live_status": "is_live"},
            {"is_live": False, "live_status": "is_upcoming"},
            {"is_live": False, "live_status": "post_live"},
        ):
            info = recorded()
            info.update(live)
            self.use(Extractor(info))
            with self.assertRaises(R.ResolveError, msg=live):
                R.resolve("ai_gI0Jbr_E")

    def test_age_gate_refused(self):
        info = recorded()
        info["age_limit"] = 18
        self.use(Extractor(info))
        with self.assertRaises(R.ResolveError) as caught:
            R.resolve("ai_gI0Jbr_E")
        self.assertIn("age", str(caught.exception))

    def test_private_video_is_a_diagnosis_not_a_traceback(self):
        class DownloadError(Exception):
            pass

        exc = DownloadError(
            "ERROR: [youtube] ai_gI0Jbr_E: Private video. Sign in if you've "
            "been granted access. See https://rr1.googlevideo.com/v?expire=1"
        )
        self.use(Extractor(exc=exc))
        with self.assertRaises(R.ResolveError) as caught:
            R.resolve("ai_gI0Jbr_E")
        text = str(caught.exception)
        self.assertIn("Private video", text)
        self.assertNotIn("expire=1", text)  # a token in a message is a leak
        self.assertNotIn("googlevideo", text)

    def test_no_avc1_refused(self):
        info = recorded()
        info["formats"] = [
            f for f in info["formats"] if not str(f.get("vcodec") or "").startswith("avc1")
        ]
        self.use(Extractor(info))
        with self.assertRaises(R.ResolveError) as caught:
            R.resolve("ai_gI0Jbr_E")
        self.assertIn("avc1", str(caught.exception))

    def test_no_aac_refused(self):
        info = recorded()
        info["formats"] = [
            f for f in info["formats"] if not str(f.get("acodec") or "").startswith("mp4a")
        ]
        self.use(Extractor(info))
        with self.assertRaises(R.ResolveError) as caught:
            R.resolve("ai_gI0Jbr_E")
        self.assertIn("AAC", str(caught.exception))

    def test_height_cap_below_anything_available_is_refused(self):
        self.use(Extractor())
        with self.assertRaises(R.ResolveError) as caught:
            R.resolve("ai_gI0Jbr_E", max_height=240)
        self.assertIn("240", str(caught.exception))
        self.assertIn("720", str(caught.exception))  # says what is available

    def test_no_duration_refused(self):
        info = recorded()
        info.pop("duration")
        self.use(Extractor(info))
        with self.assertRaises(R.ResolveError):
            R.resolve("ai_gI0Jbr_E")

    def test_id_mismatch_refused(self):
        info = recorded()
        info["id"] = "QzomK1fdSUg"
        self.use(Extractor(info))
        with self.assertRaises(R.ResolveError) as caught:
            R.resolve("ai_gI0Jbr_E")
        self.assertIn("QzomK1fdSUg", str(caught.exception))

    def test_url_only_api_still_targets_the_watch_page(self):
        extractor = self.use(Extractor())
        R.resolve("https://youtu.be/ai_gI0Jbr_E?t=10")
        self.assertEqual(extractor.calls[0][0], "https://youtu.be/ai_gI0Jbr_E?t=10")

    def test_shape_is_the_contract_value_type(self):
        self.use(Extractor())
        media = R.resolve("ai_gI0Jbr_E")
        self.assertEqual(media.__class__.__name__, "ResolvedMedia")
        self.assertIsInstance(media.video.itag, int)
        self.assertIsInstance(media.audio.itag, int)


if __name__ == "__main__":
    unittest.main()
