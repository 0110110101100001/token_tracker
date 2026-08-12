# tests/test_roll.py
"""The tween maths behind the rolling figures.

This module exists so the animation can be tested without a display and without
a main loop. The widget owns the timer and the labels; everything that decides
*what to draw at moment p* lives here, as plain arithmetic over floats.

The properties asserted below are the ones the effect actually depends on: a
roll that does not land exactly on its target leaves a wrong number on screen,
and a duration that does not scale with the distance runs an expensive turn and
a trivial one at wildly different speeds to cover their ground in one fixed
window.
"""

import unittest

from cost_meter import roll


class EaseInOutTest(unittest.TestCase):
    def test_the_endpoints_are_exact(self):
        # Not "close to": the last frame writes this value to the label, so any
        # drift here shows up as a total that is a fraction of a cent wrong.
        self.assertEqual(roll.ease_in_out(0.0), 0.0)
        self.assertEqual(roll.ease_in_out(1.0), 1.0)

    def test_the_halfway_point_is_halfway(self):
        self.assertAlmostEqual(roll.ease_in_out(0.5), 0.5)

    def test_it_never_goes_backwards(self):
        previous = -1.0
        for step in range(101):
            value = roll.ease_in_out(step / 100)
            self.assertGreaterEqual(value, previous)
            previous = value

    def test_it_starts_slower_than_it_finishes_the_first_half(self):
        """The accelerate half: equal slices of time cover growing distance."""
        first = roll.ease_in_out(0.1) - roll.ease_in_out(0.0)
        second = roll.ease_in_out(0.2) - roll.ease_in_out(0.1)
        self.assertGreater(second, first)

    def test_progress_outside_the_curve_is_clamped(self):
        # A frame can land past the end when the timer runs long; the value it
        # produces must still be the target rather than an overshoot.
        self.assertEqual(roll.ease_in_out(1.4), 1.0)
        self.assertEqual(roll.ease_in_out(-0.2), 0.0)


class ValueAtTest(unittest.TestCase):
    def test_it_starts_at_the_start_and_ends_at_the_end(self):
        self.assertEqual(roll.value_at(10.0, 48.0, 0.0), 10.0)
        self.assertEqual(roll.value_at(10.0, 48.0, 1.0), 48.0)

    def test_it_runs_downwards_too(self):
        # A 5-hour block resetting drops the row from its total to zero, so the
        # tween is not allowed to assume the value only ever grows.
        self.assertEqual(roll.value_at(48.0, 0.0, 0.0), 48.0)
        self.assertEqual(roll.value_at(48.0, 0.0, 1.0), 0.0)
        self.assertLess(roll.value_at(48.0, 0.0, 0.5), 48.0)

    def test_the_midpoint_is_between_the_two(self):
        self.assertAlmostEqual(roll.value_at(10.0, 48.0, 0.5), 29.0)


class DurationTest(unittest.TestCase):
    """A second, plus a quarter of a second for every ten dollars moved."""

    def test_a_move_of_nothing_still_takes_the_base_second(self):
        self.assertEqual(roll.duration_ms(0.0), 1000.0)

    def test_forty_dollars_takes_two_seconds(self):
        # The figure the pace was tuned against; the constant exists to hit it.
        self.assertEqual(roll.duration_ms(40.0), 2000.0)

    def test_a_falling_row_takes_as_long_as_a_rising_one(self):
        # A 5-hour block resetting is a fall, and `Roll.distance` hands over an
        # absolute figure -- but a negative one must not shorten the roll, still
        # less produce a duration of zero and a division by it.
        self.assertEqual(roll.duration_ms(-40.0), roll.duration_ms(40.0))

    def test_it_grows_with_the_distance(self):
        lengths = [roll.duration_ms(d) for d in (0.5, 4.0, 40.0, 400.0)]
        self.assertEqual(lengths, sorted(lengths))
        self.assertNotEqual(len(set(lengths)), 1)


