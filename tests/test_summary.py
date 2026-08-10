# tests/test_summary.py
import unittest
from datetime import datetime

from cost_meter.summary import build_state

PRICING = {"claude-opus-5": {"input": 5.0, "output": 25.0}}
NO_CAL = {"ceiling_5h_usd": None, "ceiling_7d_usd": None}


def event(ts, msg_id, session="s1", model="claude-opus-5", out=1_000_000):
    return [ts, msg_id, session, model, 0, out, 0, 0, 0]


class TestBuildState(unittest.TestCase):
    def setUp(self):
        # Midday local time, so "today" never straddles a midnight boundary.
        self.now = datetime.now().replace(hour=12, minute=0, second=0,
                                          microsecond=0).timestamp()

    def test_session_total_covers_only_that_session(self):
        events = [event(self.now - 60, "a"), event(self.now - 60, "b", session="s2")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertAlmostEqual(state["session"]["usd"], 25.0)

    def test_last_turn_covers_only_new_events_in_this_session(self):
        events = [
            event(self.now - 60, "old"),
            event(self.now - 5, "new"),
            event(self.now - 5, "other", session="s2"),
        ]
        state = build_state(events, PRICING, "s1", {"new", "other"}, self.now, NO_CAL)
        self.assertAlmostEqual(state["last_turn_usd"], 25.0)

    def test_five_hour_window_excludes_older_events(self):
        events = [event(self.now - 6 * 3600, "old"), event(self.now - 60, "new")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertAlmostEqual(state["window_5h"]["usd"], 25.0)

    def test_seven_day_window_excludes_older_events(self):
        events = [event(self.now - 8 * 86400, "old"), event(self.now - 60, "new")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertAlmostEqual(state["window_7d"]["usd"], 25.0)

    def test_pct_is_none_without_calibration(self):
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, NO_CAL)
        self.assertIsNone(state["window_5h"]["pct"])

    def test_pct_uses_the_calibrated_ceiling(self):
        cal = {"ceiling_5h_usd": 50.0, "ceiling_7d_usd": None}
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, cal)
        self.assertEqual(state["window_5h"]["pct"], 50)

    def test_unknown_model_is_reported_and_not_priced_as_zero(self):
        events = [event(self.now, "a", model="claude-nonexistent-9")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertEqual(state["unknown_models"], ["claude-nonexistent-9"])
        self.assertAlmostEqual(state["session"]["usd"], 0.0)

    def test_today_spans_sessions_but_not_yesterday(self):
        events = [
            event(self.now - 30 * 3600, "yesterday"),
            event(self.now - 60, "a"),
            event(self.now - 60, "b", session="s2"),
        ]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertAlmostEqual(state["today_usd"], 50.0)

    def test_cache_fields_are_priced_at_their_own_multipliers(self):
        # Distinct counts per bucket so a swapped multiplier cannot cancel out.
        events = [[self.now, "a", "s1", "claude-opus-5", 0, 0, 1_000_000, 2_000_000, 4_000_000]]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        expected = 5.0 * 1.25 + 2 * 5.0 * 2.0 + 4 * 5.0 * 0.1
        self.assertAlmostEqual(state["session"]["usd"], expected)

    def test_empty_event_list_produces_zeroed_state(self):
        state = build_state([], PRICING, "s1", set(), self.now, NO_CAL)
        self.assertEqual(state["session"]["usd"], 0.0)
        self.assertEqual(state["today_usd"], 0.0)
        self.assertEqual(state["unknown_models"], [])


if __name__ == "__main__":
    unittest.main()
