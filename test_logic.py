"""Unit tests for Paattu Petti's pure logic — no Telegram credentials needed.

Run:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from queuestate import ChatState, Track          # noqa: E402
from storage import Storage                      # noqa: E402
from utils import (format_duration, find_youtube_url, is_playlist_url,  # noqa: E402
                   normalize_title, parse_dedication, titles_match,
                   votes_needed)
from youtube import track_from_info              # noqa: E402


def make_track(title="Song", user=1, name="Ashif", **kw):
    return Track(title=title, requester_id=user, requester_name=name, **kw)


class TestFormatDuration(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(245), "4:05")
        self.assertEqual(format_duration(3600), "1:00:00")
        self.assertEqual(format_duration(3723), "1:02:03")

    def test_edge(self):
        self.assertEqual(format_duration(None), "?:??")
        self.assertEqual(format_duration(-5), "0:00")
        self.assertEqual(format_duration("abc"), "?:??")


class TestTitles(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(
            normalize_title("Kesariya (Official Music Video) [HD]"),
            "kesariya")
        self.assertEqual(normalize_title("Arijit Singh - Tum Hi Ho"),
                         "arijit singh tum hi ho")

    def test_match(self):
        self.assertTrue(titles_match("kesariya", "Kesariya (Official Video)"))
        self.assertTrue(titles_match("Tum Hi Ho", "Tum Hi Ho - Aashiqui 2"))
        self.assertTrue(titles_match("tum hi hoo", "Tum Hi Ho"))
        self.assertFalse(titles_match("random words here", "Kesariya"))
        self.assertFalse(titles_match("", "Kesariya"))


class TestVotesNeeded(unittest.TestCase):
    def test_majority(self):
        self.assertEqual(votes_needed(5), 3)
        self.assertEqual(votes_needed(4), 3)
        self.assertEqual(votes_needed(2), 2)
        self.assertEqual(votes_needed(1), 1)

    def test_fallback(self):
        self.assertEqual(votes_needed(None), 3)
        self.assertEqual(votes_needed(0), 3)
        self.assertEqual(votes_needed("x"), 3)


class TestParsing(unittest.TestCase):
    def test_dedication(self):
        self.assertEqual(parse_dedication("kesariya for @adhi"),
                         ("kesariya", "@adhi"))
        self.assertEqual(parse_dedication("plain song"), ("plain song", None))
        self.assertEqual(parse_dedication("song for two people"),
                         ("song for two people", None))

    def test_url(self):
        self.assertEqual(find_youtube_url("play https://youtu.be/abc123 now"),
                         "https://youtu.be/abc123")
        self.assertIsNone(find_youtube_url("just a song name"))
        self.assertTrue(is_playlist_url(
            "https://www.youtube.com/playlist?list=PL123"))
        self.assertFalse(is_playlist_url(
            "https://www.youtube.com/watch?v=x&list=PL123"))


class TestQueueState(unittest.TestCase):
    def setUp(self):
        self.state = ChatState(chat_id=-100)

    def test_enqueue_positions_and_remove(self):
        a, b, c = make_track("A"), make_track("B"), make_track("C")
        self.assertEqual(self.state.enqueue(a), 1)
        self.assertEqual(self.state.enqueue(b), 2)
        self.assertEqual(self.state.enqueue(c), 3)
        self.assertIs(self.state.remove(2), b)
        self.assertEqual([t.title for t in self.state.queue], ["A", "C"])
        self.assertIsNone(self.state.remove(9))

    def test_shuffle_keeps_tracks(self):
        for i in range(10):
            self.state.enqueue(make_track(f"T{i}"))
        before = sorted(t.title for t in self.state.queue)
        self.state.shuffle_queue()
        self.assertEqual(sorted(t.title for t in self.state.queue), before)

    def test_advance_basic(self):
        a, b = make_track("A"), make_track("B")
        self.state.start_track(a)
        self.state.enqueue(b)
        self.assertIs(self.state.advance(), b)
        self.assertIsNone(self.state.advance())
        self.assertIsNone(self.state.current)

    def test_loop_track_replays_on_end_but_not_on_skip(self):
        a = make_track("A")
        self.state.start_track(a)
        self.state.loop_mode = "track"
        self.assertIs(self.state.advance(skipped=False), a)
        self.assertIsNone(self.state.advance(skipped=True))

    def test_loop_queue_rotates(self):
        a, b = make_track("A"), make_track("B")
        self.state.start_track(a)
        self.state.enqueue(b)
        self.state.loop_mode = "queue"
        self.assertIs(self.state.advance(), b)
        self.assertEqual([t.title for t in self.state.queue], ["A"])
        self.assertIs(self.state.advance(), a)

    def test_per_user_count(self):
        self.state.enqueue(make_track("A", user=7))
        self.state.enqueue(make_track("B", user=7))
        self.state.enqueue(make_track("C", user=8))
        self.assertEqual(self.state.queued_by(7), 2)
        self.assertEqual(self.state.queued_by(8), 1)

    def test_voteskip_dedupes(self):
        self.assertTrue(self.state.add_voteskip(1))
        self.assertFalse(self.state.add_voteskip(1))
        self.assertTrue(self.state.add_voteskip(2))

    def test_ratings_one_per_user(self):
        self.assertTrue(self.state.add_rating(1, "fire"))
        self.assertFalse(self.state.add_rating(1, "fire"))
        self.assertTrue(self.state.add_rating(1, "poop"))   # can change mind
        self.state.add_rating(2, "fire")
        self.assertEqual(self.state.rating_counts(), (1, 1))

    def test_votes_reset_on_track_change(self):
        a, b = make_track("A"), make_track("B")
        self.state.start_track(a)
        self.state.add_voteskip(1)
        self.state.add_rating(1, "fire")
        self.state.enqueue(b)
        self.state.advance()
        self.assertEqual(len(self.state.voteskip_voters), 0)
        self.assertEqual(self.state.rating_counts(), (0, 0))


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Storage(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_stats(self):
        t1 = make_track("Song One", user=1, name="Ashif", video_id="v1")
        t2 = make_track("Song Two", user=2, name="Adhi", video_id="v2")
        self.store.record_action(-100, t1, "play")
        self.store.record_action(-100, t1, "play")
        self.store.record_action(-100, t2, "play")
        self.store.record_action(-100, t2, "skip")
        self.assertEqual(self.store.top_requesters(-100),
                         [("Ashif", 2), ("Adhi", 1)])
        self.assertEqual(self.store.most_skipped(-100), [("Adhi", 1)])
        self.assertEqual(self.store.recent_tracks(-100), ["Song Two", "Song One"])

    def test_guess_points(self):
        self.assertEqual(self.store.add_guess_point(-100, 5, "Zyn"), 1)
        self.assertEqual(self.store.add_guess_point(-100, 5, "Zyn"), 2)
        self.store.add_guess_point(-100, 6, "Adhi")
        self.assertEqual(self.store.guess_leaderboard(-100),
                         [("Zyn", 2), ("Adhi", 1)])

    def test_settings(self):
        self.assertEqual(self.store.get_setting(-100, "controls", "everyone"),
                         "everyone")
        self.store.set_setting(-100, "controls", "admins")
        self.assertEqual(self.store.get_setting(-100, "controls"), "admins")

    def test_rating_record(self):
        t1 = make_track("Song One", video_id="v1")
        self.store.record_rating(-100, t1, 9, "fire")   # must not raise


class TestTrackFromInfo(unittest.TestCase):
    def test_full_info(self):
        info = {"id": "abc", "title": "Kesariya", "duration": 272,
                "uploader": "Sony Music", "thumbnail": "http://img",
                "webpage_url": "https://youtu.be/abc"}
        track = track_from_info(info, 1, "Ashif", dedication="@adhi")
        self.assertEqual(track.title, "Kesariya")
        self.assertEqual(track.duration, 272)
        self.assertEqual(track.video_id, "abc")
        self.assertEqual(track.dedication, "@adhi")
        self.assertEqual(track.source, "youtube")

    def test_minimal_info(self):
        track = track_from_info({"id": "x", "title": "T"}, 1, "A")
        self.assertEqual(track.webpage_url,
                         "https://www.youtube.com/watch?v=x")
        self.assertEqual(track.duration, 0)


if __name__ == "__main__":
    unittest.main()
