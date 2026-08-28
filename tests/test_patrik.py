# tests/test_patrik.py
"""The particle and shake maths behind Patrik mode.

Same split as tests/test_roll.py, and for the same reason: the widget owns the
overlay window, the cairo context and the frame timer, and everything that
decides *where a glyph is and how solid it looks* is arithmetic that needs
neither a display nor a main loop.

Two of the properties asserted below are not cosmetic, and the effect is wrong
without them. Glyphs have to leave the panel's rectangle -- fading them out
inside it was the alternative, and on screen that reads as a panel with dirt on
it rather than as money flying off. And `Shake.offset` has to return exactly
(0, 0) when it is done: the widget shakes by moving the window, and the panel's
position bookkeeping treats a window that is not at its anchor as a position the
user chose. A shake that ended a pixel out would be written to config.json as a
deliberate move, and the panel would walk across the screen a pixel per turn.
"""

import ctypes
import os
import random
import time
import unittest
import unittest.mock
from datetime import datetime

import widget
from cost_meter import launch, patrik, paths, store
from tests.support import TempHome
from tests.test_widget import own_hwnds_titled

HAS_DISPLAY = launch.has_display()

if HAS_DISPLAY:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020


# The panel's rectangle inside the overlay window. The overlay is the panel plus
# a margin all round, so the panel does not begin at the origin.
RECT = (40, 40, 200, 160)


def swarm(seed=1, **kwargs):
    """A swarm with a seeded generator, so a failure here is reproducible."""
    return patrik.Swarm(rng=random.Random(seed), **kwargs)


def run(sworm, seconds, dt=0.016):
    """Advance `seconds` worth of frames at `dt` each."""
    for _ in range(int(seconds / dt)):
        sworm.frame(dt)


class BurstTest(unittest.TestCase):
    def test_a_burst_spawns_the_number_asked_for(self):
        sworm = swarm()
        sworm.burst(12, RECT)
        self.assertEqual(len(sworm.particles()), 12)

    def test_a_burst_of_nothing_leaves_the_swarm_idle(self):
        # The widget stops its frame timer on `running()`, so a zero burst that
        # reported itself as running would spin a 16 ms timer over an empty list
        # for as long as the panel lived.
        sworm = swarm()
        sworm.burst(0, RECT)
        self.assertFalse(sworm.running())

    def test_particles_start_inside_the_panel(self):
        """They come *from* the table, so that is where they are first drawn."""
        sworm = swarm()
        sworm.burst(30, RECT)
        x, y, width, height = RECT
        for particle in sworm.particles():
            self.assertGreaterEqual(particle.x, x)
            self.assertLessEqual(particle.x, x + width)
            self.assertGreaterEqual(particle.y, y)
            self.assertLessEqual(particle.y, y + height)

    def test_it_only_uses_the_glyphs_it_was_given(self):
        # U+1FA99 COIN is why this is asserted rather than assumed: it is the
        # obvious glyph for the effect, and Segoe UI Emoji on Windows 10 has no
        # glyph for it, so it draws as an empty box. The set is the caller's
        # choice and nothing in here may invent one.
        sworm = swarm(glyphs=("\U0001F911", "\U0001F4B2"))
        sworm.burst(40, RECT)
        self.assertEqual({p.glyph for p in sworm.particles()},
                         {"\U0001F911", "\U0001F4B2"})

    def test_the_default_glyphs_are_the_ones_that_render(self):
        # Measured with Pango against Segoe UI Emoji: these four have real
        # glyphs, U+1FA99 does not. See the note above.
        self.assertEqual(patrik.GLYPHS,
                         ("\U0001F911", "\U0001F4B2",
                          "\U0001F4B0", "\U0001F4B5"))

    def test_a_second_burst_joins_the_first_instead_of_replacing_it(self):
        # Two turns landing close together is ordinary, and the second must not
        # wipe the glyphs the first one still has in the air.
        sworm = swarm()
        sworm.burst(6, RECT)
        sworm.frame(0.1)
        sworm.burst(6, RECT)
        self.assertEqual(len(sworm.particles()), 12)

    def test_a_seed_makes_a_burst_reproducible(self):
        first, second = swarm(seed=7), swarm(seed=7)
        first.burst(10, RECT)
        second.burst(10, RECT)
        self.assertEqual([(p.glyph, p.x, p.y) for p in first.particles()],
                         [(p.glyph, p.x, p.y) for p in second.particles()])


class GlyphScaleTest(unittest.TestCase):
    """Glyph size follows the panel's scale.

    The panel is drag-resizable, and a spray that stayed a fixed speck on a panel
    dragged twice as large reads as somebody else's animation playing on top of
    it rather than as this one coming out of the table. Applied here rather than
    in the draw handler for the reason the rest of this module exists: a size is
    arithmetic, and arithmetic can be tested without a display.
    """

    def test_the_default_is_the_size_it_always_was(self):
        """An unscaled call is the panel at 1.0, so nothing may move under it."""
        plain, scaled = swarm(seed=3), swarm(seed=3)
        plain.burst(20, RECT)
        scaled.burst(20, RECT, scale=1.0)
        self.assertEqual([p.size for p in plain.particles()],
                         [p.size for p in scaled.particles()])

    def test_a_bigger_panel_throws_bigger_glyphs(self):
        plain, scaled = swarm(seed=3), swarm(seed=3)
        plain.burst(20, RECT)
        scaled.burst(20, RECT, scale=2.0)
        self.assertEqual([p.size * 2.0 for p in plain.particles()],
                         [p.size for p in scaled.particles()])

    def test_the_scale_reaches_the_emitted_glyphs_too(self):
        # Most of a celebration arrives through `emit` rather than the opening
        # burst, so a burst that grew while the spray behind it did not would be
        # worse than neither growing.
        plain, scaled = swarm(seed=5), swarm(seed=5)
        plain.emit(1.0, RECT, rate=10.0)
        scaled.emit(1.0, RECT, rate=10.0, scale=2.0)
        self.assertEqual([p.size * 2.0 for p in plain.particles()],
                         [p.size for p in scaled.particles()])

    def test_the_scale_does_not_move_where_they_start(self):
        """`rect` is already in overlay pixels; scaling it twice would throw the
        glyphs out of the panel they are supposed to come from."""
        sworm = swarm()
        sworm.burst(30, RECT, scale=2.0)
        x, y, width, height = RECT
        for particle in sworm.particles():
            self.assertGreaterEqual(particle.x, x)
            self.assertLessEqual(particle.x, x + width)
            self.assertGreaterEqual(particle.y, y)
            self.assertLessEqual(particle.y, y + height)


