#!/usr/bin/env python3
"""Always-on-top cost meter, anchored bottom-right.

Reads data/state.json for the dollar figures and Claude Code's own usage cache
for the account's limit percentages — the latter directly, so a five-hour block
that resets between turns is noticed. Run it through run_widget.sh or
run_widget.cmd, which enter the pixi environment; on Linux that sets
GDK_BACKEND=x11 so the window can place and raise itself.
"""

import argparse
import ctypes
import os
import sys
import threading
import time
from datetime import datetime

import cairo
import gi

gi.require_version("Gtk", "3.0")
# Gdk 4.0 is also installed here; without this the bare import picks 4.0 and
# then collides with the Gtk 3.0 requirement above.
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import (Gdk, Gio, GLib, Gtk, Pango,  # noqa: E402
                           PangoCairo)

from cost_meter import (autolaunch, log, patrik, paths, roll, sound,  # noqa: E402
                        store, summary, usage_api, utilization)

MARGIN = 24
# Where a fresh panel opens, every time, whatever config.json remembers.
#
# What a saved position really remembers is which *monitor* the panel was on,
# and that is the part that goes wrong. Drag the panel onto a second screen and
# every session from then on opens it there -- faithfully, and invisibly to
# anyone watching the first screen. There is nothing to diagnose from: the
# launcher reports a spawned pid, the process runs, the window is mapped and
# visible, and the log has nothing to say, because nothing went wrong. The panel
# is simply on a display nobody is looking at.
#
# The origin sits on the primary monitor by definition, which is the one screen
# somebody who just started the panel is certain to be watching.
#
# Positions here are GTK's, and on a scaled display they are not Win32's: the
# two differ by the scale factor, so GTK's (4265, 659) was Win32's (3412, 527)
# on a 125% screen. Comparing a figure out of config.json against a monitor
# bound read from Windows therefore proves nothing, and reading one as the other
# is what made a panel on the second monitor look like a panel off the desktop.
# See SpawnPositionTest in tests/test_widget.py.
SPAWN_POSITION = (0, 0)
WIDTH = 240
# Drag-to-resize. The panel scales as one piece — font, padding and width
# together — rather than the frame alone: the content is a fixed set of rows, so
# a wider window on its own would buy nothing but blank space around numbers that
# stayed exactly as small. One number therefore drives everything, and either
# component of the drag can move it: pulling any edge outwards grows the panel.
# Height is never *set* — the rows are as tall as the font makes them — but a
# vertical pull is still a perfectly good way to say "bigger".
MIN_SCALE = 0.7
MAX_SCALE = 3.0
# The grab band around the window's perimeter, and how far along a side still
# counts as that side's corner. Undecorated windows get no frame from anybody,
# so this band is the whole handle. 16 px is comfortable to find without
# looking; it costs the band's width off each side of the area that still moves
# the window, which on a default-size panel leaves 208×107 of the 240×139 to
# drag by. The corner reaches about twice as far along each side, so a pure-side
# grab does not start immediately beside a corner — as on any real window frame.
EDGE = 16
CORNER = 32
AMBER_AT = 60
RED_AT = 85
# Colour classes for the three limit rows. Declared after `muted` in the CSS,
# so
# they win the cascade wherever both apply.
LIMIT_CLASSES = ("green", "amber", "red")
# The file monitor only fires when the hook writes, so a hook that has stopped
# writing would never trigger a redraw — which is exactly the case the staleness
# row exists to report. This timer is the only thing that notices.
STALE_POLL_SECONDS = 60
# How often the panel asks the server for the account's limits itself, through
# cost_meter/usage_api.py. Claude Code asks on a session start and on a `/usage`
# and at no other time, which left the two percentages standing still for hours;
# this is what makes them move on their own.
#
# A minute is the endpoint's pace rather than a preference. Measured on
# 2026-08-14: polling every five seconds was refused with `429 Retry-After: 196`
# after the first answer, while a minute was answered `200` every time it was
# tried — and Claude Code throttles its own cache writes to five minutes, which
# reads like the same server-side rule seen from the other side. A whole
# percentage point of a five-hour window is minutes of heavy work anyway, so this
# is not the limit on how fast the rows can move.
#
# Overridable per machine with `usage_poll_seconds` in config.json, where 0 turns
# the fetch off and leaves the rows reading Claude Code's cache, as they did
# before. Going lower is allowed and will meet the 429; the backoff obeys the
# Retry-After it comes with, so the cost is stale rows, not a hammered endpoint.
USAGE_POLL_SECONDS = 60

# The rolling figures. Every row tweens to its new value instead of snapping to
# it, so a turn that cost $4 and one that cost $0.04 stop looking identical.
#
# The cumulative rows roll from their previous total. `last_turn` is a delta and
# rolls from zero instead, through `Roll.replay`: the distance between one
# turn's cost and the next one's is not a quantity worth animating, and it would
# run the row downwards whenever a cheap turn followed an expensive one.
#
# The three limit rows are not among them. They carry an integer percentage that
# moves once every few hours, which has nothing to tween, and the dollars that
# used to animate there have moved into the tooltip.
ROLL_KEYS = ("last_turn", "session", "today", "week_local")
TURN_KEY = "last_turn"
WINDOW_KEYS = ("window_5h", "window_scoped", "window_7d")
# Which account limit each row draws, named as the server names them.
WINDOW_KINDS = {"window_5h": utilization.SESSION,
                "window_scoped": utilization.SCOPED,
                "window_7d": utilization.WEEKLY}
# The one row with no dollars behind it and no fixed caption: state.json has no
# per-model figure to fall back on, and which model the cap is on comes with the
# figure. Both differences are read off this key rather than off a kind, so the
# other two rows stay one code path.
SCOPED_KEY = "window_scoped"
# The server's severity, mapped onto the panel's colour classes.
SEVERITY_CLASSES = {"normal": "green", "warning": "amber", "critical": "red"}
ROLL_FRAME_MS = 16
ROLL_MIN_DELTA = 0.01

# Patrik mode: the panel throwing money glyphs when a turn lands. Off unless the
# menu says otherwise, and the key lives in the same config.json as the position
# and the scale, so it outlives the panel that was asked for it.
PATRIK_KEY = "patrik_mode"
# The audible half, on its own key rather than inside Patrik mode's. Two
# switches because they are two decisions: an animation plays on your own screen,
# a sound plays in whatever room you are sitting in, and somebody who wants the
# glyphs in an open-plan office wants exactly one of them.
SOUND_KEY = "sound"
# The glyphs are drawn in a second, transparent, always-on-top window rather than
# inside the panel, because the whole point is that they leave it. This is how far
# that window reaches past the panel on every side: enough for a glyph thrown at
# 420 px/s to be watched on its way out and fade before the edge clips it.
PATRIK_MARGIN = 130
PATRIK_FRAME_MS = 16
# Titled, because that is how a test finds the window to measure its click-through
# — see ClickThroughTest in tests/test_patrik.py. Distinct from the panel's own
# title so the two are never confused for one another.
OVERLAY_TITLE = "Claude cost meter glyphs"
# Named per platform rather than left to the panel's own font, which is a text
# family and would hand the glyphs to Pango's fallback chain a character at a
# time. Where the named family is missing Pango falls back anyway, so a Linux box
# without Noto Color Emoji gets monochrome glyphs rather than empty boxes.
EMOJI_FONT = "Segoe UI Emoji" if os.name == "nt" else "Noto Color Emoji"

GWL_EXSTYLE = -20
# The documented condition for a window that mouse clicks fall through. GDK's
# win32 backend does not set it for `set_pass_through` or for an empty input
# shape — measured, both are accepted and dropped — so on Windows the panel sets
# it itself. See `PatrikOverlay.let_clicks_through`.
WS_EX_TRANSPARENT = 0x00000020

# The sizes at scale 1.0, which is the size the panel shipped with and the one
# an unresized panel has to keep.
FONT_PX = 11
WARN_FONT_PX = 10
BORDER = 10
ROW_SPACING = 3
COLUMN_SPACING = 12

# Colours and the font family live here; the font *size* deliberately does not.
# Reloading a CssProvider that is already on the screen updates what the style
# context reports and re-lays out nothing: the label keeps the layout it built
# at the old size, and `style-updated` never fires. A font-size rule here would
# therefore set the startup size, silently ignore every resize after it, and
# look for all the world like it was working. Pango attributes, applied per
# label in `apply_scale`, do rebuild the layout.
#
# The right-click menu is named here too, and it has to be. This provider is
# added for the whole screen, not for the panel, and the menu is a toplevel of
# its own: `label { color: #d8d8dc }` -- a pale grey picked to sit on the
# panel's near-black -- was landing on the menu's light background, where grey
# text reads as an item greyed out to say "unavailable". Every caption in it is
# live, so every caption is black, in every state; the hover shade is set with
# it because a theme is otherwise free to paint the highlight dark and put
# black text on top of it.
CSS = b"""
window { background-color: #1e1e22; }
label { color: #d8d8dc; font-family: monospace; }
label.value { font-weight: bold; }
label.muted { color: #8a8a92; }
label.green { color: #78d178; }
label.amber { color: #e3b341; }
label.red { color: #f06a5a; }
label.warn { color: #f06a5a; }
menu { background-color: #ffffff; }
menu label { color: #000000; }
menu menuitem:hover { background-color: #d5d5da; }
"""


def font_px(scale):
    return round(FONT_PX * scale)


def warn_px(scale):
    """The warning row's size, a footnote below the value rows at every scale.

    Rounded from a smaller base rather than derived from `font_px`, so the gap
    survives the smallest scale instead of rounding shut.
    """
    return round(WARN_FONT_PX * scale)


