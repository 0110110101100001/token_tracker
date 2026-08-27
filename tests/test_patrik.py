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

import random
import unittest

from cost_meter import patrik


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


if __name__ == "__main__":
    unittest.main()