class RollTest(unittest.TestCase):
    """Which rows animate, and what each shows on a given frame.

    This is the half of the animation that decides rather than draws, and it is
    where the effect goes wrong if it goes wrong at all: `refresh()` runs from
    the file monitor, from a 60-second poll, from the right-click menu and from
    `__init__`, so a roll driven by "the text changed" alone would re-animate the
    same figure every minute and would roll up from zero on startup.
    """

    def setUp(self):
        self.roll = roll.Roll(min_delta=0.01)

    def test_the_first_sighting_of_a_row_is_not_animated(self):
        """Startup, not a change.

        __init__ calls refresh() before place(), so rolling here would both
        assert a change that never happened and delay the first anchor.
        """
        self.assertEqual(self.roll.retarget({"today": 12.0}), False)
        self.assertEqual(self.roll.shown("today"), 12.0)

    def test_an_unchanged_value_does_not_animate(self):
        # The 60-second staleness poll re-reads the same state.json, and the
        # file monitor emits more than one event per write.
        self.roll.retarget({"today": 12.0})
        self.assertEqual(self.roll.retarget({"today": 12.0}), False)

    def test_a_real_change_animates(self):
        self.roll.retarget({"today": 12.0})
        self.assertEqual(self.roll.retarget({"today": 48.0}), True)

    def test_a_change_of_less_than_the_minimum_is_set_outright(self):
        # Forty-odd frames for a fraction of a cent is noise, not information.
        self.roll.retarget({"today": 12.0})
        self.assertEqual(self.roll.retarget({"today": 12.004}), False)
        self.assertEqual(self.roll.shown("today"), 12.004)

    def test_a_fall_animates_as_readily_as_a_rise(self):
        # The 5-hour row drops to zero when its block resets.
        self.roll.retarget({"window_5h": 48.0})
        self.assertEqual(self.roll.retarget({"window_5h": 0.0}), True)

    def test_a_frame_runs_from_the_old_value_to_the_new_one(self):
        self.roll.retarget({"today": 10.0})
        self.roll.retarget({"today": 48.0})
        self.assertEqual(self.roll.frame(0.0)["today"], 10.0)
        self.assertEqual(self.roll.frame(1.0)["today"], 48.0)

    def test_the_distance_is_the_longest_leg_in_flight(self):
        """What the duration is derived from.

        The rows share one clock, so they need one distance, and the row with
        the furthest to go is the one that has to fit inside it.
        """
        self.roll.retarget({"today": 10.0, "window_5h": 48.0})
        self.roll.retarget({"today": 14.0, "window_5h": 0.0})
        self.assertEqual(self.roll.distance(), 48.0)

    def test_a_settled_panel_has_no_distance(self):
        # Nothing in flight means nothing to time, and the caller divides the
        # elapsed milliseconds by whatever this produces a duration from.
        self.assertEqual(self.roll.distance(), 0.0)
        self.roll.retarget({"today": 10.0})
        self.assertEqual(self.roll.distance(), 0.0)

    def test_the_distance_left_mid_roll_is_measured_from_where_it_is(self):
        # A retarget re-bases the leg on what is on screen, so the second roll
        # is timed for the ground it actually has to cover, not for the whole
        # original jump.
        self.roll.retarget({"today": 10.0})
        self.roll.retarget({"today": 48.0})
        mid = self.roll.frame(0.5)["today"]
        self.roll.retarget({"today": 60.0})
        self.assertAlmostEqual(self.roll.distance(), 60.0 - mid)

    def test_only_the_rows_that_changed_are_in_a_frame(self):
        self.roll.retarget({"today": 10.0, "session": 3.0})
        self.roll.retarget({"today": 48.0, "session": 3.0})
        self.assertEqual(list(self.roll.frame(0.5)), ["today"])

    def test_the_final_frame_leaves_nothing_in_flight(self):
        self.roll.retarget({"today": 10.0})
        self.roll.retarget({"today": 48.0})
        self.roll.frame(1.0)
        self.assertFalse(self.roll.running())
        self.assertEqual(self.roll.shown("today"), 48.0)

    def test_a_retarget_mid_roll_carries_on_from_what_is_on_screen(self):
        """The one that produces a visible glitch when it is wrong.

        A second turn landing mid-roll must pick the row up where the eye last
        saw it. Restarting from the previous leg's start would snap the number
        backwards before running forwards again.
        """
        self.roll.retarget({"today": 10.0})
        self.roll.retarget({"today": 48.0})
        mid = self.roll.frame(0.5)["today"]

        self.assertEqual(self.roll.retarget({"today": 60.0}), True)
        self.assertEqual(self.roll.frame(0.0)["today"], mid)
        self.assertEqual(self.roll.frame(1.0)["today"], 60.0)

    def test_a_retarget_back_to_the_displayed_value_still_settles(self):
        # Mid-roll the shown value is neither end, so a target equal to it is a
        # real change against the remembered target and must not be left hanging
        # half way.
        self.roll.retarget({"today": 10.0})
        self.roll.retarget({"today": 48.0})
        mid = self.roll.frame(0.5)["today"]
        self.roll.retarget({"today": mid})
        self.assertEqual(self.roll.shown("today"), mid)
        self.assertFalse(self.roll.running())

    def test_cancelling_lands_on_the_target_rather_than_where_it_stopped(self):
        """Staleness cancels the roll.

        Stale means these figures are not current, and animating them would
        present them as fresh -- but the number itself is still the last one
        recorded, so the row lands on it instead of freezing mid-tween.
        """
        self.roll.retarget({"today": 10.0})
        self.roll.retarget({"today": 48.0})
        self.roll.frame(0.3)
        self.roll.cancel()
        self.assertFalse(self.roll.running())
        self.assertEqual(self.roll.shown("today"), 48.0)

    def test_a_row_with_nothing_recorded_yet_is_not_animated(self):
        # state.json can carry a null where a figure has never been written.
        self.assertEqual(self.roll.retarget({"today": None}), False)
        self.assertIsNone(self.roll.shown("today"))

    def test_a_row_that_goes_from_nothing_to_a_figure_is_set_outright(self):
        # There is no value to roll up *from*, so a tween would have to invent
        # a starting point.
        self.roll.retarget({"today": None})
        self.assertEqual(self.roll.retarget({"today": 4.0}), False)
        self.assertEqual(self.roll.shown("today"), 4.0)

    def test_an_unseen_row_has_nothing_shown(self):
        self.assertIsNone(self.roll.shown("today"))