def font_attrs(px):
    """A Pango attribute list pinning text to `px` device pixels."""
    attrs = Pango.AttrList()
    attrs.insert(Pango.attr_size_new_absolute(px * Pango.SCALE))
    return attrs


def clamp_scale(scale):
    return min(MAX_SCALE, max(MIN_SCALE, scale))


def usage_interval(config):
    """Seconds between our own reads of the account's limits. 0 turns them off.

    Validated for the reason saved_scale() is: config.json is a file a user can
    edit by hand, and this value decides how often something leaves the machine.
    A nonsense one must not become either a tight loop against the endpoint or a
    silent end to the polling the rows' freshness now depends on, so anything that
    is not a number at or above zero falls back to the default.

    Zero survives untouched because it is the off switch. Anything else is at
    least a second, since GLib counts this timer in whole seconds and rounding a
    fraction down would land on the off switch by accident.
    """
    value = (config or {}).get("usage_poll_seconds", USAGE_POLL_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return USAGE_POLL_SECONDS
    if value == 0:
        return 0
    return max(1, round(value))


def width_for_scale(scale):
    return round(WIDTH * scale)


def resize_zone(x, y, width, height):
    """Which resize handle the pointer is over, or None for the panel's body.

    None means the same drag moves the window instead, so the band has to stay
    off the middle: a grab zone that swallowed an intended move would make the
    panel feel stuck.

    All four edges are handles. A vertical pull cannot set the height — the rows
    are as tall as the font makes them — but it can say "bigger", which is the
    one number a resize here has to produce anyway.

    The side bands are tested first, so the corner squares are decided by how far
    down the side the pointer is. Which of the two bands claims a corner makes no
    difference to the result: inside a corner both are true and the name comes out
    the same either way.
    """
    west, east = x < EDGE, x >= width - EDGE
    north, south = y < EDGE, y >= height - EDGE
    if west or east:
        side = "west" if west else "east"
        if y < CORNER:
            return f"north_{side}"
        if y >= height - CORNER:
            return f"south_{side}"
        return side
    if north or south:
        side = "north" if north else "south"
        if x < CORNER:
            return f"{side}_west"
        if x >= width - CORNER:
            return f"{side}_east"
        return side
    return None


def drag_scale(zone, start_scale, dx, dy):
    """The scale a drag of `dx`, `dy` pixels arrives at.

    Outwards grows the panel on every side, so west counts leftwards and north
    counts upwards. A corner carries both components and adds them, which is why
    a diagonal pull grows about twice as fast as a straight one — and why a corner
    dragged straight down does something rather than nothing.

    `WIDTH` is the divisor on both axes: dragging a full panel width doubles the
    panel, and a pixel means the same thing whichever way it is pulled. Scaling
    the vertical axis by the panel's own height instead would make the identical
    gesture mean two different amounts depending on direction.
    """
    outwards = 0.0
    if "west" in zone:
        outwards -= dx
    elif "east" in zone:
        outwards += dx
    if "north" in zone:
        outwards -= dy
    elif "south" in zone:
        outwards += dy
    return clamp_scale(start_scale + outwards / WIDTH)


def drag_origin(zone, start, width, height):
    """Where the window has to start so the un-grabbed edges stay put, as (x, y).

    Grab the left edge and the right one should not move; that means shifting the
    window by whatever the width gained. Same on the other axis: grab the top and
    the bottom edge stays where it was. East and south handles keep the origin,
    which is where the window already grows from.

    `start` is the sample taken when the drag began — `x_window`, `y_window`,
    `width`, `height` — rather than four more parameters, because every one of
    them has to come from that same instant to be worth anything.
    """
    x = (start["x_window"] + start["width"] - width if "west" in zone
         else start["x_window"])
    y = (start["y_window"] + start["height"] - height if "north" in zone
         else start["y_window"])
    return x, y


def _fmt_usd(value):
    return "—" if value is None else f"${value:,.2f}"


def _fmt_reset(value, now=None):
    """Local `HH:MM` for a window's reset time, or `Sat 02:59` when it is not today.

    Local time because that is the clock /usage prints, and the row is only
    worth having if the two can be read against each other. An unparseable
    value is dropped rather than shown raw: a broken timestamp on the row would
    look like a limit resetting at a nonsense time.

    The weekday appears only when the reset falls on a different date. The
    5-hour block is at most five hours out, so `18:30` can only mean today and
    the short form is right; the weekly window resets days away, where a bare
    `02:59` reads as tonight. Same rule for both rows rather than one per row --
    a 5-hour block opened late in the evening resets tomorrow, and the weekday is
    just as wanted there.
    """
    epoch = summary.parse_updated_at(value)
    if epoch is None:
        return None
    when = datetime.fromtimestamp(epoch)
    now = time.time() if now is None else now
    if when.date() == datetime.fromtimestamp(now).date():
        return when.strftime("%H:%M")
    return when.strftime("%a %H:%M")


def turn_text(value, moving=False):
    """The `last turn` row: what the turn cost, or a dash if none is recorded.

    A dash and $0.00 are different claims — nothing recorded against a turn that
    cost nothing — and while the row counts up they come apart: the first frame
    of a roll draws exactly zero, and falling back to the dash there would blank
    the row for a frame before the digits started moving.
    """
    if value is None or (not value and not moving):
        return "—"
    return f"+{_fmt_usd(value)}"


def severity_class(severity, pct):
    """The colour class for a limit row.

    The server's own severity is preferred over a threshold of ours: it knows
    where the thresholds are, and they move — this account is currently carrying
    a +50 % weekly promotion, which no number compiled in here would follow.

    An unrecognised value falls back to the percentage rather than to green. A
    word this panel has never seen must not be what paints a row at 95 % as safe.
    """
    return SEVERITY_CLASSES.get(severity) or (
        "red" if pct >= RED_AT else "amber" if pct >= AMBER_AT else "green")


def _pct_of(limit):
    """The row's whole-number percentage, or None if it hasn't got one.

    `bool` is rejected explicitly because it satisfies `isinstance(x, int)`, and
    `True` would otherwise paint a row at 1 %.
    """
    pct = (limit or {}).get("pct")
    if isinstance(pct, bool) or not isinstance(pct, int):
        return None
    return pct


def window_expired(limit, now):
    """Whether this figure describes a window that has already reset.

    Age cannot answer this. A figure fetched four hours ago still bounds a weekly
    window, and one fetched twenty minutes ago bounds nothing if the 5-hour block
    turned over in between — so the reset time the server sent is what decides.

    A row without a reset time never expires: there is nothing to compare, and
    the older cache shape does not always carry one.
    """
    end = summary.parse_updated_at((limit or {}).get("resets_at"))
    return end is not None and now >= end


def scoped_limit(scoped, weekly):
    """The scoped figure with a reset time on it, borrowed from the week if need be.

    The server sends `weekly_scoped` without a `resets_at` when the cap is at
    nought, and with one when it is not; `weekly_all` carries one either way.
    Both describe the same weekly cycle -- in the sample where both were present
    they agreed to the second, differing only in microseconds -- so the week's
    time is the scoped row's time, and a row that said nothing about when it
    turns over would be withholding a fact the panel has.

    Borrowed, not adopted. The copy carries `resets_from_week`, and the tooltip
    words it differently for it: a time the server did not put on this entry is
    an inference, and this panel does not print inferences as figures. It is a
    copy for the same reason -- the rows dict is read again on every repaint,
    and filling it in place would lose the distinction after one pass.

    None in stays None out. A reset beside a dash would claim a window for a
    figure that does not exist.
    """
    if not scoped:
        return None
    filled = dict(scoped)
    borrowed = (filled.get("resets_at") is None
                and (weekly or {}).get("resets_at") is not None)
    if borrowed:
        filled["resets_at"] = weekly["resets_at"]
    filled["resets_from_week"] = borrowed
    return filled


def scoped_caption(limit):
    """The caption for the scoped row: the model the cap is on, or `scoped`.

    Every other caption on the panel is a fixed word, because what the row
    measures cannot change. This one can. `weekly_scoped` is a weekly cap on one
    model, and which model arrives with the figure rather than being known here;
    utilization.py lifts it out of the scope the server sends alongside.

    Lower case because every caption on this panel is, and a capitalised one
    among them reads as a rendering fault rather than as the server's spelling.

    `scoped` when there is no name to use -- an account with no scoped cap, or a
    figure that arrived without a scope. The row is drawn either way, so it
    needs a caption either way, and naming the kind is the honest answer when
    the model is not known.
    """
    name = (limit or {}).get("scope")
    return name.lower() if name else "scoped"


def window_row(window, limit, now=None):
    """The text and style class for one limit row, as (text, class).

    With an account figure the row is that figure and nothing else. The percentage
    describes the whole account and the dollars describe this machine; two scopes
    on one row read as one claim, and the reader divides them — which is exactly
    the arithmetic the old calibrated percentage was doing wrongly. The dollars
    move to the tooltip, which has the room to say which is which.

    The percentage is always marked `≈`, and unconditionally: the figure is
    re-asked of the server when a session starts and when the user runs /usage,
    and at no other time, so even a twelve-second-old one has had twelve seconds
    to rise. A marker that came and went would imply the unmarked form is exact,
    and it never is.

    `≈` replaced `≥` on request. `≥` was the stronger claim — usage within a
    window only grows, so the figure really is a floor — but it reads as a
    comparison rather than as a hedge, and this row is a hedge. The tooltip is
    where the floor is still stated exactly ("account at least 17 %").

    The reset time rides with the percentage, as it always has: what it is for is
    saying which window the figure describes. Once that time has passed the row is
    withdrawn, because the window it described is gone.

    Without an account figure the row falls back to the dollars alone, muted,
    exactly as it read before any of this existed. Interpolating a percentage from
    local dollars would be an invented number.
    """
    now = time.time() if now is None else now
    pct = _pct_of(limit)
    if pct is None or window_expired(limit, now):
        return _fmt_usd(window.get("usd")), "muted"
    resets = _fmt_reset(limit.get("resets_at"), now)
    tail = "" if resets is None else f" · {resets}"
    return f"≈{pct} %{tail}", severity_class(limit.get("severity"), pct)


def window_tooltip(window, limit, age_s, now=None):
    """The tooltip for one limit row: which figure belongs to which scope.

    This is where the dollar figure went when it left the row, and the only place
    the two scopes are stated rather than implied.

    The age is here rather than on the row because hours-old figures are the
    normal case, so an age beside every percentage would be permanent noise —
    while somebody wondering why a percentage has not moved all afternoon wants
    exactly this, together with the one thing that would move it.
    """
    now = time.time() if now is None else now
    machine = f"{_fmt_usd(window.get('usd'))} on this machine"
    return "\n".join([machine] + _limit_lines(limit, age_s, now))


def scoped_tooltip(limit, age_s, now=None):
    """The scoped row's tooltip: the account lines, and no machine line.

    Every other limit tooltip opens with this machine's dollars, because that is
    where the figure went when it left the row. This row has no such figure to
    open with: the dollars this project records are every model's together, and
    putting that total under a caption naming one model would be the very
    two-scopes-on-one-claim mistake window_row exists to avoid. So the line is
    absent rather than approximated, and what is left is the account's own
    figure and how old it is.

    A reset time borrowed from the week gets a line of its own rather than
    riding on the figure. On every other row `, resets 18:30` is part of the
    claim the percentage makes, and this one did not come with the percentage --
    scoped_limit inferred it. Saying which is which is the whole reason the row
    is allowed to show it at all.
    """
    now = time.time() if now is None else now
    borrowed = (limit or {}).get("resets_from_week")
    if not borrowed or _pct_of(limit) is None or window_expired(limit, now):
        return "\n".join(_limit_lines(limit, age_s, now))
    resets = _fmt_reset(limit.get("resets_at"), now)
    # Stripped before the shared lines run, so the account line states the
    # figure alone and the borrowed time speaks for itself below it.
    lines = _limit_lines(dict(limit, resets_at=None), age_s, now)
    if resets is not None:
        lines.insert(1, f"resets with the week, {resets}")
    return "\n".join(lines)


def _limit_lines(limit, age_s, now):
    """The account's own lines of a limit tooltip, machine dollars aside.

    Shared so the scoped row cannot drift from the two rows above it: how the
    floor is worded, when a reset is named, and what a reader can do about a
    figure that has not moved are one question, not three.
    """
    pct = _pct_of(limit)
    if pct is None:
        return ["no account figure available"]
    if window_expired(limit, now):
        return ["the account figure describes a window that has reset"]
    resets = _fmt_reset(limit.get("resets_at"), now)
    account = f"account at least {pct} %"
    if resets is not None:
        account += f", resets {resets}"
    if age_s is None:
        return [account, "/usage refreshes it"]
    return [account,
            f"figure {summary.format_age(age_s)} old; /usage refreshes it"]


def billing_text(billing):
    """The `billing` row: what this session is paying with, or a dash.

    A dash covers both the case where nothing could be established and a
    state.json written before this row existed — and it stays a dash, because
    the mode is not in the transcripts and so cannot be filled in afterwards.
    Guessing either way would misrepresent every row above: the same dollar
    figure is a notional number on a seat and a bill on API billing.

    With a mode but no label, the mode alone goes on the row. A login that names
    neither a subscription nor a tier still tells you the useful half.
    """
    billing = billing or {}
    mode = billing.get("mode")
    if not mode or mode == "unknown":
        return "—"
    return billing.get("label") or mode


def _row(grid, index, caption, labels):
    """Build one caption/value pair, and register both for scaling.

    Both halves go into `labels` because the whole row has to grow together;
    the caller only ever needs the value label back, which is the one that gets
    rewritten.
    """
    return _captioned_row(grid, index, caption, labels)[1]


def _captioned_row(grid, index, caption, labels):
    """The same row, with the caption label handed back as well.

    Only the scoped row needs it: every other caption is a fixed word set once
    here, and that one is rewritten as the figure names its model. Kept beside
    `_row` rather than folded into it so the eight callers that want only the
    value keep saying so.
    """
    left = Gtk.Label(label=caption, xalign=0.0)
    right = Gtk.Label(label="—", xalign=1.0)
    right.get_style_context().add_class("value")
    right.set_hexpand(True)
    grid.attach(left, 0, index, 1, 1)
    grid.attach(right, 1, index, 1, 1)
    labels.extend((left, right))
    return left, right


class PatrikOverlay(Gtk.Window):
    """The transparent window the money glyphs are drawn in.

    A window of its own rather than a Gtk.Overlay inside the panel, because the
    effect is glyphs *leaving* the table: anything drawn inside the panel is
    clipped at its border, and a burst that faded out before the edge reads as
    dirt on the window rather than as money flying off.

    It carries no input of its own and must carry none: it covers the panel and a
    wide margin round it, so every click landing there has to reach whatever is
    underneath — the panel itself, or the desktop. `let_clicks_through` is the
    only part of that which is not one GTK call, and the reason is in its
    docstring.

    Built on demand and destroyed when the mode goes off, rather than kept hidden
    for the life of the panel: an always-on-top window nobody can see is worth
    having only while something is being drawn in it.
    """

    def __init__(self, visual):
        super().__init__(title=OVERLAY_TITLE)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        # UTILITY for the reason the panel uses it: win32 turns it into
        # WS_EX_TOOLWINDOW, which is what keeps a window out of the taskbar and
        # out of Alt-Tab. A decoration with its own Alt-Tab entry would be worse
        # than no decoration at all.
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_resizable(False)
        # app_paintable and an RGBA visual together are what make the window
        # transparent; the draw handler then paints nothing but the glyphs.
        self.set_app_paintable(True)
        self.set_visual(visual)
        self.particles = []
        self.connect("draw", self.on_draw)

    def let_clicks_through(self):
        """Make the window ignore the mouse. Called once, after realize.

        `set_pass_through` is the GTK way to say this and it is what X11 acts on.
        On Windows it is a silent no-op: measured, neither it nor an empty input
        shape sets WS_EX_TRANSPARENT, which is the documented condition for
        clicks falling through a window. Left at the GTK call alone, this overlay
        would swallow every click across the panel and its whole margin for as
        long as Patrik mode was on, and nothing on screen would explain why the
        desktop had stopped answering.

        That is the same shape of bug as the taskbar one this panel already
        carries a workaround for — a hint GDK's win32 backend accepts and drops —
        so it gets the same treatment: ask GTK, then check the result ourselves on
        the platform where asking is not enough.
        """
        window = self.get_window()
        window.set_pass_through(True)
        if os.name != "nt":
            return
        handle = self.win32_handle()
        if handle is None:
            return
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(ctypes.c_void_p(handle), GWL_EXSTYLE)
        user32.SetWindowLongW(ctypes.c_void_p(handle), GWL_EXSTYLE,
                              style | WS_EX_TRANSPARENT)

    def win32_handle(self):
        """This overlay's HWND, or None if it cannot be identified.

        By title, among the top-level windows this process owns. The direct route
        would be `gdk_win32_window_get_handle`, and it is not available: PyGObject
        exposes no HWND accessor on GdkWin32Window at all — checked, there is no
        `get_handle` and no `get_hwnd` — because the function is not annotated for
        introspection.

        Both halves of the filter are load-bearing, for the reason
        tests/test_widget.py gives for the same pattern: by title alone this would
        find the overlay of a panel running in another process and make that one
        click-through instead of this one, and by process alone it would find
        GTK's own hidden helper top-levels.
        """
        user32 = ctypes.windll.user32
        ours = os.getpid()
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def visit(hwnd, _param):
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd),
                                            ctypes.byref(pid))
            if pid.value == ours:
                buffer = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(ctypes.c_void_p(hwnd), buffer, 256)
                if buffer.value == OVERLAY_TITLE:
                    found.append(hwnd)
            return True

        user32.EnumWindows(visit, None)
        # Exactly one, or none: a second match means the title no longer
        # identifies this window, and making the wrong one click-through is worse
        # than leaving this one opaque to the mouse.
        return found[0] if len(found) == 1 else None

    def on_draw(self, _widget, context):
        """Paint the glyphs, and nothing else at all.

        SOURCE rather than the default OVER for the clear: OVER onto an
        already-transparent surface leaves whatever the last frame drew, so the
        glyphs would smear into a trail instead of moving.
        """
        context.set_operator(cairo.OPERATOR_SOURCE)
        context.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        context.paint()
        context.set_operator(cairo.OPERATOR_OVER)
        layout = PangoCairo.create_layout(context)
        for particle in self.particles:
            layout.set_font_description(
                Pango.FontDescription(f"{EMOJI_FONT} {particle.size:.0f}px"))
            layout.set_text(particle.glyph, -1)
            width, height = layout.get_pixel_size()
            # Centred on the particle's own point, so `size` grows the glyph
            # about where it is rather than dragging it down and to the right.
            context.move_to(particle.x - width / 2.0,
                            particle.y - height / 2.0)
            context.push_group()
            PangoCairo.show_layout(context, layout)
            context.pop_group_to_source()
            # Through a group rather than set_source_rgba before show_layout: a
            # colour emoji is drawn from its own bitmaps and ignores the source
            # colour entirely, so this is the only way the fade reaches it.
            context.paint_with_alpha(particle.alpha)
        return True