class FlightTest(unittest.TestCase):
    def test_a_frame_moves_them(self):
        sworm = swarm()
        sworm.burst(10, RECT)
        before = [(p.x, p.y) for p in sworm.particles()]
        sworm.frame(0.05)
        self.assertNotEqual(before, [(p.x, p.y) for p in sworm.particles()])

    def test_a_still_frame_moves_nothing(self):
        """A dt of zero is what two frames inside one millisecond look like."""
        sworm = swarm()
        sworm.burst(10, RECT)
        before = [(p.x, p.y) for p in sworm.particles()]
        sworm.frame(0.0)
        self.assertEqual(before, [(p.x, p.y) for p in sworm.particles()])

    def test_they_rise_before_they_fall(self):
        # Thrown up and pulled down, rather than drifting: a glyph that only
        # ever sank looked like the panel leaking, and one that only rose left
        # the screen too fast to read.
        sworm = swarm()
        sworm.burst(1, RECT)
        particle = sworm.particles()[0]
        start_y = particle.y
        run(sworm, 0.2)
        self.assertLess(particle.y, start_y)  # y grows downward
        run(sworm, 4.0)
        self.assertGreater(particle.y, start_y)

    def test_gravity_only_ever_pulls_downward(self):
        sworm = swarm()
        sworm.burst(1, RECT)
        particle = sworm.particles()[0]
        previous = particle.vy
        for _ in range(30):
            sworm.frame(0.016)
            self.assertGreater(particle.vy, previous)
            previous = particle.vy

    def test_every_glyph_leaves_the_panel(self):
        """The whole point: they cross the edge rather than fading inside it.

        Asserted for every particle rather than for one of them -- a spread that
        let part of the burst die inside the rectangle would look like half the
        money never made it out.
        """
        sworm = swarm()
        sworm.burst(20, RECT)
        x, y, width, height = RECT
        watched = list(sworm.particles())
        escaped = set()
        for _ in range(600):
            sworm.frame(0.016)
            for index, particle in enumerate(watched):
                if not (x <= particle.x <= x + width
                        and y <= particle.y <= y + height):
                    escaped.add(index)
        self.assertEqual(len(escaped), len(watched),
                         "every glyph has to cross the panel's edge")


class FadeTest(unittest.TestCase):
    def test_they_start_solid(self):
        sworm = swarm()
        sworm.burst(10, RECT)
        for particle in sworm.particles():
            self.assertEqual(particle.alpha, 1.0)

    def test_the_fade_never_goes_back_up(self):
        sworm = swarm()
        sworm.burst(1, RECT)
        particle = sworm.particles()[0]
        previous = 1.0
        while sworm.running():
            sworm.frame(0.016)
            self.assertLessEqual(particle.alpha, previous)
            previous = particle.alpha

    def test_alpha_stays_within_range(self):
        sworm = swarm()
        sworm.burst(20, RECT)
        while sworm.running():
            for particle in sworm.frame(0.016):
                self.assertGreaterEqual(particle.alpha, 0.0)
                self.assertLessEqual(particle.alpha, 1.0)

    def test_a_faded_particle_is_dropped(self):
        # Not merely left invisible: an alpha-zero glyph still costs a cairo
        # show_text on every frame, and the timer would never stop.
        sworm = swarm()
        sworm.burst(8, RECT)
        run(sworm, 30.0)
        self.assertEqual(sworm.particles(), [])

    def test_the_swarm_stops_running_once_it_is_empty(self):
        sworm = swarm()
        sworm.burst(8, RECT)
        self.assertTrue(sworm.running())
        run(sworm, 30.0)
        self.assertFalse(sworm.running())

    def test_frame_returns_the_particles_still_alive(self):
        """What the draw handler iterates, so it never sees a dead glyph."""
        sworm = swarm()
        sworm.burst(8, RECT)
        self.assertEqual(sworm.frame(0.016), sworm.particles())



class DurationTest(unittest.TestCase):
    """How long a celebration lasts, and why it depends on the turn.

    The same shape as `roll.duration_ms` and for the same reason: a fixed length
    runs an expensive turn and a trivial one at wildly different speeds to cover
    their ground in one window, and it is the expensive one that most deserves to
    be watched. The numbers here are exactly double the roll's, so the figures
    finish counting up around halfway through the glyphs.
    """

    def test_a_turn_that_cost_nothing_still_gets_the_base_length(self):
        self.assertEqual(patrik.duration_ms(0.0), 2000.0)

    def test_forty_dollars_takes_four_seconds(self):
        self.assertEqual(patrik.duration_ms(40.0), 4000.0)

    def test_it_grows_with_the_spend(self):
        previous = 0.0
        for usd in (0.0, 0.01, 1.0, 5.0, 40.0, 200.0):
            length = patrik.duration_ms(usd)
            self.assertGreater(length, previous)
            previous = length

    def test_a_missing_figure_is_the_base_length_rather_than_a_crash(self):
        # state.json can carry a null here, and the panel must not die of it.
        self.assertEqual(patrik.duration_ms(None), 2000.0)

    def test_a_negative_figure_does_not_run_backwards(self):
        """A correction can push a turn's cost below zero; length is a length."""
        self.assertEqual(patrik.duration_ms(-40.0), patrik.duration_ms(40.0))


