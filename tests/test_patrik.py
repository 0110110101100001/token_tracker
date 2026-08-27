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


def a_state(written, turn_usd=0.25):
    """A state.json whose `updated_at` is what says whether a turn is new.

    Written near the present rather than at a round epoch: anything older than
    summary.STALE_AFTER_SECONDS is stale, and a stale panel deliberately cancels
    its animations -- which would make every assertion below pass or fail for a
    reason that has nothing to do with Patrik mode.
    """
    return {"updated_at": datetime.fromtimestamp(written).astimezone().isoformat(),
            "last_turn_usd": turn_usd,
            "session": {"id": "s1", "usd": 1.0},
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

    def turn(self, turn_usd=0.25):
        """Land a turn: a state.json with a new `updated_at`, then a repaint.

        Each call steps a second further back, because `updated_at` moving is the
        whole test of whether a turn is new and two calls inside one clock tick
        would produce the same string.
        """
        self._turns = getattr(self, "_turns", 0) + 1
        store.write_json_atomic(paths.state_path(),
                                a_state(time.time() - self._turns, turn_usd))
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
                         "Patrik mode")

    def test_the_caption_says_what_the_click_will_do(self):
        # Read as the menu is built rather than cached, exactly as the
        # auto-launch pause is: another process writes the same file.
        self.window.set_patrik(True)
        self.assertIn("Patrik mode off", self.captions())
        self.window.set_patrik(False)
        self.assertIn("Patrik mode", self.captions())

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
        self.window.place()
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
        self.window.place()
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


if __name__ == "__main__":
    unittest.main()