class CostMeter(Gtk.Window):
    def __init__(self):
        super().__init__(title="Claude cost meter")
        self.set_decorated(False)
        self.set_keep_above(True)
        # UTILITY is what keeps the panel out of the Windows taskbar, and the
        # skip hints below are not: GDK's win32 backend accepts
        # set_skip_taskbar_hint() and does nothing with it, so the panel sat in
        # the taskbar for its whole run while this code read as though it had
        # opted out. UTILITY is the hint win32 turns into WS_EX_TOOLWINDOW,
        # which is the documented condition for no taskbar button (and drops it
        # from Alt-Tab too, which a panel wants anyway). DOCK would describe
        # this window better but earns neither on that backend.
        #
        # Set on both platforms rather than behind an os.name test: on X11 it is
        # the accurate type for a floating panel and the skip hints there
        # already did this job, so nothing about Linux changes.
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        # Still asked for, because they are what X11 acts on.
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        # Resizable because `resize()` is how a scale change is applied, and
        # set_resizable(False) pins the geometry hints to the natural size and
        # makes that call a no-op. Nothing else about the window changes: the
        # size still comes from the scale, never from the window manager.
        self.set_resizable(True)
        self.scale = self.saved_scale()
        self.set_default_size(width_for_scale(self.scale), -1)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK
                        | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("button-press-event", self.on_click)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("destroy", Gtk.main_quit)

        # The resize drag in progress, or None. Held ourselves rather than
        # handed to begin_resize_drag: the window manager would stretch the
        # height for the length of the drag and we would take it back on every
        # frame, which reads as the panel shuddering. `_cursor_name` is the
        # shape currently set, so a pointer crossing the band sets it once
        # instead of on every motion event.
        self._resize = None
        self._cursor_name = None

        # Position bookkeeping. user_positioned means the user chose a spot, so
        # automatic re-anchoring must stop; _anchor is the last position we set
        # ourselves, which is how a self-inflicted move is told apart from a
        # user drag; _position_timer is the single debounce source.
        self.user_positioned = False
        self._anchor = None
        self._position_timer = None
        self.connect("size-allocate", self.on_size_allocate)
        self.connect("configure-event", self.on_configure)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        grid = Gtk.Grid()
        self.grid = grid
        self.add(grid)

        # Every label the scale has to reach. Collected as the rows are built,
        # because a caption that stayed 11 px while its value grew would look
        # like a rendering fault rather than a missing line of code.
        self.labels = []
        self.last_turn = _row(grid, 0, "last turn", self.labels)
        self.session = _row(grid, 1, "session", self.labels)
        self.today = _row(grid, 2, "today", self.labels)
        grid.attach(Gtk.Separator(), 0, 3, 2, 1)
        self.window_5h = _row(grid, 4, "5h window", self.labels)
        # Directly under the 5h window, because it is the same kind of claim --
        # the account's share of a limit -- and because the week and the machine
        # dollars below it are a pair that must not be split. Its caption is
        # written here only as a placeholder: draw_limits replaces it with the
        # model the figure names, and `scoped` is what an account with no scoped
        # cap is left reading.
        self.scoped_name, self.scoped_value = _captioned_row(
            grid, 5, "scoped", self.labels)
        self.window_7d = _row(grid, 6, "week", self.labels)
        # Directly under the percentage it belongs to, because the two describe
        # the same seven days by different measures: the account's share of its
        # limit, and what this installation put into it. Adjacency is what says
        # they are one window; a caption naming the machine is what stops the
        # dollars being read as the account's.
        self.week_local = _row(grid, 7, "this machine", self.labels)
        # Below a separator of its own: everything above is a measured figure,
        # and this is the fact that says what those figures mean — money owed on
        # API billing, notional against a seat.
        grid.attach(Gtk.Separator(), 0, 8, 2, 1)
        self.billing = _row(grid, 9, "billing", self.labels)
        # The rows made of state.json's dollars, and so the rows staleness mutes.
        # The three limit rows are deliberately absent — see set_stale: they come
        # from Claude Code's cache instead, and `muted` reaches them only through
        # draw_limits, when there is no account figure and they are showing
        # dollars after all. The billing row rides with the plain ones: it is
        # drawn once from state.json and only staleness ever mutes it.
        self.usd_values = (self.last_turn, self.session, self.today,
                           self.week_local, self.billing)
        self.rows = {"last_turn": self.last_turn,
                     "session": self.session, "today": self.today,
                     "week_local": self.week_local,
                     "window_5h": self.window_5h, "window_7d": self.window_7d,
                     SCOPED_KEY: self.scoped_value}

        # Animation state. `_roll_source` is a single timer for the whole panel,
        # retargeted in place rather than stacked; `_roll_began` is what progress
        # is measured from, so a slow frame shortens the roll instead of
        # stretching it past its duration, and `_roll_ms` is how long this
        # particular roll was given — it depends on the distance, so it is
        # decided per roll.
        self.roll = roll.Roll(min_delta=ROLL_MIN_DELTA)
        # The last two window dicts and the account's limit figures. Held rather
        # than read where they are needed because the limit rows are repainted
        # outside refresh() — a five-hour block can reset with no turn happening,
        # and the staleness poll is what notices.
        self.windows = {key: {} for key in WINDOW_KEYS}
        self.limits = {}
        # The `updated_at` the turn row was last counted up for. It is what says
        # a turn is new; the figure cannot, because two turns costing the same
        # cent are ordinary.
        self._turn_stamp = None
        self._roll_source = None
        self._roll_began = 0.0
        self._roll_ms = roll.BASE_MS

        # Patrik mode. The swarm exists whether or not the mode is on, because an
        # empty swarm costs nothing and code that has to ask whether it has one
        # before every check reads far worse than code that asks whether it is
        # running. `overlay` is built on the first burst and destroyed when the
        # mode goes off; `_patrik_began` is what the shake's progress is measured
        # from, and `_patrik_frame` the wall-clock of the last frame, so the
        # glyphs advance by real elapsed time rather than by an assumed 16 ms.
        self.swarm = patrik.Swarm()
        self.shake = patrik.Shake()
        self.overlay = None
        self._patrik_source = None
        self._patrik_began = 0.0
        self._patrik_frame = 0.0
        # How long this celebration was given, and how much of that its flinch
        # takes. Decided per turn, because both depend on what the turn cost --
        # see `patrik.duration_ms`.
        self._patrik_ms = patrik.BASE_MS
        # Glyphs per second, taken from the session's total once per celebration
        # for the reason the length is: see `celebrate`.
        self._patrik_rate = patrik.RATE
        self._shake_ms = patrik.SHAKE_MS
        # Where the window sits for the duration of a shake, and what it is handed
        # back to. Held rather than read per frame: read live it would drift by
        # whatever the last frame's offset was, and the panel would walk.
        self._patrik_base = None

        self.warning = Gtk.Label(label="", xalign=0.0)
        self.warning.get_style_context().add_class("warn")
        self.warning.set_no_show_all(True)
        # Row 10, below `billing` on row 9: the two once shared a row and GTK drew
        # them on top of each other, so the red staleness note sat over the
        # billing text. Every row index here is literal, so a row added above has
        # to push this one down with it -- the scoped row did exactly that.
        grid.attach(self.warning, 0, 10, 2, 1)

        # After every label exists, since this is what sizes them.
        self.apply_scale(self.scale)

        # Our own read of the account's limits. `usage_wait` is how long the
        # backoff is currently holding off after a failure and `usage_next` the
        # monotonic time it may resume at; `usage_busy` keeps one request in
        # flight, because a timer that fired while the last request was still
        # waiting on a timeout would stack threads.
        self.usage_seconds = self.saved_usage_interval()
        self.usage_busy = False
        self.usage_wait = 0.0
        self.usage_next = 0.0

        # Both timers, so destroying the panel can take them with it. A GLib
        # timeout belongs to the main context rather than to the widget that
        # registered it, so without this a destroyed panel keeps waking up and
        # keeps polling the endpoint — invisible where a panel outlives the
        # process, and not invisible in a process that builds a second one.
        self.alive = True
        self.sources = []

        # False across the opening refresh below, so Patrik mode does not
        # celebrate the figure that was already on disk when the panel opened.
        self._opened = False
        self.watch()
        self.refresh()  # before place(), so the first anchor sees real content
        self._opened = True
        self.place()
        self.connect("destroy", self.stop_timers)
        self.sources.append(
            GLib.timeout_add_seconds(STALE_POLL_SECONDS, self.refresh))
        if self.usage_seconds:
            # One interval after startup rather than at it, deliberately:
            # `--selftest` builds a real panel, and a smoke test must not reach
            # the network. The rows have Claude Code's cache to show meanwhile.
            self.sources.append(
                GLib.timeout_add_seconds(self.usage_seconds, self.poll_usage))

    @staticmethod
    def saved_scale():
        """The scale the panel was left at, or 1.0.

        Clamped and type-checked on the way in: config.json is a file a user can
        edit, and a hand-typed `"widget_scale": 40` would open a panel larger
        than the screen with its own resize handles off the edge — unreachable,
        and only fixable by editing the file back.
        """
        config = store.read_json(paths.config_path(), default={}) or {}
        try:
            return clamp_scale(float(config.get("widget_scale") or 1.0))
        except (TypeError, ValueError):
            return 1.0

    @staticmethod
    def saved_usage_interval():
        """How often to ask the server ourselves, from config.json.

        Read once at construction, like the scale: changing how often a panel
        polls is a restart, not something to re-read on every tick.
        """
        return usage_interval(store.read_json(paths.config_path(), default={}) or {})

    def apply_scale(self, scale):
        """Resize the whole panel to `scale`: text, spacings and window.

        Text goes through Pango attributes rather than the stylesheet, for the
        reason recorded above `CSS` — a reloaded provider restyles nothing that
        already exists.

        The height is asked for as 1 rather than computed: GTK clamps a request
        up to the natural size, so the rows decide the height and this only ever
        sets the width. Computing it here would mean knowing the new font
        metrics before the attributes that produce them have been applied.
        """
        self.scale = clamp_scale(scale)
        attrs = font_attrs(font_px(self.scale))
        for label in self.labels:
            label.set_attributes(attrs)
        self.warning.set_attributes(font_attrs(warn_px(self.scale)))
        self.grid.set_border_width(round(BORDER * self.scale))
        self.grid.set_row_spacing(round(ROW_SPACING * self.scale))
        self.grid.set_column_spacing(round(COLUMN_SPACING * self.scale))
        self.resize(width_for_scale(self.scale), 1)

    def remember_scale(self):
        self.update_config(lambda c: c.__setitem__("widget_scale", self.scale))

    def reset_scale(self):
        self.update_config(lambda c: c.pop("widget_scale", None))
        self.apply_scale(1.0)

    def set_resize_cursor(self, zone):
        """Show the handle under the pointer, or hand the cursor back.

        The window is undecorated, so this shape is the only thing that says a
        resize handle is there at all. Set only on a change: GDK takes a cursor
        per call and motion events arrive by the dozen.
        """
        name = {"west": "ew-resize", "east": "ew-resize",
                "north": "ns-resize", "south": "ns-resize",
                "north_west": "nwse-resize", "south_east": "nwse-resize",
                "north_east": "nesw-resize", "south_west": "nesw-resize",
                }.get(zone)
        if name == self._cursor_name:
            return
        self._cursor_name = name
        window = self.get_window()
        if window is None:
            return  # not realized yet; the next motion event sets it
        window.set_cursor(
            None if name is None
            else Gdk.Cursor.new_from_name(self.get_display(), name))

    def on_motion(self, _widget, event):
        if self._resize is None:
            self.set_resize_cursor(
                resize_zone(event.x, event.y, *self.get_size()))
            return False
        start = self._resize
        scale = drag_scale(start["zone"], start["scale"],
                           event.x_root - start["x"],
                           event.y_root - start["y"])
        if scale != self.scale:
            self.apply_scale(scale)
            self.move_for_drag(start)
        return True

    def move_for_drag(self, start):
        """Keep the edges the user is not holding where they left them.

        Only when the user has placed the panel themselves. While it is anchored
        the corner owns the position: on_size_allocate re-anchors on every scale
        change, so moving here would be overruled a moment later anyway, and the
        right and bottom edges already stay put because those are the edges in
        the corner.

        The width is taken from the scale rather than from the window, because it
        is exact the instant the scale changes. The height cannot be: apply_scale
        asks GTK for 1 and lets the rows clamp it up, so the real figure only
        exists once the allocation has happened — which is why on_size_allocate
        calls this too. From here a north drag can be one frame behind; from
        there it is right, and a resize always allocates.
        """
        if not self.user_positioned:
            return
        self.move(*drag_origin(start["zone"], start,
                               width_for_scale(self.scale),
                               self.get_size().height))

    def on_release(self, _widget, _event):
        if self._resize is None:
            return False
        self._resize = None
        self.remember_scale()
        return True

    def place(self):
        """Open at SPAWN_POSITION, whatever config.json remembers.

        The saved position is deliberately not read back. What it carries is the
        monitor the panel was last dragged to, and restoring that is how a panel
        goes missing: it opens on the second screen, correctly and every time,
        while whoever started it watches the first one and sees nothing.

        `user_positioned` is set here rather than left as it was, and that is
        what makes the origin stick. on_size_allocate re-anchors to the
        bottom-right corner on every size change while the flag is False, so
        without this the window would be put at the origin and slide off it a
        frame later, as soon as the first row was measured.

        Dragging the panel still moves it and is still persisted -- only the
        restore is gone, so what a drag writes now lasts for the session rather
        than for the install.
        """
        self.user_positioned = True
        self._anchor = SPAWN_POSITION
        self.move(*self._anchor)

    def anchor_bottom_right(self):
        """Seat the window in the bottom-right corner of the work area.

        Anchored on the window's actual size, not on a pre-realize guess: the
        warning row is unwrapped, so two long unknown model ids can double the
        width, and anchoring on a lower bound hangs the panel off the screen
        exactly when it has something urgent to report. on_size_allocate calls
        this again whenever that size changes.
        """
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()
        size = self.get_size()
        target = (area.x + area.width - size.width - MARGIN,
                  area.y + area.height - size.height - MARGIN)
        if target == self._anchor:
            return  # already seated here; moving again would loop
        self._anchor = target
        self.move(*target)

    def on_size_allocate(self, _widget, _allocation):
        if not self.user_positioned:
            self.anchor_bottom_right()
        elif self._resize is not None:
            # The height is only knowable here — see move_for_drag. Without this
            # a drag on the top edge would leave the bottom one creeping.
            self.move_for_drag(self._resize)

    def at_anchor(self):
        """True when the window sits exactly where we last put it ourselves.

        This is what separates our own re-anchor from a user drag, and it is
        deliberately not a timer: a resize emits its own configure-event, so a
        short-lived "I am anchoring now" flag misses it and the automatic move
        gets persisted as if the user had chosen it.
        """
        return self._anchor is not None and tuple(self.get_position()) == self._anchor

    def update_config(self, mutate):
        """Read-modify-write config under the lock.

        cost_meter/autolaunch.py writes the paused flag into the same file, and
        the panel itself writes a position from one process and a scale from
        another; without the lock a drag could clobber whichever value was written
        moments earlier. Every writer uses this same helper, so all of them hold
        the lock across the read as well as the write.
        """
        try:
            with store.update_json_locked(paths.config_path(),
                                          paths.lock_path()) as config:
                mutate(config)
        except store.LockTimeout:
            pass  # not worth interrupting the user over a window position

    def remember_position(self):
        position = list(self.get_position())
        self.update_config(lambda c: c.__setitem__("widget_position", position))

    def watch(self):
        """Repaint whenever either source file changes.

        Two files, because the panel has two sources and they move on unrelated
        schedules: state.json when the tally hook finishes a turn, and Claude
        Code's cache when it re-asks the server — session start, or a /usage.

        The cache is watched rather than left to the 60-second poll because that
        minute is exactly when somebody is looking. `/usage` and the panel read
        the same figures, so a percentage that disagrees with the /usage just
        printed above it reads as a broken panel, not as a poll that has not come
        round yet. The poll stays: it is what notices a window reaching its
        `resets_at`, which changes a row with no file changing at all.

        The monitors are held on the instance because a GFileMonitor stops
        watching when it is collected, and a local would be collected the moment
        this returns.
        """
        self.monitors = [self._watch_file(paths.state_path()),
                         self._watch_file(paths.claude_config_path())]

    def _watch_file(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        monitor = Gio.File.new_for_path(str(path)).monitor_file(
            Gio.FileMonitorFlags.NONE, None
        )
        monitor.connect("changed", lambda *_: self.refresh())
        return monitor

    @staticmethod
    def read_limits():
        """The account's limit figures, read live rather than from state.json.

        state.json carries a copy, but the tally hook writes it, so it only moves
        when a turn lands. The cache behind it moves on Claude Code's schedule
        instead — a session starting, a /usage — and a five-hour block resets on
        nobody's schedule at all. Between turns that left the 5h row showing
        dollars for a window that had already turned over, while a live
        percentage for the new block sat on disk unread. This is what closes that
        gap: watch() puts a file monitor on the cache, so a new figure is up as
        soon as it is written, and the 60-second poll re-reads it besides.

        Sole source, with no fallback to state.json's copy: utilization.read()
        returns None exactly when the cache cannot be trusted — missing, another
        account's, past the sanity cap — and state.json's copy came from that
        same file. Falling back to it would put up a figure that was just judged
        untrustworthy. `{}` then means no account figure, which draw_limits and
        window_row already read as "show the dollars, muted".
        """
        return utilization.read() or {}

    def stop_timers(self, *_):
        """Let the panel's timers die with the window.

        `alive` covers what source_remove cannot: a fetch already in flight on a
        worker thread will still come back through idle_add, and it must find a
        window that says it is gone rather than repaint a destroyed one.
        """
        self.alive = False
        for source in self.sources:
            GLib.source_remove(source)
        self.sources = []
        # Cleared rather than left holding a source id that has just been removed:
        # `end_patrik` would otherwise remove it a second time, which GLib treats
        # as a programmer error and warns about.
        self._patrik_source = None
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None

    def poll_usage(self):
        """Ask the server for the account's limits, off the main loop.

        The request goes to a worker thread because it is a network read with a
        ten-second timeout on it, and the main loop is what draws the panel: a
        blocking call here would freeze the window, drag included, for as long as
        the endpoint took to answer.

        Two guards, both for the same failure. `usage_busy` skips a tick whose
        predecessor is still waiting, so a slow endpoint cannot stack a thread
        every interval, and `usage_next` is the failure backoff — set in
        usage_fetched, checked here rather than by rescheduling the timer, so the
        interval itself stays the one thing that decides the poll's rate.
        """
        if not self.alive:
            return False
        if self.usage_busy or time.monotonic() < self.usage_next:
            return True
        self.usage_busy = True
        threading.Thread(target=self._fetch_usage, daemon=True).start()
        return True

    def _fetch_usage(self):
        """The worker thread's whole job: fetch, then hand back to the main loop.

        Nothing here touches a widget. GTK is not thread-safe, so the answer
        crosses back through GLib.idle_add and every repaint happens where every
        other repaint happens.
        """
        ok, retry_after = usage_api.refresh()
        GLib.idle_add(self.usage_fetched, ok, retry_after)

    def usage_fetched(self, ok, retry_after=None):
        """Take the worker's answer on the main loop. Returns False: one shot.

        A fresh figure is repainted through refresh() like any other, since
        read_limits goes to whichever source is newer and neither the rows nor
        their marker care which one that was.

        A failure paints nothing. The previous figure is still the best available
        and is still a floor, so the only thing that changes is when we next ask:
        the backoff doubles, which is what keeps a moved endpoint or a closed
        laptop from being asked every few seconds all day.

        `retry_after` is a wait the server itself asked for, and it wins over the
        backoff whenever it is longer. An early backoff step is a few seconds; a
        rate-limited endpoint means it for minutes, and asking again before then
        would earn nothing but another refusal.
        """
        self.usage_busy = False
        if not self.alive:
            return False
        if ok:
            self.usage_wait = 0.0
            self.usage_next = 0.0
            self.refresh()
        else:
            wait = usage_api.backoff_seconds(self.usage_wait, self.usage_seconds)
            self.usage_wait = max(wait, retry_after or 0.0)
            self.usage_next = time.monotonic() + self.usage_wait
        return False

    def refresh(self):
        state = store.read_json(paths.state_path(), default=None)
        if not state:
            return True

        # The turn row counts up from zero, and only when the turn is new.
        # refresh() also runs from the staleness poll, from `Refresh now` and
        # from __init__, all of which re-read a state.json that has not changed;
        # `updated_at` moving is what separates those from a turn landing.
        #
        # Before retarget, not after: retarget re-bases every leg in flight on
        # what its row is showing, and the count has to have put zero there
        # first.
        stamp = state.get("updated_at")
        turn_rolling = False
        if stamp != self._turn_stamp:
            self._turn_stamp = stamp
            turn_usd = state.get("last_turn_usd") or 0.0
            turn_rolling = self.roll.replay(TURN_KEY, turn_usd)
            # Patrik mode rides on the same test, and skips the panel's own
            # opening refresh for the reason `Roll.replay` sets the row outright
            # there: the figure on disk at startup has not just been charged, and
            # celebrating it would announce a turn that was only read.
            #
            # `_opened` rather than "`_turn_stamp` was None", which is the same
            # thing right up until a fresh install has no state.json at all --
            # then the first turn ever recorded is a real one, and inferring
            # startup from an absent stamp would be the one burst that most
            # deserves to happen.
            session_usd = (state.get("session") or {}).get("usd")
            if self._opened and self.patrik_enabled():
                self.celebrate(turn_usd, session_usd)
            # Independently of the glyphs, and gated on `_opened` for the same
            # reason: auto-launch opens a panel at every session start, and the
            # figure already on disk has not just been charged. A noise on that
            # would be the panel greeting a session it did not witness.
            #
            # The turn's own cost, not the session's: the sound answers "what did
            # that one cost", which is the figure on the top row and the only one
            # the person who just pressed enter is waiting on. Keyed to the
            # session it could only ever climb, so an afternoon that had already
            # run up $500 would play the loudest file for a two-cent turn and go
            # on playing it until midnight -- and the glyph rate, which does read
            # the session, is already the channel that says how deep we are in.
            if self._opened and self.sound_enabled():
                sound.play_for(turn_usd)

        for key in WINDOW_KEYS:
            self.windows[key] = state.get(key) or {}
        # Live from Claude Code's cache, not from `state` — see read_limits.
        self.limits = self.read_limits()
        rolling = self.roll.retarget({
            "session": (state.get("session") or {}).get("usd"),
            "today": state.get("today_usd"),
            "week_local": self.windows["window_7d"].get("usd"),
        }) or turn_rolling

        # A broken tally exits 0 and simply stops rewriting state.json, so
        # without this the panel would keep showing hours-old figures as though
        # they were current — the same invisible-gap failure the `?` row exists
        # to prevent, reached from the other side.
        stale, age = summary.staleness(state, time.time())

        if stale:
            # Stale figures are not being presented as current, and rolling them
            # would say exactly the opposite. They still land on their targets:
            # old numbers are the best available, they just stop moving.
            self.roll.cancel()
            rolling = False
        for key in ROLL_KEYS:
            self.draw_row(key)
        self.draw_limits()
        # Not a rolling row: it is a fact, not a quantity, and there is nothing
        # between `team · max 5x` and `API` to animate through.
        self.billing.set_text(billing_text(state.get("billing")))
        if rolling:
            self.start_roll()

        notes = []
        if stale and age is None:
            notes.append("! stale, age unknown")
        elif stale:
            notes.append(f"! stale {summary.format_age(age)}")
        unknown = state.get("unknown_models") or []
        if unknown:
            notes.append("? " + ", ".join(unknown))
        if notes:
            self.warning.set_text("   ".join(notes))
            self.warning.show()
        else:
            self.warning.hide()

        # After draw_limits, which owns `muted` for the no-account-figure case.
        self.set_stale(stale)
        return True

    def start_roll(self):
        """Run the animation, from now, on the one timer the panel has.

        Restarting the clock rather than adding a source is what makes a second
        turn landing mid-roll safe: `Roll.retarget` has already re-based the
        rows in flight on what they are showing, so progress going back to zero
        continues them from there instead of snapping them backwards.

        The duration is taken here, once, from the distance the rows have left
        to cover. Read per frame instead it would change under a retarget while
        the elapsed time it is divided into did not, and progress would jump.
        """
        self._roll_began = time.monotonic()
        self._roll_ms = roll.duration_ms(self.roll.distance())
        if self._roll_source is None:
            self._roll_source = GLib.timeout_add(ROLL_FRAME_MS, self.on_roll_frame)

    def on_roll_frame(self):
        elapsed_ms = (time.monotonic() - self._roll_began) * 1000.0
        for key in self.roll.frame(elapsed_ms / self._roll_ms):
            self.draw_row(key)
        if self.roll.running():
            return True
        self._roll_source = None
        return False

    def set_stale(self, stale):
        """Mute the dollar rows while the state is stale.

        Muting rather than hiding or zeroing: the last known figures are still
        the best information available, they just stop being presented as
        current. The warning row carries the age.

        The dollar rows only, because staleness here means one thing: the tally
        hook has stopped rewriting state.json. That is exactly what those rows
        are made of. The limit rows are not — read_limits goes to Claude Code's
        cache on every poll, so a dead hook does not make a percentage any older
        than it was, and grey would be claiming it did. They keep their colour,
        and their own freshness is answered where it belongs: the cache's age in
        the tooltip, and withdrawal on `resets_at` when the window is gone.

        Nothing here has to mute them for the dollar fallback either. A limit row
        with no account figure is showing state.json's dollars, and draw_limits
        has already marked it `muted` for that reason — the same class, arrived
        at from the row's own source rather than from this flag.
        """
        for label in self.usd_values:
            context = label.get_style_context()
            if stale:
                context.add_class("muted")
            else:
                context.remove_class("muted")
        # When fresh, the window rows are left alone: draw_limits has already set
        # or cleared their `muted` for the no-account-figure case, and clearing it
        # here would present a bare dollar figure as an account percentage.

    def draw_limits(self):
        """Paint every limit row from the account figures in state.json.

        Separate from draw_row because these rows do not animate: there is nothing
        to tween between 11 % and 12 %, and the figure behind them is re-asked of
        the server only when a session starts or the user runs /usage.

        Called from refresh(), which the 60-second staleness poll also drives —
        and it has to be, because a five-hour block can reset while nothing is
        writing state.json. That is the one case where a row changes with no new
        data behind it: the percentage is withdrawn because the window it
        described is gone.
        """
        rows = self.limits.get("rows") or {}
        age = self.limits.get("age_s")
        for key in WINDOW_KEYS:
            window = self.windows[key]
            limit = rows.get(WINDOW_KINDS[key])
            if key == SCOPED_KEY:
                # The server leaves `resets_at` off a scoped cap sitting at
                # nought, so the row takes the week's -- same cycle, and the
                # tooltip says where it came from. See scoped_limit.
                limit = scoped_limit(limit, rows.get(utilization.WEEKLY))
            label = self.rows[key]
            context = label.get_style_context()
            for name in LIMIT_CLASSES + ("muted",):
                context.remove_class(name)
            text, style = window_row(window, limit)
            context.add_class(style)
            label.set_text(text)
            if key == SCOPED_KEY:
                # Two things this row does that the others cannot: it names the
                # model the cap is on, and its tooltip has no machine dollars to
                # open with. Both because `window` is empty here -- state.json
                # records no per-model figure -- which is also what leaves the
                # row reading `—`, muted, when the account has no scoped cap.
                self.scoped_name.set_text(scoped_caption(limit))
                label.set_tooltip_text(scoped_tooltip(limit, age))
            else:
                label.set_tooltip_text(window_tooltip(window, limit, age))

    def draw_row(self, key):
        """Paint one value row from whatever the roll says it is showing.

        The dollar figure comes from the roll rather than from state.json, so a
        frame mid-tween and a settled row go through exactly one code path.

        Colour is whatever the row's own state says it is, mid-roll included:
        the only thing a frame changes is the text. Dimming the digits while
        they moved was tried and read as a blink, not as blur.
        """
        label = self.rows[key]
        context = label.get_style_context()
        for name in LIMIT_CLASSES + ("muted",):
            context.remove_class(name)

        value = self.roll.shown(key)
        if key == TURN_KEY:
            text = turn_text(value, self.roll.moving(key))
        else:
            text = _fmt_usd(value)
        label.set_text(text)

    def on_click(self, _widget, event):
        if event.button == 1:
            zone = resize_zone(event.x, event.y, *self.get_size())
            if zone is not None:
                # Everything the drag is measured against, sampled once here:
                # read per motion event instead, each frame would be measured
                # against the size the previous frame had just produced and the
                # panel would run away from the pointer.
                position, size = self.get_position(), self.get_size()
                self._resize = {"zone": zone,
                                "x": event.x_root, "y": event.y_root,
                                "scale": self.scale,
                                "x_window": position[0], "y_window": position[1],
                                "width": size.width, "height": size.height}
                return True
            self.begin_move_drag(event.button, int(event.x_root),
                                 int(event.y_root), event.time)
            return True  # on_configure persists wherever the drag ends up
        if event.button == 3:
            self.show_menu(event)
            return True
        return False

    def on_configure(self, _widget, _event):
        """Debounce position saves until movement stops.

        A fixed timer started at drag begin would record a mid-drag position and
        never the drop point, and repeated drags would stack timers. Resetting a
        single source on every configure-event stores exactly one position, the
        final one.
        """
        if self.at_anchor():
            return False  # our own re-anchor, not the user moving the window
        if self._position_timer:
            GLib.source_remove(self._position_timer)
        self._position_timer = GLib.timeout_add(700, self._persist_position)
        return False

    def _persist_position(self):
        self._position_timer = None
        if self._patrik_base is not None:
            # A shake is moving the window at this very moment, so wherever it is
            # now is not a position anybody chose. Come back once it has landed.
            #
            # This is the only route by which the celebration can reach the saved
            # position, and it is a narrow one: letting go of a drag starts this
            # debounce, and a turn landing inside those 700 ms has the panel
            # mid-wobble when the timer arrives. Without this the offset is
            # written to config.json and restored at every session afterwards.
            #
            # Rescheduling rather than dropping it: the drag that started this
            # was real and still has to be recorded. The wait is bounded, because
            # `_patrik_base` is cleared when the last glyph dies -- and glyphs
            # have a lifetime measured in a couple of seconds.
            self._position_timer = GLib.timeout_add(700, self._persist_position)
            return False
        if self.at_anchor():
            # Settled exactly where we put it, so this was a resize re-anchor
            # rather than a choice; claiming it would freeze the panel in place.
            return False
        self.user_positioned = True
        self._anchor = None
        self.remember_position()
        return False

    def menu_entries(self):
        """The right-click menu, as (caption, handler) pairs in order.

        A function of its own rather than a literal inside `show_menu` so the
        order and the captions can be asserted without a pointer event to pop a
        menu with. Both are things a test should hold: `Patrik mode` was asked for
        directly under `Refresh now`, and a toggle whose caption points the wrong
        way is the failure that goes unnoticed longest.

        Every flag is read here, as the menu is built, and none is cached: the CLI
        writes the same file, so a caption decided at startup could be a session
        out of date.
        """
        paused = autolaunch.paused()
        patrik_on = self.patrik_enabled()
        sound_on = self.sound_enabled()
        return (
            ("Refresh now", lambda *_: self.refresh()),
            ("Set Patrik mode off" if patrik_on else "Set Patrik mode on",
             lambda *_: self.set_patrik(not patrik_on)),
            ("Set sound off" if sound_on else "Set sound on",
             lambda *_: self.set_sound(not sound_on)),
            ("Reset position", lambda *_: self.reset_position()),
            ("Reset size", lambda *_: self.reset_scale()),
            ("Resume auto-launch" if paused else "Pause auto-launch",
             lambda *_: self.set_autolaunch_paused(not paused)),
            ("Quit", lambda *_: Gtk.main_quit()),
        )

    def build_menu(self):
        """The menu as a widget, assembled but not yet on screen.

        Split out of `show_menu` for the reason `menu_entries` is split out of
        this one: how the menu *looks* is as much a thing to hold as what it
        says, and the colour a caption ends up is a cascade that can only be
        read off a real widget -- see MenuColourTest. Neither needs a pointer
        event to get at.
        """
        menu = Gtk.Menu()
        for caption, handler in self.menu_entries():
            item = Gtk.MenuItem(label=caption)
            item.connect("activate", handler)
            menu.append(item)
        return menu

    def show_menu(self, event):
        menu = self.build_menu()
        menu.show_all()
        menu.popup_at_pointer(event)
        self.menu = menu  # keep a reference so it is not collected mid-display

    def set_autolaunch_paused(self, paused):
        """Pause or resume the panel opening itself at the next session.

        Through update_config rather than autolaunch.set_paused, because this
        side already has the lock discipline and the "not worth interrupting the
        user over" handling for a busy config file. The key and its meaning stay
        owned by cost_meter/autolaunch.py, which is what the hook reads.

        This never closes the panel. Pausing says what the *next* session does;
        quitting is the item below it.
        """
        self.update_config(
            lambda c: c.__setitem__(autolaunch.KEY, True) if paused
            else c.pop(autolaunch.KEY, None))

    def patrik_enabled(self):
        """Whether the panel celebrates a turn. Read from disk, never cached.

        Off for anything but a literal `true`, which covers both a config written
        before this existed and a hand-edited file with a string in it: a panel
        that started throwing glyphs because somebody typed `"yes"` would be a
        panel with no obvious way of being asked to stop.
        """
        config = store.read_json(paths.config_path(), default={}) or {}
        return config.get(PATRIK_KEY) is True

    def sound_enabled(self):
        """Whether a turn makes a noise. Read from disk, never cached.

        Off for anything but a literal `true`, exactly as `patrik_enabled` is: a
        panel that started beeping because somebody hand-edited `"yes"` into the
        config would be a panel with no obvious way of being asked to stop, and
        this one is audible to a whole room rather than only to its owner.
        """
        config = store.read_json(paths.config_path(), default={}) or {}
        return config.get(SOUND_KEY) is True

    def set_sound(self, on):
        """Turn the sound on or off, for this panel and the next one.

        Nothing plays on the click. The menu item promises the *next* turn, and
        a sound here would also fire on the state already sitting on disk -- the
        trap `Roll.replay` documents for the counting rows and `set_patrik` for
        the glyphs, reached a third way.
        """
        self.update_config(
            lambda c: c.__setitem__(SOUND_KEY, True) if on
            else c.pop(SOUND_KEY, None))

    def set_patrik(self, on):
        """Turn the celebration on or off, for this panel and the next one.

        Turning it off takes the overlay down there and then. Leaving one parked
        is not merely untidy: it is a transparent, always-on-top window the user
        cannot see and cannot reach, and the only thing that would ever remove it
        is quitting the panel.

        Switching it on deliberately does not celebrate. The menu item promises
        the *next* turn, and a burst on the click would also fire on the state
        already sitting on disk — the same trap `Roll.replay` documents for the
        counting rows, where a figure merely read at startup must not be announced
        as a new charge.
        """
        self.update_config(
            lambda c: c.__setitem__(PATRIK_KEY, True) if on
            else c.pop(PATRIK_KEY, None))
        if not on:
            self.end_patrik()

    def panel_rect(self):
        """The panel's rectangle in overlay coordinates: (x, y, width, height).

        What `Swarm.burst` is handed, so the glyphs start on the rows rather than
        somewhere in the margin. The overlay is the panel grown by PATRIK_MARGIN
        on every side, so the panel sits at exactly that offset inside it.
        """
        size = self.get_size()
        return (PATRIK_MARGIN, PATRIK_MARGIN, size.width, size.height)

    def build_overlay(self):
        """The glyph window, or None where the screen cannot composite one.

        None is a first-class answer, not a failure. Without a compositor or an
        RGBA visual the window would be drawn on an opaque background — a grey
        slab over the desktop, which is far worse than no glyphs — so the mode
        simply draws nothing and the meter carries on.

        That is not caution for its own sake. This panel has already died once on
        an `import gi` because a single DLL had no reputation with Smart App
        Control, and the lesson recorded then applies here exactly: a decoration
        must never be able to take the meter down with it.
        """
        screen = self.get_screen() or Gdk.Screen.get_default()
        visual = screen.get_rgba_visual()
        if visual is None or not screen.is_composited():
            return None
        try:
            overlay = PatrikOverlay(visual)
            overlay.set_default_size(*self.overlay_size())
            overlay.show_all()
            overlay.let_clicks_through()
        except Exception as error:
            # Logged rather than swallowed, and logged rather than raised: the
            # panel keeps running, and data/widget-output.log is where the reason
            # a burst never appeared has to be findable.
            log.write(f"patrik overlay: {error!r}")
            return None
        return overlay

    def overlay_size(self):
        size = self.get_size()
        return (size.width + 2 * PATRIK_MARGIN, size.height + 2 * PATRIK_MARGIN)

    def follow_overlay(self):
        """Sit the overlay over the panel, centred on it.

        Called on every frame rather than only at the start, because the panel can
        move under it — the shake moves it deliberately, and a drag can move it
        mid-burst. An overlay that stayed put would leave the glyphs behind.
        """
        if self.overlay is None:
            return
        x, y = self.get_position()
        self.overlay.resize(*self.overlay_size())
        self.overlay.move(x - PATRIK_MARGIN, y - PATRIK_MARGIN)

    def celebrate(self, turn_usd, session_usd=None):
        """Start a celebration for a turn costing `turn_usd`. A turn has landed.

        The caller decides what a turn is; this only obeys. `refresh()` runs from
        the file monitor, the staleness poll, `Refresh now` and `__init__`, and
        only one of those four is a charge — a burst driven by anything but
        `updated_at` moving would spray the panel every minute.

        The length comes from the cost, so an expensive turn is watched rather
        than crammed into the same window as a trivial one. A turn landing while
        the last one is still in the air restarts the clock on the new length and
        leaves the glyphs already flying alone, which is the same thing
        `Roll.retarget` does for the counting rows.

        How *fast* the glyphs arrive comes from `session_usd` instead — the
        session's running total, not this turn's cost. The turn already has its
        say in the length, and the two answer different questions: how big was
        that, against how deep are we in. Frozen here alongside the length rather
        than read per frame, because a total cannot meaningfully move inside the
        couple of seconds a celebration lasts, and a rate that changed mid-spray
        would be the one place the burst visibly changed its mind.

        The panel's *scale* is deliberately not frozen with it: see
        `on_patrik_frame`.
        """
        if self.overlay is None:
            self.overlay = self.build_overlay()
        if self.overlay is None:
            return
        self._patrik_ms = patrik.duration_ms(turn_usd)
        self._patrik_rate = patrik.rate(session_usd)
        self._shake_ms = self._patrik_ms * patrik.SHAKE_SHARE
        # Rebuilt per celebration rather than reused: the wobble's frequency is
        # derived from its duration, so a longer shake needs its own.
        self.shake = patrik.Shake(duration_ms=self._shake_ms)
        self.swarm.burst(patrik.BURST, self.panel_rect(),
                         max_life=self._patrik_ms / 1000.0, scale=self.scale)
        # The base is taken once per celebration rather than per frame: read live
        # it would include the previous frame's offset, and the errors would
        # accumulate into the panel walking across the screen.
        if self._patrik_base is None:
            self._patrik_base = tuple(self.get_position())
        self._patrik_began = time.monotonic()
        self._patrik_frame = self._patrik_began
        if self._patrik_source is None:
            self._patrik_source = GLib.timeout_add(PATRIK_FRAME_MS,
                                                   self.on_patrik_frame)
            self.sources.append(self._patrik_source)

    def on_patrik_frame(self):
        """One frame of the celebration. False when there is nothing left to do.

        Elapsed time is measured rather than assumed: a frame that arrives late
        has to advance the glyphs by however long it actually took, or a busy
        machine plays the whole thing in slow motion.

        New glyphs are emitted for as long as the animation runs, each with only
        the time that is left to live, so the celebration keeps arriving instead
        of spraying once and watching the spray fall — and still ends when it said
        it would.

        The scale is read here, per frame, rather than taken once in `celebrate`.
        Dragging the panel larger is how its size is set and can happen at any
        moment, celebration or not, and the spray has to follow the panel it is
        supposed to be coming out of instead of finishing at whatever size the
        panel was when the turn landed. Glyphs already in the air keep the size
        they were thrown at: re-sizing them mid-flight would be the whole burst
        twitching at once.
        """
        now = time.monotonic()
        dt, self._patrik_frame = now - self._patrik_frame, now
        elapsed_ms = (now - self._patrik_began) * 1000.0
        remaining = max(0.0, (self._patrik_ms - elapsed_ms) / 1000.0)
        if remaining > patrik.EMIT_FLOOR:
            self.swarm.emit(dt, self.panel_rect(), rate=self._patrik_rate,
                            max_life=remaining, scale=self.scale)
        self.swarm.frame(dt)
        self.shake_to(elapsed_ms / self._shake_ms)
        if self.overlay is not None:
            self.follow_overlay()
            self.overlay.particles = self.swarm.particles()
            self.overlay.queue_draw()
        # Both conditions, because either alone ends it early: the swarm empties
        # for an instant between two emitted glyphs, and the clock runs out while
        # the last of them is still fading.
        if self.swarm.running() or remaining > 0.0:
            return True
        self.end_patrik()
        return False

    def shake_to(self, progress):
        """Move the window to the shake's offset at `progress`, 0 to 1.

        Always relative to `_patrik_base`, never to where the window is now: read
        live, each frame would be offset from the previous frame's offset, the
        errors would accumulate, and the panel would walk across the screen.

        What keeps the wobble out of the saved position is not here — it is the
        deferral in `_persist_position`, which is the only path that can record
        one. Setting `_anchor` to the base for the duration was tried first and
        does nothing: `at_anchor()` is consulted when the debounce fires, and by
        then the window is back on its base whatever the anchor says.
        """
        if self._patrik_base is None:
            return
        base_x, base_y = self._patrik_base
        dx, dy = self.shake.offset(progress)
        self.move(base_x + dx, base_y + dy)

    def end_patrik(self):
        """Stop the burst, land the window, and take the overlay down.

        Safe to call when nothing is running, because both places that need it —
        the last frame, and the menu switching the mode off mid-burst — reach it
        by different routes and neither can know what the other has already done.
        """
        if self._patrik_source is not None:
            if self._patrik_source in self.sources:
                self.sources.remove(self._patrik_source)
            GLib.source_remove(self._patrik_source)
            self._patrik_source = None
        if self._patrik_base is not None:
            # Explicitly, rather than trusting the last frame to have been the one
            # at progress 1.0. An interrupted shake -- the mode switched off
            # mid-wobble -- never reaches that frame, and a window left a few
            # pixels out is a position the next debounce will record and keep.
            self.move(*self._patrik_base)
            self._patrik_base = None
        self.swarm = patrik.Swarm()
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None

    def reset_position(self):
        # Drop any debounce still in flight, or it would write the old position
        # straight back after the reset.
        if self._position_timer:
            GLib.source_remove(self._position_timer)
            self._position_timer = None
        self.update_config(lambda c: c.pop("widget_position", None))
        self.place()


def selftest(output):
    """Render one frame off-screen. Verifies GTK starts and the layout builds.

    Gdk.pixbuf_get_from_window cannot be used here: it is a screen scrape and
    asserts gdk_window_is_viewable, so on an unmapped window it fails and
    returns None. Gtk.OffscreenWindow renders the very same widget tree, with
    the same CSS, through the normal draw path and never maps anything.
    """
    window = CostMeter()
    content = window.get_child()
    window.remove(content)
    # Whatever the saved scale is, not WIDTH: a panel the user has resized
    # renders at its own size, and measuring it against the unscaled width
    # would call a correct frame too small.
    width = width_for_scale(window.scale)

    offscreen = Gtk.OffscreenWindow()
    offscreen.set_size_request(width, -1)
    offscreen.add(content)
    offscreen.show_all()
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)

    pixbuf = offscreen.get_pixbuf()
    if pixbuf is None:
        print("selftest failed: offscreen render produced no pixbuf",
              file=sys.stderr)
        return 1
    if pixbuf.get_width() < width or pixbuf.get_height() < 40:
        print(f"selftest failed: frame too small "
              f"({pixbuf.get_width()}x{pixbuf.get_height()})", file=sys.stderr)
        return 1

    # A blank frame is a single flat colour. Real rows and labels produce many.
    channels = pixbuf.get_n_channels()
    pixels = pixbuf.get_pixels()
    colors = {pixels[i:i + channels] for i in range(0, len(pixels), channels)}
    if len(colors) < 8:
        print(f"selftest failed: frame looks blank ({len(colors)} colours)",
              file=sys.stderr)
        return 1

    pixbuf.savev(output, "png", [], [])
    print(f"selftest wrote {output} "
          f"({pixbuf.get_width()}x{pixbuf.get_height()}, "
          f"{len(colors)} distinct colours)")
    return 0