class RateTest(unittest.TestCase):
    """How fast glyphs arrive, and why the session's total is what decides it.

    The session rather than the turn, because the turn already has its say: it
    sets the length. Spend is what the panel is for, and a session deep into real
    money should look different from one that has barely started, not merely run
    for longer.

    Steps rather than a slope. A rate that crept up with every cent would be a
    change nobody could see between one turn and the next; a step is something
    the panel does at a figure the user can name.
    """

    def test_a_quiet_session_gets_the_base_rate(self):
        self.assertEqual(patrik.rate(4.99), patrik.RATE)

    def test_each_threshold_lifts_it(self):
        self.assertEqual(patrik.rate(10.0), 11.0)
        self.assertEqual(patrik.rate(20.0), 13.0)
        self.assertEqual(patrik.rate(30.0), 15.0)

    def test_a_threshold_lands_on_the_figure_itself(self):
        # The row shows two decimals, so the step has to happen where the panel
        # says it does: $9.99 is still a quiet session and $10.00 is not.
        self.assertEqual(patrik.rate(9.99), patrik.RATE)
        self.assertLess(patrik.rate(9.99), patrik.rate(10.0))

    def test_it_never_falls_as_the_session_grows(self):
        previous = 0.0
        for usd in (0.0, 9.99, 10.0, 19.99, 20.0, 29.99, 30.0, 500.0):
            current = patrik.rate(usd)
            self.assertGreaterEqual(current, previous, usd)
            previous = current

    def test_the_top_step_is_the_ceiling(self):
        # Unbounded was the alternative, and a session in the hundreds would
        # have filled the overlay faster than the glyphs could leave it.
        self.assertEqual(patrik.rate(5000.0), patrik.rate(30.0))

    def test_a_missing_figure_is_the_base_rate_rather_than_a_crash(self):
        """state.json can carry a null there, exactly as `duration_ms` handles."""
        self.assertEqual(patrik.rate(None), patrik.RATE)

    def test_a_negative_total_is_a_quiet_session_not_a_loud_one(self):
        # A correction can push a total below zero. Unlike `duration_ms`, this
        # one must not take the absolute value: -$40 is not a $40 session.
        self.assertEqual(patrik.rate(-40.0), patrik.RATE)

    def test_the_steps_are_the_ones_the_panel_ships_with(self):
        self.assertEqual(patrik.RATE_TIERS,
                         ((30.0, 15.0), (20.0, 13.0), (10.0, 11.0)))


class EmitTest(unittest.TestCase):
    """Glyphs keep arriving for as long as the celebration runs.

    One opening burst was the first version, and on a four-second animation it
    left three and a half seconds of glyphs merely falling -- the celebration
    visibly ran out before it ended.
    """

    def test_it_spawns_at_the_rate_it_was_given(self):
        sworm = swarm()
        sworm.emit(1.0, RECT, rate=10.0)
        self.assertEqual(len(sworm.particles()), 10)

    def test_a_fraction_of_a_glyph_is_carried_across_frames(self):
        """At 16 ms a frame, ten a second is 0.16 of a glyph per frame.

        Truncated per frame that is zero every time, and no glyph would ever
        arrive after the opening burst.
        """
        sworm = swarm()
        for _ in range(63):                      # a second of 16 ms frames
            sworm.emit(0.016, RECT, rate=10.0)
        self.assertGreaterEqual(len(sworm.particles()), 9)

    def test_no_time_passing_spawns_nothing(self):
        sworm = swarm()
        sworm.emit(0.0, RECT, rate=10.0)
        self.assertEqual(sworm.particles(), [])

    def test_emitted_glyphs_start_inside_the_panel_too(self):
        sworm = swarm()
        sworm.emit(1.0, RECT, rate=10.0)
        x, y, width, height = RECT
        for particle in sworm.particles():
            self.assertGreaterEqual(particle.x, x)
            self.assertLessEqual(particle.x, x + width)


class LifeBudgetTest(unittest.TestCase):
    """No glyph may outlive the animation it belongs to.

    The animation's length is the promise -- two seconds, four on an expensive
    turn -- and a glyph thrown near the end with a full lifetime would still be
    falling a second and a half after the celebration was supposed to be over.
    """

    def test_a_glyph_cannot_outlive_the_budget_it_was_given(self):
        sworm = swarm()
        sworm.burst(20, RECT, max_life=0.3)
        for particle in sworm.particles():
            self.assertLessEqual(particle.life, 0.3)

    def test_a_budget_longer_than_a_natural_life_changes_nothing(self):
        sworm = swarm()
        sworm.burst(20, RECT, max_life=99.0)
        for particle in sworm.particles():
            self.assertLessEqual(particle.life, patrik.LIFE_MAX)

    def test_a_clamped_glyph_still_fades_over_its_whole_life(self):
        """Which is what stops a late glyph blinking out instead of fading.

        The fade is measured against the glyph's own life, not against the
        animation, so a glyph given a third of a second fades smoothly across
        that third of a second.
        """
        sworm = swarm()
        sworm.burst(1, RECT, max_life=0.3)
        particle = sworm.particles()[0]
        run(sworm, 0.15, dt=0.015)
        self.assertAlmostEqual(particle.alpha, 0.5, places=1)

    def test_a_budget_of_nothing_spawns_nothing(self):
        # The last frame of the animation asks for a life of zero, and a glyph
        # that is born dead would be painted once at alpha zero for no reason.
        sworm = swarm()
        sworm.burst(10, RECT, max_life=0.0)
        self.assertEqual(sworm.particles(), [])

