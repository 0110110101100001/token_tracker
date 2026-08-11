# tests/test_summary.py
import unittest
from datetime import datetime

from cost_meter.summary import build_state, parse_updated_at

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

    def test_five_hour_window_excludes_the_previous_block(self):
        """The limit is a fixed block, not a trailing five hours.

        The block opens on the first message after the previous one expired and
        runs five hours from there, so spend from the previous block does not
        follow you into this one even while it is still less than five hours old.
        Counting it trailing-style is what made the calibration drift: the same
        reported percentage divided by an inflated dollar figure produced a
        ceiling that changed every time the two blocks overlapped differently.
        """
        events = [
            event(self.now - 6 * 3600, "block-a-open"),   # opens block A
            event(self.now - 90 * 60, "block-a-tail"),    # still inside block A
            event(self.now - 30 * 60, "block-b-open"),    # block A expired: opens B
        ]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        # A trailing window would also count block-a-tail, reading 50.00.
        self.assertAlmostEqual(state["window_5h"]["usd"], 25.0)

    def test_five_hour_window_reports_when_the_block_resets(self):
        events = [event(self.now - 6 * 3600, "a"), event(self.now - 30 * 60, "b")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        resets_at = parse_updated_at(state["window_5h"]["resets_at"])
        self.assertAlmostEqual(resets_at, self.now - 30 * 60 + 5 * 3600, places=3)

    def test_five_hour_window_is_zero_once_the_block_has_expired(self):
        """No block is open, so nothing is counted against the limit yet.

        Reporting the expired block's spend would keep the row red long after
        the limit had actually reset, which is the opposite of the mistake the
        trailing window made.
        """
        events = [event(self.now - 6 * 3600, "a")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertAlmostEqual(state["window_5h"]["usd"], 0.0)
        self.assertIsNone(state["window_5h"]["resets_at"])

    def test_blocks_chain_from_the_oldest_event_not_from_now(self):
        """Where the open block starts depends on the whole chain before it.

        Two messages 30 minutes apart open one block, not two: the second falls
        inside the first's five hours. Anchoring on the most recent gap instead
        would put the reset 30 minutes late and undercount the block.
        """
        events = [
            event(self.now - 3 * 3600, "open"),
            event(self.now - 150 * 60, "same-block"),
        ]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertAlmostEqual(state["window_5h"]["usd"], 50.0)
        resets_at = parse_updated_at(state["window_5h"]["resets_at"])
        self.assertAlmostEqual(resets_at, self.now - 3 * 3600 + 5 * 3600, places=3)

    def test_out_of_order_events_do_not_break_block_chaining(self):
        """events.jsonl is append-ordered per scan, not globally sorted.

        With several sessions running, one hook can append a batch that predates
        what another already wrote, so the chain has to sort before walking it.
        """
        events = [
            event(self.now - 30 * 60, "block-b-open"),
            event(self.now - 6 * 3600, "block-a-open"),
            event(self.now - 90 * 60, "block-a-tail"),
        ]
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