class ReplayTest(unittest.TestCase):
    """The per-turn delta, which counts up from zero every time.

    `last turn` is not a running total: the distance between what one turn cost
    and what the next one cost is not a quantity anybody is watching, and a
    cheap turn after an expensive one would run the row *downwards* to announce
    a new charge. Every turn therefore starts at zero and climbs to its own
    figure, which is also why this cannot go through `retarget` -- two turns
    that happen to cost the same are still two turns, and `retarget` is built to
    say nothing when the figure it is handed has not changed.
    """

    def setUp(self):
        self.roll = roll.Roll(min_delta=0.01)

    def test_the_first_sighting_is_not_animated(self):
        # Startup, as in `retarget`: the panel opens showing whatever the last
        # turn cost, and counting that up would assert a turn that just landed.
        self.assertEqual(self.roll.replay("last_turn", 4.2), False)
        self.assertEqual(self.roll.shown("last_turn"), 4.2)

    def test_a_turn_runs_up_from_zero(self):
        self.roll.replay("last_turn", 0.03)
        self.assertEqual(self.roll.replay("last_turn", 4.2), True)
        self.assertEqual(self.roll.frame(0.0)["last_turn"], 0.0)
        self.assertEqual(self.roll.frame(1.0)["last_turn"], 4.2)

    def test_a_cheaper_turn_still_runs_upwards(self):
        # The case that rules out treating this row as cumulative.
        self.roll.replay("last_turn", 4.2)
        self.assertEqual(self.roll.replay("last_turn", 0.5), True)
        self.assertEqual(self.roll.frame(0.0)["last_turn"], 0.0)
        self.assertEqual(self.roll.frame(1.0)["last_turn"], 0.5)

    def test_the_same_figure_twice_is_two_turns(self):
        # Where this parts company with retarget: identical consecutive costs
        # are ordinary, and the row holding still would hide the second turn.
        self.roll.replay("last_turn", 0.03)
        self.assertEqual(self.roll.replay("last_turn", 0.03), True)

    def test_a_turn_too_small_to_watch_is_set_outright(self):
        self.roll.replay("last_turn", 4.2)
        self.assertEqual(self.roll.replay("last_turn", 0.004), False)
        self.assertEqual(self.roll.shown("last_turn"), 0.004)

    def test_a_turn_of_nothing_is_set_outright(self):
        # A tally run that found no new messages reports a zero delta; there is
        # no distance to cover and the row simply goes back to its dash.
        self.roll.replay("last_turn", 4.2)
        self.assertEqual(self.roll.replay("last_turn", 0.0), False)
        self.assertEqual(self.roll.shown("last_turn"), 0.0)

    def test_a_replayed_row_shares_the_panel_distance(self):
        # One clock for the whole panel: the turn row must be in the figure the
        # duration is taken from, or it would be given the other rows' pace.
        self.roll.retarget({"today": 10.0})
        self.roll.replay("last_turn", 0.01)
        self.roll.replay("last_turn", 4.0)
        self.roll.retarget({"today": 12.0})
        self.assertEqual(self.roll.distance(), 4.0)

    def test_a_replay_survives_a_retarget_rebasing_the_legs(self):
        """The ordering trap: `retarget` re-bases every leg in flight.

        The widget replays the turn row and retargets the cumulative rows in the
        same refresh. Re-basing takes each leg's start from what the row is
        showing, so the replay has to have put zero there already -- otherwise
        the row would be re-based onto the previous turn's figure and the count
        would start from the wrong end.
        """
        self.roll.replay("last_turn", 0.03)
        self.roll.retarget({"today": 10.0})
        self.roll.replay("last_turn", 4.2)
        self.roll.retarget({"today": 14.2})
        self.assertEqual(self.roll.frame(0.0)["last_turn"], 0.0)

    def test_cancelling_lands_on_the_turn_figure_not_on_zero(self):
        # Staleness stops the count; the figure it was counting to is still the
        # last thing recorded, so the row shows that rather than $0.00.
        self.roll.replay("last_turn", 0.03)
        self.roll.replay("last_turn", 4.2)
        self.roll.frame(0.3)
        self.roll.cancel()
        self.assertEqual(self.roll.shown("last_turn"), 4.2)

    def test_a_row_counting_up_is_moving(self):
        # What tells `$0.00 on the way up` apart from `nothing recorded`.
        self.roll.replay("last_turn", 0.03)
        self.roll.replay("last_turn", 4.2)
        self.assertTrue(self.roll.moving("last_turn"))

    def test_a_settled_row_is_not_moving(self):
        self.roll.replay("last_turn", 0.03)
        self.roll.replay("last_turn", 4.2)
        self.roll.frame(1.0)
        self.assertFalse(self.roll.moving("last_turn"))

    def test_a_row_that_never_moved_is_not_moving(self):
        self.assertFalse(self.roll.moving("last_turn"))


if __name__ == "__main__":
    unittest.main()