class ShakeTest(unittest.TestCase):
    def test_it_ends_exactly_at_the_origin(self):
        """The property the panel's saved position depends on.

        `_persist_position` declines to record a position while the window is at
        its anchor, so the shake has to hand the window back to the exact pixel
        it was lifted from. Near enough is a panel that walks.
        """
        self.assertEqual(patrik.Shake().offset(1.0), (0, 0))

    def test_it_is_still_at_the_origin_past_the_end(self):
        # The widget derives progress from wall-clock time, so a frame landing
        # after the duration is ordinary and must not wrap around into a move.
        shake = patrik.Shake()
        self.assertEqual(shake.offset(1.4), (0, 0))
        self.assertEqual(shake.offset(9.0), (0, 0))

    def test_it_starts_at_the_origin(self):
        self.assertEqual(patrik.Shake().offset(0.0), (0, 0))

    def test_it_actually_moves_in_between(self):
        shake = patrik.Shake()
        offsets = {shake.offset(step / 40) for step in range(1, 40)}
        self.assertGreater(len(offsets), 3, "a shake that never moves is none")

    def test_the_offsets_are_whole_pixels(self):
        """Gtk.Window.move takes ints; a float is silently truncated."""
        shake = patrik.Shake()
        for step in range(41):
            dx, dy = shake.offset(step / 40)
            self.assertIsInstance(dx, int)
            self.assertIsInstance(dy, int)

    def test_it_never_exceeds_its_amplitude(self):
        shake = patrik.Shake(amplitude=5)
        for step in range(101):
            dx, dy = shake.offset(step / 100)
            self.assertLessEqual(abs(dx), 5)
            self.assertLessEqual(abs(dy), 5)

    def test_it_decays(self):
        """Late in the shake it moves less than early, or it is a vibration."""
        shake = patrik.Shake(amplitude=12)
        early = max(abs(shake.offset(step / 100)[0]) for step in range(0, 30))
        late = max(abs(shake.offset(step / 100)[0]) for step in range(70, 100))
        self.assertLess(late, early)

    def test_a_long_shake_wobbles_as_fast_as_a_short_one(self):
        """Cycles come from the duration, so a flinch stays a flinch.

        With a fixed cycle count a shake stretched from 420 ms to 1.6 s becomes a
        slow sway -- the panel leaning about rather than reacting. Holding the
        time per wobble constant is what keeps the character when the length
        changes with the turn.
        """
        def wobbles_per_second(duration_ms):
            shake = patrik.Shake(amplitude=40, duration_ms=duration_ms)
            crossings, previous = 0, 0
            for step in range(1, 400):
                dx = shake.offset(step / 400)[0]
                if dx and previous and (dx > 0) != (previous > 0):
                    crossings += 1
                if dx:
                    previous = dx
            return crossings / (duration_ms / 1000.0)

        short = wobbles_per_second(420)
        long_shake = wobbles_per_second(1680)
        self.assertAlmostEqual(short, long_shake, delta=short * 0.25)

    def test_the_default_length_is_a_share_of_the_base_animation(self):
        self.assertEqual(patrik.SHAKE_MS,
                         patrik.BASE_MS * patrik.SHAKE_SHARE)

    def test_a_zero_amplitude_shake_is_simply_still(self):
        # Config can say 0 to keep the glyphs and drop the jiggle.
        shake = patrik.Shake(amplitude=0)
        for step in range(21):
            self.assertEqual(shake.offset(step / 20), (0, 0))



# ---------------------------------------------------------------------------
# The wiring into the panel. Everything above needs no display; everything below
# builds a real CostMeter, for the reason tests/test_widget.py gives for its own
# window tests -- the questions here are not what the code asks for but what GTK
# and the window manager actually do with it, and on Windows those came apart
# once already.


def a_state(written, turn_usd=0.25, session_usd=1.0):
    """A state.json whose `updated_at` is what says whether a turn is new.

    Written near the present rather than at a round epoch: anything older than
    summary.STALE_AFTER_SECONDS is stale, and a stale panel deliberately cancels
    its animations -- which would make every assertion below pass or fail for a
    reason that has nothing to do with Patrik mode.
    """
    return {"updated_at": datetime.fromtimestamp(written).astimezone().isoformat(),
            "last_turn_usd": turn_usd,
            "session": {"id": "s1", "usd": session_usd},
            "today_usd": 1.0,
            "window_5h": {"usd": 2.0},
            "window_7d": {"usd": 3.0},
            "limits": None,
            "unknown_models": []}


class PanelTest(TempHome):
    """A real panel, torn down after each test, sharing one COST_METER_HOME."""

    def setUp(self):
        super().setUp()
        # A clock the test drives. The burst advances by real elapsed time, which
        # is right on screen and useless here: a tight loop of on_patrik_frame
        # would see a dt of nearly zero every time, so the glyphs would never
        # fade, the shake would never leave progress 0, and every assertion below
        # would pass without exercising anything.
        self.clock = 10_000.0
        patcher = unittest.mock.patch.object(
            widget.time, "monotonic", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = widget.CostMeter()
        # No main loop here, so the close handler would turn teardown into a
        # Gtk-CRITICAL.
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)
        self.addCleanup(self.window.update_config,
                        lambda c: c.pop("widget_position", None))
        self.addCleanup(self.window.update_config,
                        lambda c: c.pop(widget.PATRIK_KEY, None))

    def config(self):
        return store.read_json(paths.config_path(), default={}) or {}

    def turn(self, turn_usd=0.25, session_usd=1.0):
        """Land a turn: a state.json with a new `updated_at`, then a repaint.

        Each call steps a second further back, because `updated_at` moving is the
        whole test of whether a turn is new and two calls inside one clock tick
        would produce the same string.
        """
        self._turns = getattr(self, "_turns", 0) + 1
        store.write_json_atomic(
            paths.state_path(),
            a_state(time.time() - self._turns, turn_usd, session_usd))
        self.window.refresh()

    def tick(self, seconds=0.016):
        """One frame, `seconds` after the last. Returns what GLib would see."""
        self.clock += seconds
        return self.window.on_patrik_frame()

    def drive(self, limit=4000):
        """Frames until the burst reports itself finished. True if it did."""
        for _ in range(limit):
            if not self.tick():
                return True
        return False


