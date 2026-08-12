"""Tween maths for the rolling figures. Pure arithmetic, no GTK.

The panel used to snap: `set_text` with the new number, and a turn that cost $4
looked exactly like one that cost $0.04 -- the figure was simply different next
time you glanced at it. Rolling up to the new value is what makes the change
itself visible.

Separate from widget.py for the reason the row text already is: what to draw at
moment `p` is arithmetic, and arithmetic can be tested without a display or a
main loop. The widget owns the timer, the labels and the CSS classes; this
module owns the curve.

`p` is progress through the roll, 0 to 1. Every function clamps it, because the
caller derives it from wall-clock time against the duration and the frame that
lands past the end must still produce the target rather than an overshoot.

Nothing here touches contrast or opacity. The digits were once drawn dimmer at
speed, as a stand-in for motion blur; on screen that read as the row blinking
rather than smearing, so the only thing that moves now is the figure itself.
"""

# How long a roll lasts: a fixed base plus a share of the distance travelled, so
# a big jump gets the time to be watched rather than being crammed into the same
# window as a small one. 40 dollars lands on 2 s, which is the pace this was
# tuned against; the base is what a fraction-of-a-dollar turn gets.
BASE_MS = 1000.0
MS_PER_USD = 25.0


def _clamp(p):
    return 0.0 if p < 0.0 else 1.0 if p > 1.0 else p


def ease_in_out(p):
    """Cubic ease-in-out: slow start, fast middle, slow settle.

    Cubic rather than the cheaper quadratic because the ends are where the
    figure is legible, and the cubic spends more of the roll there -- the middle
    is churn either way, and the digits are drawn at full contrast throughout.

    Both endpoints are exact, not merely close: the final frame writes this
    value to the label, so any drift leaves a total on screen that is a fraction
    of a cent away from the one in state.json.
    """
    p = _clamp(p)
    if p < 0.5:
        return 4 * p ** 3
    return 1 - (-2 * p + 2) ** 3 / 2


def value_at(start, end, p):
    """The figure to display at progress `p`.

    Direction is not assumed. When a 5-hour block resets, the row drops from its
    running total to zero, and that fall is as much a change worth showing as
    the rises are.
    """
    return start + (end - start) * ease_in_out(p)


def duration_ms(distance):
    """How long a roll of `distance` dollars should take.

    Linear in the distance rather than constant, because a fixed duration makes
    a $40 turn and a $0.40 one move at wildly different speeds to cover their
    ground in the same time -- and it is the fast one, the expensive turn, that
    most deserves to be legible on the way up.

    Unbounded on purpose: the only moves large enough to run long are a 5-hour
    block resetting, and that is a figure worth a long look. Direction does not
    change the length, so a reset takes as long falling as it took rising.
    """
    return BASE_MS + abs(distance) * MS_PER_USD


