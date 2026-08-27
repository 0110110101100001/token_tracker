"""Particle and shake maths for Patrik mode. Pure arithmetic, no GTK.

Patrik mode is the panel celebrating what a turn cost: money glyphs are thrown
out of the table and the panel itself flinches. It is off by default and lives
behind a menu item, because a panel that did this unasked would be a panel
nobody could read.

Separate from widget.py for the reason cost_meter/roll.py already is: where a
glyph is at moment `t` and how solid it looks are arithmetic, and arithmetic can
be tested without a display or a main loop. The widget owns the overlay window,
the cairo context and the frame timer; this module owns the trajectories.

Two decisions here are load-bearing rather than decorative:

The glyphs are thrown *up* and pulled down, instead of drifting. A glyph that
only ever sank read as the panel leaking, and one that only rose left the screen
too fast to see. Up-then-down also guarantees the thing the effect is for -- a
glyph that starts anywhere inside the panel is certain to cross an edge, at the
top on the way up or at the bottom on the way back -- so no part of a burst dies
inside the table looking like dirt on the window.

`Shake.offset` reaches exactly (0, 0) at both ends. The widget shakes by moving
the window, and widget.py treats a window away from its anchor as a position the
user chose and writes it to config.json. A shake landing a pixel out would be
saved as a deliberate move, and the panel would walk across the screen a pixel
per turn. Both endpoints fall out of the maths -- sin(0) is 0 and the decay is 0
at the end -- and `offset` clamps past the end on top of that, because progress
comes from wall-clock time and the frame after the last one is ordinary.
"""

import math
import random

# The four that have glyphs. U+1FA99 COIN is the obvious fifth and is
# deliberately absent: measured with Pango against Segoe UI Emoji on Windows 10,
# it has no glyph and draws as an empty box. Money-mouth face, heavy dollar sign,
# money bag, banknote.
GLYPHS = ("\U0001F911", "\U0001F4B2", "\U0001F4B0", "\U0001F4B5")

# How many glyphs a turn is worth. Enough to read as a spray, few enough that a
# 200-pixel-wide panel is not hidden behind them.
BURST = 14

# Pixels per second squared. Strong, because the glyphs have to clear the panel
# and fall back out of it inside their lifetime rather than hanging in the air.
GRAVITY = 900.0
# The upward throw, in pixels per second. The spread is what stops the burst
# moving as one sheet.
RISE_MIN, RISE_MAX = 240.0, 420.0
# Sideways drift, symmetric: the burst opens outwards instead of leaning.
DRIFT = 170.0
# Seconds a glyph lasts. The floor is above the time it takes to rise and fall
# back past where it started, so every glyph outlives its own escape.
LIFE_MIN, LIFE_MAX = 1.4, 2.4
# Glyph size in pixels at scale 1.0. The widget multiplies by the panel's scale,
# so the spray grows with the window rather than staying a fixed speck on a
# panel somebody dragged twice as large.
SIZE_MIN, SIZE_MAX = 13.0, 24.0

# The flinch: pixels at the widest, and how many times it crosses back over.
# Three is a flinch; one is a lean, and six is a fault.
SHAKE_AMPLITUDE = 6
SHAKE_CYCLES = 3.0
# The vertical share. Mostly sideways, because a panel that bounced vertically
# read as the window manager dropping it rather than as the panel reacting.
SHAKE_VERTICAL = 0.6
SHAKE_MS = 420.0


def _clamp(p):
    return 0.0 if p < 0.0 else 1.0 if p > 1.0 else p