@unittest.skipUnless(HAS_DISPLAY, "no display")
class MenuTest(PanelTest):
    """Where the item sits, and which way its caption points.

    The position is asserted rather than left to reading the source because it
    was the request: directly under `Refresh now`, not appended at the end next
    to `Quit`, where a celebration toggle would sit beside the one item nobody
    wants to hit by accident.
    """

    def captions(self):
        return [caption for caption, _ in self.window.menu_entries()]

    def test_it_sits_directly_under_refresh_now(self):
        captions = self.captions()
        self.assertEqual(captions[captions.index("Refresh now") + 1],
                         "Set Patrik mode on")

    def test_the_caption_says_what_the_click_will_do(self):
        # Read as the menu is built rather than cached, exactly as the
        # auto-launch pause is: another process writes the same file.
        self.window.set_patrik(True)
        self.assertIn("Set Patrik mode off", self.captions())
        self.window.set_patrik(False)
        self.assertIn("Set Patrik mode on", self.captions())

    def test_every_entry_still_has_a_handler(self):
        for caption, handler in self.window.menu_entries():
            self.assertTrue(callable(handler), caption)


@unittest.skipUnless(HAS_DISPLAY, "no display")
class ToggleTest(PanelTest):
    def test_it_is_off_until_it_is_asked_for(self):
        """A panel that celebrated unasked would be a panel nobody can read."""
        self.assertFalse(self.window.patrik_enabled())

    def test_the_setting_outlives_the_panel(self):
        self.window.set_patrik(True)
        second = widget.CostMeter()
        second.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(second.destroy)
        self.assertTrue(second.patrik_enabled())

    def test_toggling_leaves_the_window_position_alone(self):
        # The same file carries the position and the scale, and every writer of
        # it goes through update_config for exactly this reason.
        self.window.update_config(
            lambda c: c.__setitem__("widget_position", [7, 9]))
        self.window.set_patrik(True)
        self.window.set_patrik(False)
        self.assertEqual(self.config().get("widget_position"), [7, 9])

    def test_turning_it_off_leaves_no_key_behind(self):
        self.window.set_patrik(True)
        self.window.set_patrik(False)
        self.assertNotIn(widget.PATRIK_KEY, self.config())

    def test_turning_it_off_takes_the_overlay_down(self):
        # The overlay is a second always-on-top window. Leaving one parked over
        # the desktop after the mode was switched off is a window the user
        # cannot see, cannot reach and did not ask for.
        self.window.set_patrik(True)
        self.turn()
        self.assertIsNotNone(self.window.overlay)
        self.window.set_patrik(False)
        self.assertIsNone(self.window.overlay)


@unittest.skipUnless(HAS_DISPLAY, "no display")
class PanelScaleTest(PanelTest):
    """The spray is the panel's size, not a fixed one.

    The panel is drag-resizable between 0.7 and 3.0, and glyphs that stayed the
    same 13-24 px on a panel dragged to three times the size read as a fixed
    speck sitting on top of the meter rather than as money coming out of it.
    """

    def sizes(self):
        return [p.size for p in self.window.swarm.particles()]

    def test_a_larger_panel_throws_larger_glyphs(self):
        # Compared floor against ceiling rather than average against average:
        # every size is drawn from a range, and only the ranges failing to
        # overlap says the scale reached them at all.
        self.window.set_patrik(True)
        self.window.apply_scale(1.0)
        self.turn()
        self.assertTrue(self.drive(), "the burst never finished")
        small = max(self.sizes() or [patrik.SIZE_MAX])

        self.window.apply_scale(2.0)
        self.turn()
        self.assertGreater(min(self.sizes()), small)

    def test_a_resize_mid_celebration_reaches_the_glyphs_still_to_come(self):
        """The scale is read per frame, not frozen when the turn landed.

        Dragging the panel larger while the glyphs are flying is ordinary --
        the drag is how the size is set -- and the spray has to follow the panel
        it is coming out of rather than finish at the old size.
        """
        self.window.set_patrik(True)
        self.window.apply_scale(1.0)
        self.turn()
        self.window.apply_scale(3.0)
        for _ in range(60):
            self.tick()
        self.assertGreater(max(self.sizes()), patrik.SIZE_MAX)

    def test_they_start_inside_the_panel_at_every_scale(self):
        """A glyph may fly out of the panel; none may begin outside it.

        The whole effect is money leaving the table, and one that appeared in
        the margin already outside would be a glyph belonging to nothing. The
        size is scaled and the rectangle is not -- `panel_rect` is already in
        overlay pixels, and scaling it too would throw the spawn points clear of
        the panel.
        """
        self.window.set_patrik(True)
        for scale in (0.7, 1.0, 3.0):
            with self.subTest(scale=scale):
                self.window.apply_scale(scale)
                self.turn()
                x, y, width, height = self.window.panel_rect()
                for particle in self.window.swarm.particles():
                    self.assertGreaterEqual(particle.x, x)
                    self.assertLessEqual(particle.x, x + width)
                    self.assertGreaterEqual(particle.y, y)
                    self.assertLessEqual(particle.y, y + height)
                self.assertTrue(self.drive(), "the burst never finished")


