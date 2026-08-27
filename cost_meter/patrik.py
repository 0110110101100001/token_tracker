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

# The opening handful, thrown the instant the turn lands, so the celebration
# starts full rather than trickling up to speed.
BURST = 14
# And the rest, in glyphs per second, for as long as the animation runs. One
# opening burst was the first version of this, and on a four-second animation it
# left three and a half seconds of glyphs merely falling -- the celebration
# visibly ran out well before it ended.
RATE = 9.0

# How long a celebration lasts: a fixed base plus a share of what the turn cost,
# so an expensive turn gets the time to be watched instead of being crammed into
# the same window as a trivial one. Deliberately the same shape as
# `roll.duration_ms`, and deliberately twice its numbers -- the figures finish
# counting up around halfway through the glyphs, which reads as one event rather
# than two.
BASE_MS = 2000.0
MS_PER_USD = 50.0
# Emitting stops when less than this is left, in seconds. A glyph born in the
# final moments fades correctly -- the fade is measured against its own life --
# but it never travels far enough to be anything but a speck appearing and
# vanishing on the panel, which reads as flicker rather than as money.
EMIT_FLOOR = 0.35

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

# The flinch. Pixels at the widest, and the share of the animation it occupies --
# so it lengthens with the turn like everything else, rather than being over in a
# blink on a four-second celebration.
SHAKE_AMPLITUDE = 6
SHAKE_SHARE = 0.4
SHAKE_MS = BASE_MS * SHAKE_SHARE
# Milliseconds per wobble, which is what a fixed cycle count cannot give. Stretch
# a three-cycle shake from 420 ms to 1.6 s and it becomes a slow sway -- the panel
# leaning about rather than reacting. Holding the time per wobble constant keeps
# the character when the length changes.
MS_PER_WOBBLE = 140.0
# The vertical share. Mostly sideways, because a panel that bounced vertically
# read as the window manager dropping it rather than as the panel reacting.
SHAKE_VERTICAL = 0.6


def _clamp(p):
    return 0.0 if p < 0.0 else 1.0 if p > 1.0 else p


def duration_ms(turn_usd):
    """How long the celebration of a turn costing `turn_usd` should run.

    Absolute, because a length is a length: a correction can push a turn's cost
    below zero, and a negative duration would end the animation before it began.
    A missing figure is the base length rather than an error -- state.json can
    carry a null there, and the panel must not die of it.

    Unbounded on purpose, as in `roll.duration_ms`: the turns long enough to run
    long are the ones worth a long look.
    """
    return BASE_MS + abs(turn_usd or 0.0) * MS_PER_USD


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
        # The fraction of a glyph a frame was owed but could not deliver. At
        # 16 ms a frame, nine a second is 0.14 of a glyph per frame: truncated
        # and forgotten it is zero every time, and nothing would ever arrive
        # after the opening burst.
        self._pending = 0.0

    def burst(self, count, rect, max_life=None):
        """Throw `count` glyphs out of `rect`, the panel inside the overlay.

        They start inside the panel because that is what says they came from the
        table. The overlay window is the panel plus a margin, so `rect` is
        offset from the origin and every coordinate here is in overlay space.

        `max_life` is how much of the animation is left, in seconds. A glyph
        thrown near the end with a full lifetime would still be falling long
        after the celebration was supposed to be over, and the animation's length
        is the promise. Clamping is safe to do bluntly because the fade is
        measured against the glyph's own life: one given a third of a second
        fades smoothly across that third rather than blinking out.
        """
        x, y, width, height = rect
        for _ in range(count):
            life = self._rng.uniform(LIFE_MIN, LIFE_MAX)
            if max_life is not None:
                life = min(life, max_life)
            if life <= 0.0:
                # Born dead. Skipped rather than appended: it would be painted
                # once at alpha zero and dropped on the very next frame.
                continue
            self._alive.append(Particle(
                glyph=self._rng.choice(self._glyphs),
                x=x + self._rng.uniform(0.0, width),
                y=y + self._rng.uniform(0.0, height),
                vx=self._rng.uniform(-DRIFT, DRIFT),
                vy=-self._rng.uniform(RISE_MIN, RISE_MAX),
                size=self._rng.uniform(SIZE_MIN, SIZE_MAX),
                life=life,
            ))

    def emit(self, dt, rect, rate=RATE, max_life=None):
        """Spawn `rate` glyphs per second, over a frame of `dt` seconds.

        The whole of the celebration is fed through here after the opening burst,
        so glyphs keep arriving for as long as it runs instead of the panel
        spraying once and then watching the spray fall.
        """
        self._pending += rate * dt
        count = int(self._pending)
        self._pending -= count
        self.burst(count, rect, max_life=max_life)

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

    def __init__(self, amplitude=SHAKE_AMPLITUDE, duration_ms=SHAKE_MS):
        self._amplitude = amplitude
        # Cycles from the duration rather than fixed, so the wobble runs at the
        # same speed however long the shake is. At least one, because a shake
        # shorter than a single wobble would return a fragment of a sine and read
        # as the panel jumping aside and back.
        self._cycles = max(1.0, duration_ms / MS_PER_WOBBLE)

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
