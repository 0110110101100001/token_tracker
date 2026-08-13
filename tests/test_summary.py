# tests/test_summary.py
import unittest
from datetime import datetime, timezone

from cost_meter.summary import build_state, current_block

PRICING = {"claude-opus-5": {"input": 5.0, "output": 25.0}}
# No account figures available: what every row showed before this existed.
NO_LIMITS = None


def limits(pct_5h=11, pct_week=15, resets_at=None, age_s=60.0):
    """A utilization.read() result, as build_state now receives it."""
    return {"age_s": age_s, "rows": {
        "session": {"pct": pct_5h, "severity": "normal",
                    "resets_at": resets_at, "scope": None},
        "weekly_all": {"pct": pct_week, "severity": "normal",
                       "resets_at": None, "scope": None},
    }}


def event(ts, msg_id, session="s1", model="claude-opus-5", out=1_000_000):
    return [ts, msg_id, session, model, 0, out, 0, 0, 0]


class TestBuildState(unittest.TestCase):
    def setUp(self):
        # Midday local time, so "today" never straddles a midnight boundary.
        self.now = datetime.now().replace(hour=12, minute=0, second=0,
                                          microsecond=0).timestamp()

    def test_session_total_covers_only_that_session(self):
        events = [event(self.now - 60, "a"), event(self.now - 60, "b", session="s2")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_LIMITS)
        self.assertAlmostEqual(state["session"]["usd"], 25.0)

    def test_last_turn_covers_only_new_events_in_this_session(self):
        events = [
            event(self.now - 60, "old"),
            event(self.now - 5, "new"),
            event(self.now - 5, "other", session="s2"),
        ]
        state = build_state(events, PRICING, "s1", {"new", "other"}, self.now, NO_LIMITS)
        self.assertAlmostEqual(state["last_turn_usd"], 25.0)

    def test_five_hour_window_excludes_older_events(self):
        events = [event(self.now - 6 * 3600, "old"), event(self.now - 60, "new")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_LIMITS)
        self.assertAlmostEqual(state["window_5h"]["usd"], 25.0)

    def test_five_hour_window_excludes_the_previous_block(self):
        """The limit is a fixed block, not a trailing five hours.

        The block opens on the first message after the previous one expired and
        runs five hours from there, so spend from the previous block does not
        follow you into this one even while it is still less than five hours old.
        Counting it trailing-style inflates the figure by whatever the previous
        block spent in its last five hours, which is a different amount every
        time the two blocks overlap differently.
        """
        events = [
            event(self.now - 6 * 3600, "block-a-open"),   # opens block A
            event(self.now - 90 * 60, "block-a-tail"),    # still inside block A
            event(self.now - 30 * 60, "block-b-open"),    # block A expired: opens B
        ]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_LIMITS)
        # A trailing window would also count block-a-tail, reading 50.00.
        self.assertAlmostEqual(state["window_5h"]["usd"], 25.0)

    def test_the_block_ends_five_hours_after_the_message_that_opened_it(self):
        # Asserted against current_block rather than through state.json: the
        # reset time on the row now comes from the account figures, and this is
        # the local guess that stands in when there are none.
        timestamps = [self.now - 6 * 3600, self.now - 30 * 60]
        _, end = current_block(timestamps, self.now)
        self.assertAlmostEqual(end, self.now - 30 * 60 + 5 * 3600, places=3)

    def test_five_hour_window_is_zero_once_the_block_has_expired(self):
        """No block is open, so nothing is counted against the limit yet.

        Reporting the expired block's spend would keep the row red long after
        the limit had actually reset, which is the opposite of the mistake the
        trailing window made.
        """
        events = [event(self.now - 6 * 3600, "a")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_LIMITS)
        self.assertAlmostEqual(state["window_5h"]["usd"], 0.0)
        self.assertIsNone(current_block([self.now - 6 * 3600], self.now))

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
        state = build_state(events, PRICING, "s1", set(), self.now, NO_LIMITS)
        self.assertAlmostEqual(state["window_5h"]["usd"], 50.0)
        _, end = current_block([ts for ts, *_ in events], self.now)
        self.assertAlmostEqual(end, self.now - 3 * 3600 + 5 * 3600, places=3)

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
        state = build_state(events, PRICING, "s1", set(), self.now, NO_LIMITS)
        self.assertAlmostEqual(state["window_5h"]["usd"], 25.0)

    def test_seven_day_window_excludes_older_events(self):
        events = [event(self.now - 8 * 86400, "old"), event(self.now - 60, "new")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_LIMITS)
        self.assertAlmostEqual(state["window_7d"]["usd"], 25.0)

    def test_unknown_model_is_reported_and_not_priced_as_zero(self):
        events = [event(self.now, "a", model="claude-nonexistent-9")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_LIMITS)
        self.assertEqual(state["unknown_models"], ["claude-nonexistent-9"])
        self.assertAlmostEqual(state["session"]["usd"], 0.0)

    def test_today_spans_sessions_but_not_yesterday(self):
        events = [
            event(self.now - 30 * 3600, "yesterday"),
            event(self.now - 60, "a"),
            event(self.now - 60, "b", session="s2"),
        ]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_LIMITS)
        self.assertAlmostEqual(state["today_usd"], 50.0)

    def test_cache_fields_are_priced_at_their_own_multipliers(self):
        # Distinct counts per bucket so a swapped multiplier cannot cancel out.
        events = [[self.now, "a", "s1", "claude-opus-5", 0, 0, 1_000_000, 2_000_000, 4_000_000]]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_LIMITS)
        expected = 5.0 * 1.25 + 2 * 5.0 * 2.0 + 4 * 5.0 * 0.1
        self.assertAlmostEqual(state["session"]["usd"], expected)

    def test_empty_event_list_produces_zeroed_state(self):
        state = build_state([], PRICING, "s1", set(), self.now, NO_LIMITS)
        self.assertEqual(state["session"]["usd"], 0.0)
        self.assertEqual(state["today_usd"], 0.0)
        self.assertEqual(state["unknown_models"], [])