@unittest.skipUnless(HAS_DISPLAY, "no display")
class SessionRateTest(PanelTest):
    """A session deep into real money throws them faster.

    Counted over a window shorter than the shortest life, so nothing has died
    yet and the count is everything thrown rather than everything surviving.
    """

    def glyphs_after(self, frames=30):
        for _ in range(frames):
            self.tick()
        return len(self.window.swarm.particles())

    def test_a_deep_session_throws_them_faster_than_a_quiet_one(self):
        self.window.set_patrik(True)
        self.turn(session_usd=1.0)
        quiet = self.glyphs_after()
        self.assertTrue(self.drive(), "the burst never finished")

        self.turn(session_usd=30.0)
        self.assertGreater(self.glyphs_after(), quiet)

    def test_the_session_decides_it_rather_than_the_turn(self):
        # The turn already has its say: it sets the length. A big turn early in
        # a quiet session is still a quiet session.
        self.window.set_patrik(True)
        self.turn(turn_usd=9.0, session_usd=1.0)
        quiet = self.glyphs_after()
        self.assertTrue(self.drive(), "the burst never finished")

        self.turn(turn_usd=0.01, session_usd=30.0)
        self.assertGreater(self.glyphs_after(), quiet)

    def test_a_session_with_no_figure_still_celebrates(self):
        """state.json can carry a null there and the panel must not die of it."""
        self.window.set_patrik(True)
        store.write_json_atomic(paths.state_path(),
                                dict(a_state(time.time() - 1), session=None))
        self.window.refresh()
        self.assertTrue(self.window.swarm.running())


@unittest.skipUnless(HAS_DISPLAY, "no display")
class CelebrationTest(PanelTest):
    """What makes the glyphs fly, and -- mostly -- what must not."""

    def test_a_new_turn_throws_glyphs(self):
        self.window.set_patrik(True)
        self.turn()
        self.assertTrue(self.window.swarm.running())

    def test_a_new_turn_throws_nothing_while_the_mode_is_off(self):
        self.turn()
        self.assertFalse(self.window.swarm.running())

    def test_a_repaint_that_is_not_a_turn_throws_nothing(self):
        """refresh() runs four ways and only one of them is a turn.

        The file monitor, the 60-second staleness poll, `Refresh now` and
        __init__ all re-read a state.json that has not changed. A burst driven by
        anything but `updated_at` moving would spray the panel every minute.
        """
        self.window.set_patrik(True)
        self.turn()
        self.assertTrue(self.drive(), "the burst never finished")
        self.window.refresh()          # same state.json, no new turn
        self.assertFalse(self.window.swarm.running())

    def test_switching_it_on_does_not_celebrate_by_itself(self):
        # Turning the mode on is not a turn. The first burst waits for the next
        # one, which is what the menu item promises.
        self.window.set_patrik(True)
        self.assertFalse(self.window.swarm.running())

    def test_the_first_repaint_after_switching_on_is_not_a_turn_either(self):
        """__init__ reads a state.json that already has a last turn in it.

        Without this the panel would celebrate a turn that had merely been read
        off disk -- the same trap `Roll.replay` documents for the counting rows.
        """
        self.turn()              # a turn the panel has already seen
        self.window.set_patrik(True)
        self.window.refresh()
        self.assertFalse(self.window.swarm.running())

    def test_a_panel_opening_on_an_existing_turn_does_not_celebrate(self):
        """The figure on disk at startup has not just been charged.

        __init__ runs a refresh of its own, and to that refresh every stamp is a
        new one. Celebrating there would throw a burst for a turn the panel had
        only read -- and, because auto-launch opens a panel at every session
        start, it would do it on every single session.
        """
        self.window.set_patrik(True)
        self.turn()
        self.assertTrue(self.drive(), "the burst never finished")
        second = widget.CostMeter()
        second.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(second.destroy)
        self.assertFalse(second.swarm.running())
        self.assertIsNone(second.overlay)

    def test_a_first_ever_turn_is_still_celebrated(self):
        """The case that separates `_opened` from "no stamp yet".

        A fresh install has no state.json, so the panel's opening refresh reads
        nothing and records no stamp. The next turn is then both the first this
        panel has seen and a real charge, and it is the one burst that most
        deserves to happen.
        """
        self.assertIsNone(self.window._turn_stamp)
        self.window.set_patrik(True)
        self.turn()
        self.assertTrue(self.window.swarm.running())

    def test_the_frame_timer_stops_once_the_last_glyph_is_gone(self):
        self.window.set_patrik(True)
        self.turn()
        self.assertIsNotNone(self.window._patrik_source)
        self.assertTrue(self.drive(), "the burst never finished")
        self.assertFalse(self.window.swarm.running())
        self.assertIsNone(self.window._patrik_source)

    def test_destroying_the_panel_takes_the_frame_timer_with_it(self):
        # A GLib timeout belongs to the main context rather than to the widget
        # that registered it, so a destroyed panel would otherwise keep waking
        # up at 16 ms for the life of the process.
        self.window.set_patrik(True)
        self.turn()
        source = self.window._patrik_source
        self.assertIn(source, self.window.sources)
        self.window.stop_timers()
        self.assertIsNone(self.window._patrik_source)