class Particle:
    """One glyph in flight.

    Plain attributes rather than properties: the draw handler reads x, y, alpha
    and size once per glyph per frame, and `alpha` is stored rather than derived
    so a particle dropped from the swarm stops changing when it stops being
    advanced.
    """

    __slots__ = ("glyph", "x", "y", "vx", "vy", "size", "age", "life", "alpha")

    def __init__(self, glyph, x, y, vx, vy, size, life):
        self.glyph = glyph
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.size = size
        self.life = life
        self.age = 0.0
        self.alpha = 1.0

    def advance(self, dt):
        """Move by `dt` seconds and fade by the same. True while still alive.

        Velocity is integrated before position rather than after, which costs a
        half-frame of accuracy nobody can see and keeps `vy` monotonic -- the
        property that says gravity only ever pulls one way.
        """
        self.vy += GRAVITY * dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        self.age += dt
        self.alpha = _clamp(1.0 - self.age / self.life)
        return self.age < self.life


class Swarm:
    """The glyphs currently in the air, and where they are next frame.

    Bursts accumulate rather than replace. Two turns landing seconds apart is
    ordinary, and the second one wiping what the first still had in flight would
    read as the effect stuttering.

    `rng` is injectable so a test can seed it. Nothing here uses the module-level
    `random`, because a failure that cannot be reproduced is a failure that
    cannot be fixed.
    """

    def __init__(self, glyphs=GLYPHS, rng=None):
        self._glyphs = tuple(glyphs)
        self._rng = rng or random.Random()
        self._alive = []

    def burst(self, count, rect):
        """Throw `count` glyphs out of `rect`, the panel inside the overlay.

        They start inside the panel because that is what says they came from the
        table. The overlay window is the panel plus a margin, so `rect` is
        offset from the origin and every coordinate here is in overlay space.
        """
        x, y, width, height = rect
        for _ in range(count):
            self._alive.append(Particle(
                glyph=self._rng.choice(self._glyphs),
                x=x + self._rng.uniform(0.0, width),
                y=y + self._rng.uniform(0.0, height),
                vx=self._rng.uniform(-DRIFT, DRIFT),
                vy=-self._rng.uniform(RISE_MIN, RISE_MAX),
                size=self._rng.uniform(SIZE_MIN, SIZE_MAX),
                life=self._rng.uniform(LIFE_MIN, LIFE_MAX),
            ))

    def frame(self, dt):
        """Advance every glyph by `dt` seconds. Returns the survivors.

        Dead glyphs are dropped rather than left at alpha zero: an invisible
        glyph still costs a text layout and a paint on every frame, and the
        widget stops its timer on `running()` going false, which an unemptied
        list would never do.
        """
        self._alive = [p for p in self._alive if p.advance(dt)]
        return list(self._alive)

    def particles(self):
        """The glyphs in the air, in the order they were thrown."""
        return list(self._alive)

    def running(self):
        return bool(self._alive)


class Shake:
    """The panel's flinch: a decaying wobble, in whole pixels, ending at zero.

    Progress-based like cost_meter/roll.py rather than dt-based like the swarm
    above, and the difference is not an inconsistency: the glyphs each have their
    own age, while the shake is one movement with one clock, so the widget can
    hand it elapsed-over-duration exactly as it does for a roll.
    """

    def __init__(self, amplitude=SHAKE_AMPLITUDE, cycles=SHAKE_CYCLES):
        self._amplitude = amplitude
        self._cycles = cycles

    def offset(self, p):
        """Where the window sits at progress `p`, relative to its anchor.

        Whole pixels because Gtk.Window.move takes ints and would truncate a
        float silently -- which would bias every offset towards zero and lose
        the wobble on a small amplitude.

        The two axes run at different frequencies so the panel jitters rather
        than sliding along a diagonal. Neither carries a phase offset, which is
        what makes p=0 land on (0, 0) by arithmetic instead of by a special case.
        """
        p = _clamp(p)
        decay = 1.0 - p
        angle = 2.0 * math.pi * self._cycles * p
        dx = self._amplitude * decay * math.sin(angle)
        dy = self._amplitude * SHAKE_VERTICAL * decay * math.sin(angle * 1.7)
        return int(round(dx)), int(round(dy))