class Roll:
    """Which rows are rolling, from what, to what -- and what to draw at `p`.

    Deciding is separated from drawing because this is where the animation goes
    wrong if it goes wrong at all. `refresh()` runs from the file monitor, from
    the 60-second staleness poll, from `Refresh now` and from `__init__`, and
    only one of those four is a turn that actually cost money. A roll driven by
    "the text is different" would restart on every poll and would roll up from
    zero at startup; a roll driven by the remembered *target* per row does
    neither, and none of that needs a display to test.

    One shared progress for the whole panel rather than a clock per row: two
    rows almost always move together, and separate clocks would have them
    settle a frame or two apart for no reason anybody can see.
    """

    def __init__(self, min_delta=0.01):
        self._min_delta = min_delta
        # What each row was last asked to show. Compared against, rather than
        # against what is on screen, so a repeated state.json is a no-op even
        # while that row is mid-roll.
        self._targets = {}
        # What each row is showing now -- the tween's own output between frames,
        # which is what a mid-roll retarget has to start from.
        self._shown = {}
        self._legs = {}  # key -> (start, end) for the rows in flight

    def retarget(self, values):
        """Take in new figures. True when something needs animating from here.

        A row is set outright rather than rolled when there is nothing to roll
        from (first sighting, or a figure that was never recorded), when the
        figure is missing now, and when the move is smaller than `min_delta` --
        forty-odd frames for a fraction of a cent is noise, not information.
        """
        started = False
        for key, value in values.items():
            if key in self._targets and self._targets[key] == value:
                continue  # nothing new was asked for; leave any roll running
            self._targets[key] = value
            start = self._shown.get(key)
            if value is None or start is None or abs(value - start) < self._min_delta:
                self._legs.pop(key, None)
                self._shown[key] = value
                continue
            self._legs[key] = (start, value)
            started = True

        if started:
            # Progress is shared, so it goes back to zero for everybody. Rows
            # already in flight are re-based on what they are showing right now;
            # keeping their original start would snap them backwards first.
            self._legs = {key: (self._shown[key], end)
                          for key, (_, end) in self._legs.items()}
        return started

    def replay(self, key, value, start=0.0):
        """Count `key` up from `start` to `value`. True when that will animate.

        For the per-turn delta rather than a running total. `retarget` compares
        against the figure it was last handed and says nothing when that has not
        changed -- correct for a total, wrong for a delta, where two turns
        costing the same are still two turns and the row holding still would
        hide the second one. It would also run the row *downwards* whenever a
        cheap turn followed an expensive one, announcing a new charge with a
        fall.

        So every turn starts at zero. The intermediate figures then mean
        something on their own -- what this turn has cost so far -- rather than
        being the distance between two unrelated numbers.

        The caller decides when a turn is new; this only obeys. `refresh()` runs
        four times for every one turn, and replaying on each of them would count
        the same figure up again every minute.

        First sighting is set outright, as in `retarget`: the panel opens
        showing what the last turn cost, and counting that up on startup would
        assert a turn that has only just been read off disk.
        """
        self._targets[key] = value
        if (self._shown.get(key) is None or value is None
                or abs(value - start) < self._min_delta):
            self._legs.pop(key, None)
            self._shown[key] = value
            return False
        # Before the leg, not after: a `retarget` later in the same refresh
        # re-bases every leg in flight on what its row is showing, so this is
        # where the count's starting point has to already be.
        self._shown[key] = start
        self._legs[key] = (start, value)
        return True

    def distance(self):
        """The longest leg in flight, in dollars. 0.0 when nothing is rolling.

        The longest rather than one row's own, because progress is shared: the
        rows move together, so they need one duration, and the row with the
        furthest to go is the one that sets the pace. In practice the four are
        within a cent of each other -- the same turn is added to all of them --
        and they diverge only when a window resets.
        """
        return max((abs(end - start) for start, end in self._legs.values()),
                   default=0.0)

    def frame(self, p):
        """`{key: value}` for the rows in flight at progress `p`.

        At `p >= 1` the legs are retired, so the caller's `running()` check
        stops the timer on the same frame that lands the final figure.
        """
        drawn = {}
        for key, (start, end) in self._legs.items():
            self._shown[key] = value_at(start, end, p)
            drawn[key] = self._shown[key]
        if p >= 1.0:
            self._legs.clear()
        return drawn

    def cancel(self):
        """Stop rolling and land on the targets.

        Called when the state goes stale: those figures are no longer being
        presented as current, and animating them would say the opposite. The
        numbers themselves are still the last ones recorded, so the rows finish
        on them rather than freezing part-way.
        """
        for key in self._legs:
            self._shown[key] = self._targets[key]
        self._legs.clear()

    def running(self):
        return bool(self._legs)

    def moving(self, key):
        """Whether this one row is in flight.

        Only the turn row needs it, and only to tell a $0.00 it is counting up
        through apart from a $0.00 meaning no turn was recorded.
        """
        return key in self._legs

    def shown(self, key):
        """What `key` is displaying, or None if it has never shown a figure."""
        return self._shown.get(key)