@unittest.skipUnless(HAS_DISPLAY, "no display")
class ShakeWiringTest(PanelTest):
    """The flinch, and the position bookkeeping it must not disturb.

    This is the failure the whole design was arranged around. The panel shakes by
    moving its own window, and widget.py reads a window away from its anchor as a
    position the user chose and writes it to config.json. Get this wrong and the
    panel walks across the screen a few pixels per turn, permanently, in a file
    the user never edited.
    """

    def test_the_window_ends_up_exactly_where_it_started(self):
        self.window.set_patrik(True)
        before = tuple(self.window.get_position())
        self.turn()
        self.assertTrue(self.drive(), "the burst never finished")
        self.assertEqual(tuple(self.window.get_position()), before)

    def test_the_shake_is_not_recorded_as_a_position_the_user_chose(self):
        self.window.set_patrik(True)
        self.turn()
        self.assertTrue(self.drive(), "the burst never finished")
        # What the debounce would eventually call. Called directly because the
        # test has no main loop for the 700 ms timer to fire in.
        self.window._persist_position()
        self.assertNotIn("widget_position", self.config())

    def test_a_shake_after_the_user_moved_the_panel_keeps_their_position(self):
        """The case a naive shake gets wrong.

        `_persist_position` clears the anchor once the user has chosen a spot, so
        from then on `at_anchor()` is False and every shake looks like a fresh
        drag. The saved coordinates have to be the ones the user dropped it at,
        not wherever a frame of the wobble happened to leave it.
        """
        self.window.update_config(
            lambda c: c.__setitem__("widget_position", [140, 160]))
        # Moved outright rather than through place(), which no longer restores a
        # saved position -- see SpawnPositionTest in tests/test_widget.py.
        self.window.move(140, 160)
        self.window.user_positioned = True
        self.window._anchor = None
        chosen = tuple(self.window.get_position())
        self.window.set_patrik(True)
        self.turn()
        self.assertTrue(self.drive(), "the burst never finished")
        self.window._persist_position()
        self.assertEqual(tuple(self.window.get_position()), chosen)
        self.assertEqual(tuple(self.config().get("widget_position")), chosen)

    def test_a_debounce_already_in_flight_does_not_save_a_frame_of_the_wobble(self):
        """The walking bug, by the one route that actually reaches it.

        Letting go of a drag starts a 700 ms debounce. If a turn lands inside that
        window, the shake is moving the panel at the moment the timer fires, and
        what reaches config.json is wherever the wobble happened to be -- a
        position nobody chose, restored at every session from then on, in a file
        the user never edited.

        Landing the window back on its base is not enough to prevent this, which
        is why the test drives exactly one frame and then fires the debounce by
        hand: at that instant the panel is genuinely off its base, and every
        no-op-looking guard in the position code has already been passed.
        """
        self.window.update_config(
            lambda c: c.__setitem__("widget_position", [140, 160]))
        # Moved outright rather than through place(), which no longer restores a
        # saved position -- see SpawnPositionTest in tests/test_widget.py.
        self.window.move(140, 160)
        self.window.user_positioned = True
        self.window._anchor = None
        chosen = tuple(self.window.get_position())
        self.window.set_patrik(True)
        self.turn()
        self.tick()
        self.assertNotEqual(tuple(self.window.get_position()), chosen,
                            "the panel should be mid-wobble here")
        # What the debounce does when it fires. Called directly because the test
        # has no main loop for a 700 ms timer to arrive in.
        self.window._persist_position()
        self.assertEqual(tuple(self.config().get("widget_position") or ()),
                         chosen)

    def test_switching_the_mode_off_mid_wobble_lands_the_window(self):
        """Nothing else will put it back.

        The frame at progress 1.0 is what normally returns the panel to its base,
        and an interrupted shake never reaches that frame. A window abandoned a
        few pixels out is the walking bug arriving by the back door: the next
        debounce records the offset as a position, and it sticks.
        """
        self.window.set_patrik(True)
        before = tuple(self.window.get_position())
        self.turn()
        self.tick()
        self.assertNotEqual(tuple(self.window.get_position()), before)
        self.window.set_patrik(False)
        self.assertEqual(tuple(self.window.get_position()), before)

    def test_the_wobble_never_strays_further_than_its_amplitude(self):
        """Every offset is measured from the base, not from the last frame.

        Read live, a frame would add its offset to the offset the frame before it
        left behind, the errors would compound, and the panel would wander off
        across the desktop for the length of the burst. `end_patrik` puts it back
        on its base at the end, so nothing afterwards would show it had happened
        -- which is exactly why the straying has to be caught while it strays.
        """
        self.window.set_patrik(True)
        base_x, base_y = self.window.get_position()
        self.turn()
        for _ in range(120):
            self.tick()
            x, y = self.window.get_position()
            self.assertLessEqual(abs(x - base_x), patrik.SHAKE_AMPLITUDE)
            self.assertLessEqual(abs(y - base_y), patrik.SHAKE_AMPLITUDE)

    def test_the_panel_really_does_move_during_the_shake(self):
        self.window.set_patrik(True)
        before = tuple(self.window.get_position())
        self.turn()
        seen = set()
        for _ in range(60):
            self.tick()
            seen.add(tuple(self.window.get_position()))
        self.assertNotEqual(seen, {before}, "the panel never flinched")


@unittest.skipUnless(HAS_DISPLAY, "no display")
class OverlayTest(PanelTest):
    def test_the_overlay_reaches_beyond_the_panel_on_every_side(self):
        """Room for the glyphs to be seen crossing the edge rather than clipped."""
        self.window.set_patrik(True)
        self.turn()
        panel = self.window.get_size()
        overlay = self.window.overlay.get_size()
        self.assertGreater(overlay.width, panel.width)
        self.assertGreater(overlay.height, panel.height)

    def test_the_panel_rectangle_sits_inside_the_overlay(self):
        # What Swarm.burst is handed, so the glyphs start on the rows rather than
        # in a corner of the margin.
        self.window.set_patrik(True)
        self.turn()
        x, y, width, height = self.window.panel_rect()
        overlay = self.window.overlay.get_size()
        self.assertGreater(x, 0)
        self.assertGreater(y, 0)
        self.assertLessEqual(x + width, overlay.width)
        self.assertLessEqual(y + height, overlay.height)

    def test_the_overlay_stays_out_of_the_taskbar(self):
        # UTILITY for the same reason the panel is: it is what win32 turns into
        # WS_EX_TOOLWINDOW, and a decoration window with an Alt-Tab entry is
        # worse than no effect at all.
        self.window.set_patrik(True)
        self.turn()
        self.assertEqual(self.window.overlay.get_type_hint(),
                         Gdk.WindowTypeHint.UTILITY)

    def test_without_a_compositor_the_panel_carries_on(self):
        """No overlay is a panel with no glyphs, never a panel that fell over.

        The precedent is this project's own history: the panel died on `import
        gi` because one DLL had no reputation with Smart App Control. A
        celebration must never be able to take the meter with it.
        """
        self.window.set_patrik(True)
        with unittest.mock.patch.object(
                Gdk.Screen, "get_rgba_visual", return_value=None):
            self.turn()
        self.assertIsNone(self.window.overlay)
        self.assertEqual(self.window.last_turn.get_text(), "+$0.25")


@unittest.skipUnless(HAS_DISPLAY and os.name == "nt",
                     "Windows click-through rules")
