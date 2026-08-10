# tests/test_turns.py
"""Per-session last_turn bookkeeping and the widget's staleness decision."""
import unittest
from datetime import datetime, timezone

from cost_meter.summary import (
    STALE_AFTER_SECONDS,
    format_age,
    new_turn_ids,
    prune_marks,
    staleness,
)


def event(ts, msg_id, session="s1"):
    return [ts, msg_id, session, "claude-opus-5", 0, 1_000_000, 0, 0, 0]


class TestNewTurnIds(unittest.TestCase):
    def test_first_run_of_a_session_counts_everything_it_has(self):
        events = [event(100.0, "a"), event(101.0, "b")]
        ids, mark = new_turn_ids(events, "s1", None)
        self.assertEqual(ids, {"a", "b"})
        self.assertEqual(mark, {"ts": 101.0, "ids": ["b"]})

    def test_a_second_run_with_nothing_new_reports_no_turn(self):
        events = [event(100.0, "a"), event(101.0, "b")]
        _, mark = new_turn_ids(events, "s1", None)
        ids, again = new_turn_ids(events, "s1", mark)
        self.assertEqual(ids, set())
        self.assertEqual(again, mark)

    def test_only_events_after_the_bookmark_count(self):
        first = [event(100.0, "a")]
        _, mark = new_turn_ids(first, "s1", None)
        ids, _ = new_turn_ids(first + [event(200.0, "b")], "s1", mark)
        self.assertEqual(ids, {"b"})

    def test_events_sharing_the_bookmark_timestamp_are_not_counted_twice(self):
        # Two messages on the identical timestamp: the first run counts both, the
        # second must count neither. A bare `ts > mark` test would re-count one.
        events = [event(100.0, "a"), event(100.0, "b")]
        ids, mark = new_turn_ids(events, "s1", None)
        self.assertEqual(ids, {"a", "b"})
        self.assertEqual(mark["ids"], ["a", "b"])
        again, _ = new_turn_ids(events, "s1", mark)
        self.assertEqual(again, set())

    def test_a_new_event_on_the_bookmark_timestamp_still_counts(self):
        events = [event(100.0, "a")]
        _, mark = new_turn_ids(events, "s1", None)
        ids, _ = new_turn_ids(events + [event(100.0, "b")], "s1", mark)
        self.assertEqual(ids, {"b"})

    def test_two_interleaved_sessions_each_see_only_their_own_turn(self):
        events = [event(100.0, "a1"), event(101.0, "b1", session="s2")]
        _, mark_a = new_turn_ids(events, "s1", None)
        _, mark_b = new_turn_ids(events, "s2", None)

        events += [event(200.0, "a2"), event(201.0, "b2", session="s2")]
        ids_a, _ = new_turn_ids(events, "s1", mark_a)
        ids_b, _ = new_turn_ids(events, "s2", mark_b)
        self.assertEqual(ids_a, {"a2"})
        self.assertEqual(ids_b, {"b2"})

    def test_events_appended_by_another_sessions_run_still_count_as_ours(self):
        # The exact failure mode: session B's hook fires first and its scan
        # absorbs A's new message too. A's next run must still see it.
        events = [event(100.0, "a1")]
        _, mark_a = new_turn_ids(events, "s1", None)

        # B's run appends both sessions' new messages, then computes its own turn.
        events += [event(200.0, "a2"), event(201.0, "b1", session="s2")]
        ids_b, _ = new_turn_ids(events, "s2", None)
        self.assertEqual(ids_b, {"b1"})

        ids_a, _ = new_turn_ids(events, "s1", mark_a)
        self.assertEqual(ids_a, {"a2"})

    def test_an_unknown_session_id_yields_no_turn_and_no_bookmark(self):
        ids, mark = new_turn_ids([event(100.0, "a")], "s9", None)
        self.assertEqual(ids, set())
        self.assertIsNone(mark)

    def test_a_missing_session_id_is_not_bookmarked(self):
        # calibrate.py refreshes with session_id="" and must not claim a turn.
        ids, mark = new_turn_ids([event(100.0, "a")], "", None)
        self.assertEqual(ids, set())
        self.assertIsNone(mark)

    def test_pruned_events_do_not_rewind_the_bookmark(self):
        events = [event(100.0, "a"), event(200.0, "b")]
        _, mark = new_turn_ids(events, "s1", None)
        # Everything counted has been pruned away except an older leftover.
        ids, kept = new_turn_ids([event(100.0, "a")], "s1", mark)
        self.assertEqual(ids, set())
        self.assertEqual(kept, mark)


class TestPruneMarks(unittest.TestCase):
    def test_old_sessions_are_dropped_and_recent_ones_kept(self):
        marks = {"old": {"ts": 100.0, "ids": ["a"]},
                 "new": {"ts": 500.0, "ids": ["b"]}}
        self.assertEqual(prune_marks(marks, 400.0), {"new": marks["new"]})

    def test_a_corrupt_entry_is_dropped_rather_than_raising(self):
        self.assertEqual(prune_marks({"bad": "not a dict"}, 0.0), {})


class TestStaleness(unittest.TestCase):
    def state(self, epoch):
        stamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        return {"updated_at": stamp}

    def test_a_fresh_state_is_not_stale(self):
        stale, age = staleness(self.state(1000.0), 1060.0)
        self.assertFalse(stale)
        self.assertAlmostEqual(age, 60.0)

    def test_a_state_just_inside_the_threshold_is_not_stale(self):
        stale, _ = staleness(self.state(1000.0), 1000.0 + STALE_AFTER_SECONDS)
        self.assertFalse(stale)

    def test_a_state_past_the_threshold_is_stale(self):
        stale, age = staleness(self.state(1000.0), 1000.0 + STALE_AFTER_SECONDS + 1)
        self.assertTrue(stale)
        self.assertAlmostEqual(age, STALE_AFTER_SECONDS + 1)

    def test_the_threshold_is_overridable(self):
        stale, _ = staleness(self.state(1000.0), 1100.0, threshold=60)
        self.assertTrue(stale)

    def test_a_missing_timestamp_is_stale_with_an_unknown_age(self):
        stale, age = staleness({}, 1000.0)
        self.assertTrue(stale)
        self.assertIsNone(age)

    def test_an_unparseable_timestamp_is_stale_with_an_unknown_age(self):
        stale, age = staleness({"updated_at": "last tuesday"}, 1000.0)
        self.assertTrue(stale)
        self.assertIsNone(age)

    def test_a_timestamp_from_the_future_is_fresh_not_negative(self):
        stale, age = staleness(self.state(2000.0), 1000.0)
        self.assertFalse(stale)
        self.assertEqual(age, 0.0)

    def test_a_zulu_suffix_parses(self):
        # 2026-08-10T09:44:47Z is 1786355087.0; a plain fromisoformat call would
        # have rejected the Z before Python 3.11.
        stale, age = staleness({"updated_at": "2026-08-10T09:44:47Z"}, 1786355147.0)
        self.assertFalse(stale)
        self.assertEqual(age, 60.0)


class TestFormatAge(unittest.TestCase):
    def test_minutes(self):
        self.assertEqual(format_age(605.0), "10 min")

    def test_hours_and_minutes(self):
        self.assertEqual(format_age(3 * 3600 + 25 * 60), "3 h 25 min")

    def test_days_and_hours(self):
        self.assertEqual(format_age(2 * 86400 + 5 * 3600), "2 d 5 h")

    def test_unknown(self):
        self.assertEqual(format_age(None), "age unknown")


if __name__ == "__main__":
    unittest.main()