class LimitsTest(unittest.TestCase):
    """The account figures pass through, and they bound the local 5h window."""

    def setUp(self):
        self.now = datetime.now().replace(hour=12, minute=0, second=0,
                                          microsecond=0).timestamp()

    def test_the_account_figures_reach_state_untouched(self):
        given = limits()
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, given)
        self.assertEqual(state["limits"], given)

    def test_no_account_figures_is_recorded_as_such(self):
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, NO_LIMITS)
        self.assertIsNone(state["limits"])

    def test_the_window_rows_no_longer_carry_a_percentage_or_a_reset(self):
        # Both moved into `limits`, which has one owner. Two sources for the same
        # fact is what the ceilings did.
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, limits())
        self.assertEqual(set(state["window_5h"]), {"usd"})
        self.assertEqual(set(state["window_7d"]), {"usd"})

    # Three events chosen so the anchor and the local guess disagree. The server
    # says the open block ends 10 minutes from now, so it began 4h50m ago. The
    # local guess instead opens a block on the oldest event, 5h33m ago, which
    # expires before the newest event and so re-anchors on that one alone.
    SPREAD = (-20_000.0, -16_000.0, -60.0)

    def spread(self):
        return [event(self.now + offset, f"e{index}")
                for index, offset in enumerate(self.SPREAD)]

    def test_the_servers_reset_time_bounds_the_local_dollar_window(self):
        # A block opened on another machine began before anything in these
        # events, so the local guess puts its start in the wrong place. The reset
        # time the server reports is what the block really is: two of the three
        # events fall inside it.
        reset = datetime.fromtimestamp(self.now + 600.0, timezone.utc).isoformat()
        state = build_state(self.spread(), PRICING, "s1", set(), self.now,
                            limits(resets_at=reset))
        self.assertAlmostEqual(state["window_5h"]["usd"], 50.0)

    def test_without_an_anchor_the_local_guess_counts_something_else(self):
        # The same events, no account figure: the chain re-anchors on the newest
        # event and the row reports one event instead of two. This is the figure
        # the anchor exists to correct, and it is still what a machine with no
        # cached account figures sees.
        state = build_state(self.spread(), PRICING, "s1", set(), self.now,
                            NO_LIMITS)
        self.assertAlmostEqual(state["window_5h"]["usd"], 25.0)

    def test_a_reset_time_already_past_falls_back_to_the_local_guess(self):
        past = datetime.fromtimestamp(self.now - 60.0, timezone.utc).isoformat()
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, limits(resets_at=past))
        self.assertAlmostEqual(state["window_5h"]["usd"], 25.0)


if __name__ == "__main__":
    unittest.main()