class ClickThroughTest(PanelTest):
    """The overlay must not eat clicks meant for whatever is underneath it.

    Measured, not assumed, and this is the second time this project has had to:
    GDK's win32 backend accepts `set_pass_through(True)` and an empty input
    shape and does nothing with either -- neither sets WS_EX_TRANSPARENT, which
    is the documented condition for a window that clicks fall through. Left at
    the GTK call, a transparent window the size of the panel plus its margin
    would swallow every click in that rectangle for as long as the mode was on,
    and nothing on screen would explain why the desktop had stopped responding.

    Exactly the shape of the taskbar bug in tests/test_widget.py: a hint the
    backend takes and drops, and code that reads as though it had worked.
    """

    def test_clicks_fall_through_the_overlay(self):
        self.window.set_patrik(True)
        self.turn()
        hwnds = own_hwnds_titled(widget.OVERLAY_TITLE)
        self.assertEqual(len(hwnds), 1, "expected exactly one overlay window")
        exstyle = ctypes.windll.user32.GetWindowLongW(
            ctypes.c_void_p(hwnds[0]), GWL_EXSTYLE)
        self.assertTrue(exstyle & WS_EX_TRANSPARENT,
                        f"overlay would swallow clicks: exstyle 0x{exstyle:08X}")


@unittest.skipUnless(HAS_DISPLAY, "no display")
class LengthTest(PanelTest):
    """The celebration's length comes from what the turn cost.

    Driven on the test's own clock, so these are counts of virtual milliseconds
    rather than a race against the machine.
    """

    STEP = 0.016

    def length_ms(self, turn_usd):
        """How long the whole celebration ran, in milliseconds."""
        self.window.set_patrik(True)
        self.turn(turn_usd)
        frames = 0
        while self.tick(self.STEP):
            frames += 1
            if frames > 4000:
                self.fail("the celebration never finished")
        return frames * self.STEP * 1000.0

    def test_a_cheap_turn_runs_about_two_seconds(self):
        self.assertAlmostEqual(self.length_ms(0.0), 2000.0, delta=120.0)

    def test_a_forty_dollar_turn_runs_about_four(self):
        self.assertAlmostEqual(self.length_ms(40.0), 4000.0, delta=120.0)

    def test_nothing_outlives_the_animation(self):
        """The length is the promise, so the last glyph dies inside it.

        Without the life budget a glyph thrown near the end keeps falling for its
        own full lifetime, and a two-second celebration is still clearing up a
        second and a half later.
        """
        self.window.set_patrik(True)
        self.turn(0.0)
        frames = 0
        while self.tick(self.STEP):
            frames += 1
        self.assertLess(frames * self.STEP * 1000.0,
                        patrik.duration_ms(0.0) + 120.0)


@unittest.skipUnless(HAS_DISPLAY, "no display")
class ArrivalTest(PanelTest):
    """New glyphs keep coming for as long as the celebration runs."""

    def test_glyphs_keep_arriving_after_the_opening_burst(self):
        self.window.set_patrik(True)
        self.turn(40.0)
        seen = {id(p) for p in self.window.swarm.particles()}
        self.assertEqual(len(seen), patrik.BURST)
        for _ in range(60):
            self.tick()
            seen.update(id(p) for p in self.window.swarm.particles())
        self.assertGreater(len(seen), patrik.BURST,
                           "the spray stopped after the opening burst")

    def test_glyphs_are_still_arriving_late_in_a_long_celebration(self):
        """Not merely for the first moment of it.

        A four-second animation whose glyphs all arrived in the first half would
        spend its second half visibly running out.
        """
        self.window.set_patrik(True)
        self.turn(40.0)
        half = int(patrik.duration_ms(40.0) / 2.0 / 16.0)
        for _ in range(half):
            self.tick()
        before = {id(p) for p in self.window.swarm.particles()}
        for _ in range(30):
            self.tick()
        arrived = {id(p) for p in self.window.swarm.particles()} - before
        self.assertTrue(arrived, "nothing new arrived in the second half")

    def test_the_spray_stops_before_the_end(self):
        """Glyphs born in the last moments are specks, not money.

        Their life is clamped to what is left, so they would appear and vanish
        within a frame or two of the panel -- flicker rather than a celebration.
        """
        self.window.set_patrik(True)
        self.turn(0.0)
        # Just past the floor, then every remaining frame is checked. A window of
        # a few frames is not enough to catch this: at nine glyphs a second, 80 ms
        # owes less than one, so a spray running to the very end would go unnoticed
        # most of the time.
        stops_at = (patrik.duration_ms(0.0) - patrik.EMIT_FLOOR * 1000.0) / 16.0
        for _ in range(int(stops_at) + 2):
            self.tick()
        settled = {id(p) for p in self.window.swarm.particles()}
        while self.tick():
            self.assertFalse(
                {id(p) for p in self.window.swarm.particles()} - settled,
                "a glyph was still being born in the final moments")


@unittest.skipUnless(HAS_DISPLAY, "no display")
class ShakeLengthTest(PanelTest):
    def test_the_flinch_lasts_a_share_of_the_celebration(self):
        """So it grows with the turn instead of being over in a blink.

        Measured as the last frame on which the panel is still off its base: with
        a fixed length that figure would not move between a cheap turn and an
        expensive one.
        """
        def moving_until_ms(turn_usd):
            self.window.set_patrik(True)
            self.turn(turn_usd)
            base = self.window._patrik_base
            last, frames = 0, 0
            while self.tick():
                frames += 1
                if tuple(self.window.get_position()) != base:
                    last = frames
            return last * 16.0

        cheap = moving_until_ms(0.0)
        dear = moving_until_ms(40.0)
        self.assertAlmostEqual(cheap, patrik.duration_ms(0.0) * patrik.SHAKE_SHARE,
                               delta=200.0)
        self.assertGreater(dear, cheap * 1.5)


if __name__ == "__main__":
    unittest.main()