def claim_liveness_lock():
    """Hold a lock for as long as this panel runs. Returns the handle, or None.

    This is what cost_meter/launch.py checks before starting a panel, and it
    replaces asking the operating system whether the pid in widget.pid is still
    alive. A pid cannot answer that question honestly on Windows, which reuses
    the numbers: one unrelated process landing on a dead panel's number was
    enough to suppress the launch in every session afterwards.

    None means a panel is already up, and main() exits on it rather than opening
    a second one. Two panels used to be allowed here on the grounds that only a
    human starting one by hand could reach it; the launcher's unavoidable gap
    between looking at this lock and the spawned panel taking it meant two
    hooks reached it too, and one of them was always a duplicate nobody asked
    for.
    """
    return store.try_acquire(paths.widget_lock_path())


def write_pid():
    """Record our pid, so a panel that needs killing can be found.

    Diagnostic only since the liveness claim moved to a lock. It is still worth
    writing: `launch: already running (pid N)` in the log is only actionable
    with the number in it, and a lock file's holder is not something the user
    can look up.
    """
    path = paths.pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def clear_pid():
    """Remove the pid file, but only while it is still ours.

    Checked rather than unlinked blindly: if this panel was killed with SIGKILL
    and a later one took over the file, that survivor's claim must outlive us.
    A kill that skips this cleanup leaves the file behind — harmless now that
    the file is only a diagnostic, and the reason it stopped being more.
    """
    path = paths.pid_path()
    try:
        if int(path.read_text(encoding="utf-8").strip()) != os.getpid():
            return
        path.unlink()
    except (OSError, ValueError):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", metavar="PNG",
                        help="render one frame to PNG and exit")
    args = parser.parse_args()

    # A selftest is not a running panel, so it deliberately claims neither the
    # lock nor the pid file: either would make the launcher skip a real panel
    # afterwards.
    if args.selftest:
        return selftest(args.selftest)

    # Claimed before anything is built, and a failed claim ends the run. The
    # claim is the only place a second panel can be stopped: cost_meter/launch.py
    # looks at the same lock, but it cannot look and spawn in one step -- the
    # panel it starts needs a nested `pixi run` and GTK startup, seconds, before
    # it claims anything, and two SessionStart hooks firing that close together
    # (Claude Desktop fires two per code-mode session) both find the lock free
    # and both spawn. Whoever loses here costs a wasted interpreter; before this
    # it cost a second window that stayed for the whole session.
    #
    # Before the window rather than after, so the loser never flashes one, and
    # before the pid file so it cannot clear the winner's claim on its way out --
    # `launch: already running (pid None)` in the log was exactly that.
    handle = claim_liveness_lock()
    if handle is None:
        log.write("widget: another panel holds the lock, exiting")
        return 0
    CostMeter().show_all()
    # The pid is written inside the claim and removed before it is given back,
    # so there is no instant where a launcher can see the claim gone while this
    # panel is still on screen.
    write_pid()
    try:
        Gtk.main()
    finally:
        clear_pid()
        store.release(handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
