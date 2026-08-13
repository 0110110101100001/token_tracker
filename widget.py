#!/usr/bin/env python3
"""Always-on-top cost meter, anchored bottom-right.

Reads data/state.json and nothing else. Run it through run_widget.sh or
run_widget.cmd, which enter the pixi environment; on Linux that sets
GDK_BACKEND=x11 so the window can place and raise itself.
"""

import argparse
import os
import sys
import time
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
# Gdk 4.0 is also installed here; without this the bare import picks 4.0 and
# then collides with the Gtk 3.0 requirement above.
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from cost_meter import autolaunch, paths, roll, store, summary, utilization  # noqa: E402

MARGIN = 24
WIDTH = 240
# Drag-to-resize. The panel scales as one piece — font, padding and width
# together — rather than the frame alone: the content is five fixed rows, so a
# wider window on its own would buy nothing but blank space around numbers that
# stayed exactly as small. One number therefore drives everything, and it is
# taken from the horizontal component of the drag. Height is never an input;
# there are five rows and they are as tall as the font makes them.
MIN_SCALE = 0.7
MAX_SCALE = 3.0
# The grab band around the window's perimeter, and how far along a side still
# counts as that side's corner. Undecorated windows get no frame from anybody,
# so this band is the whole handle; 6 px is what the pointer can find without
# the band eating drags meant to move the panel. A 6 px corner square would be
# too small to hit, hence the longer reach, as on any real window frame.
EDGE = 6
CORNER = 16
AMBER_AT = 60
RED_AT = 85
# Colour classes for the two limit rows. Declared after `muted` in the CSS, so
# they win the cascade wherever both apply.
LIMIT_CLASSES = ("green", "amber", "red")
# The file monitor only fires when the hook writes, so a hook that has stopped
# writing would never trigger a redraw — which is exactly the case the staleness
# row exists to report. This timer is the only thing that notices.
STALE_POLL_SECONDS = 60

# The rolling figures. Every row tweens to its new value instead of snapping to
# it, so a turn that cost $4 and one that cost $0.04 stop looking identical.
#
# The cumulative rows roll from their previous total. `last_turn` is a delta and
# rolls from zero instead, through `Roll.replay`: the distance between one
# turn's cost and the next one's is not a quantity worth animating, and it would
# run the row downwards whenever a cheap turn followed an expensive one.
#
# The two limit rows are not among them. They carry an integer percentage that
# moves once every few hours, which has nothing to tween, and the dollars that
# used to animate there have moved into the tooltip.
ROLL_KEYS = ("last_turn", "session", "today")
TURN_KEY = "last_turn"
WINDOW_KEYS = ("window_5h", "window_7d")
# Which account limit each row draws, named as the server names them.
WINDOW_KINDS = {"window_5h": utilization.SESSION,
                "window_7d": utilization.WEEKLY}
# The server's severity, mapped onto the panel's colour classes.
SEVERITY_CLASSES = {"normal": "green", "warning": "amber", "critical": "red"}
ROLL_FRAME_MS = 16
ROLL_MIN_DELTA = 0.01

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
CSS = b"""
window { background-color: #1e1e22; }
label { color: #d8d8dc; font-family: monospace; }
label.value { font-weight: bold; }
label.muted { color: #8a8a92; }
label.green { color: #78d178; }
label.amber { color: #e3b341; }
label.red { color: #f06a5a; }
label.warn { color: #f06a5a; }
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


def width_for_scale(scale):
    return round(WIDTH * scale)


def resize_zone(x, y, width, height):
    """Which resize handle the pointer is over, or None for the panel's body.

    None means the same drag moves the window instead, so the band has to stay
    off the middle: a grab zone that swallowed an intended move would make the
    panel feel stuck.

    The top and bottom edges are deliberately not handles. Height follows the
    content and the scale, so a vertical-only drag would have nothing to change,
    and offering a resize cursor for a drag that does nothing is worse than
    leaving it a move. The corners are handles because they carry a horizontal
    component like any other.
    """
    west, east = x < EDGE, x >= width - EDGE
    if not (west or east):
        return None
    side = "west" if west else "east"
    if y < CORNER:
        return f"north_{side}"
    if y >= height - CORNER:
        return f"south_{side}"
    return side


def drag_scale(zone, start_scale, dx):
    """The scale a horizontal drag of `dx` pixels arrives at.

    Outwards grows the panel on either side, so a drag on a west handle counts
    the opposite direction. `WIDTH` is the divisor because it is what scale 1.0
    measures: dragging a full panel width doubles the panel.
    """
    outwards = -dx if zone.endswith("west") else dx
    return clamp_scale(start_scale + outwards / WIDTH)


def drag_origin(zone, start_x, start_width, width):
    """Where the window has to start so the un-grabbed edge stays put.

    Grab the left edge and the right one should not move; that means shifting
    the window by whatever the width gained. An east handle keeps the origin,
    which is where the window already grows from.
    """
    if zone.endswith("west"):
        return start_x + start_width - width
    return start_x


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


def window_row(window, limit, now=None):
    """The text and style class for one limit row, as (text, class).

    With an account figure the row is that figure and nothing else. The percentage
    describes the whole account and the dollars describe this machine; two scopes
    on one row read as one claim, and the reader divides them — which is exactly
    the arithmetic the old calibrated percentage was doing wrongly. The dollars
    move to the tooltip, which has the room to say which is which.

    The percentage is always marked `≥`. The figure is re-asked of the server when
    a session starts and when the user runs /usage, and at no other time, so it is
    usually hours old — and usage within a window only grows, which makes it a
    floor rather than a reading. Unconditional, because a marker that came and
    went would imply the unmarked form is exact, and it never is: even a
    twelve-second-old figure has had twelve seconds to rise. This is the `~`
    marker's replacement and it claims something stronger — `~` admitted the
    number could be wrong in either direction.

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
    return f"≥{pct} %{tail}", severity_class(limit.get("severity"), pct)


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
    lines = [f"{_fmt_usd(window.get('usd'))} on this machine"]
    pct = _pct_of(limit)
    if pct is None:
        lines.append("no account figure available")
        return "\n".join(lines)
    if window_expired(limit, now):
        lines.append("the account figure describes a window that has reset")
        return "\n".join(lines)
    resets = _fmt_reset(limit.get("resets_at"), now)
    account = f"account at least {pct} %"
    if resets is not None:
        account += f", resets {resets}"
    lines.append(account)
    if age_s is None:
        lines.append("/usage refreshes it")
    else:
        lines.append(f"figure {summary.format_age(age_s)} old; /usage refreshes it")
    return "\n".join(lines)


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
    left = Gtk.Label(label=caption, xalign=0.0)
    right = Gtk.Label(label="—", xalign=1.0)
    right.get_style_context().add_class("value")
    right.set_hexpand(True)
    grid.attach(left, 0, index, 1, 1)
    grid.attach(right, 1, index, 1, 1)
    labels.extend((left, right))
    return right


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
        self.window_7d = _row(grid, 5, "week", self.labels)
        # Below a separator of its own: everything above is a measured figure,
        # and this is the fact that says what those figures mean — money owed on
        # API billing, notional against a seat.
        grid.attach(Gtk.Separator(), 0, 6, 2, 1)
        self.billing = _row(grid, 7, "billing", self.labels)
        # Split because `muted` has two owners: staleness for all five rows, and
        # draw_limits for the two window rows when there is no account figure.
        # The billing row rides with the plain ones — it is drawn once from
        # state.json and only staleness ever mutes it.
        self.usd_values = (self.last_turn, self.session, self.today,
                           self.billing)
        self.window_values = (self.window_5h, self.window_7d)
        self.rows = {"last_turn": self.last_turn,
                     "session": self.session, "today": self.today,
                     "window_5h": self.window_5h, "window_7d": self.window_7d}

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

        self.warning = Gtk.Label(label="", xalign=0.0)
        self.warning.get_style_context().add_class("warn")
        self.warning.set_no_show_all(True)
        grid.attach(self.warning, 0, 8, 2, 1)

        # After every label exists, since this is what sizes them.
        self.apply_scale(self.scale)

        self.watch()
        self.refresh()  # before place(), so the first anchor sees real content
        self.place()
        GLib.timeout_add_seconds(STALE_POLL_SECONDS, self.refresh)

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
                           event.x_root - start["x"])
        if scale != self.scale:
            self.apply_scale(scale)
            self.move_for_drag(start)
        return True

    def move_for_drag(self, start):
        """Keep the edge the user is not holding where they left it.

        Only when the user has placed the panel themselves. While it is anchored
        the corner owns the position: on_size_allocate re-anchors on every scale
        change, so moving here would be overruled a moment later anyway, and the
        right edge already stays put because that is the edge in the corner.
        """
        if not self.user_positioned:
            return
        self.move(drag_origin(start["zone"], start["x_window"],
                              start["width"], width_for_scale(self.scale)),
                  self.get_position()[1])

    def on_release(self, _widget, _event):
        if self._resize is None:
            return False
        self._resize = None
        self.remember_scale()
        return True

    def place(self):
        config = store.read_json(paths.config_path(), default={}) or {}
        position = config.get("widget_position")
        self.user_positioned = bool(position)
        self._anchor = None
        if position:
            # Restoring a saved position is a move we make ourselves, so record
            # it as the anchor too; otherwise every startup writes the same
            # coordinates straight back and takes the lock to do it.
            self._anchor = (int(position[0]), int(position[1]))
            self.move(*self._anchor)
            return
        self.anchor_bottom_right()

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
        path = paths.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.monitor = Gio.File.new_for_path(str(path)).monitor_file(
            Gio.FileMonitorFlags.NONE, None
        )
        self.monitor.connect("changed", lambda *_: self.refresh())

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
            turn_rolling = self.roll.replay(
                TURN_KEY, state.get("last_turn_usd") or 0.0)

        for key in WINDOW_KEYS:
            self.windows[key] = state.get(key) or {}
        self.limits = state.get("limits") or {}
        rolling = self.roll.retarget({
            "session": (state.get("session") or {}).get("usd"),
            "today": state.get("today_usd"),
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
        """Mute every value row while the state is stale.

        Muting rather than hiding or zeroing: the last known figures are still
        the best information available, they just stop being presented as
        current. The warning row carries the age.
        """
        for label in self.usd_values:
            context = label.get_style_context()
            if stale:
                context.add_class("muted")
            else:
                context.remove_class("muted")
        if stale:
            for label in self.window_values:
                context = label.get_style_context()
                # The colour classes are declared after `muted` in the CSS, so
                # they would win the cascade and a stale limit row would still
                # read confidently green or red. Drop the colour to mute it.
                for name in LIMIT_CLASSES:
                    context.remove_class(name)
                context.add_class("muted")
        # When fresh, the window rows are left alone: draw_limits has already set
        # or cleared their `muted` for the no-account-figure case, and clearing it
        # here would present a bare dollar figure as an account percentage.

    def draw_limits(self):
        """Paint both limit rows from the account figures in state.json.

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
            label = self.rows[key]
            context = label.get_style_context()
            for name in LIMIT_CLASSES + ("muted",):
                context.remove_class(name)
            text, style = window_row(window, limit)
            context.add_class(style)
            label.set_text(text)
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
                self._resize = {"zone": zone, "x": event.x_root,
                                "scale": self.scale,
                                "x_window": self.get_position()[0],
                                "width": self.get_size().width}
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
        if self.at_anchor():
            # Settled exactly where we put it, so this was a resize re-anchor
            # rather than a choice; claiming it would freeze the panel in place.
            return False
        self.user_positioned = True
        self._anchor = None
        self.remember_position()
        return False

    def show_menu(self, event):
        menu = Gtk.Menu()
        # Read as the menu is built, not cached: the CLI writes the same key,
        # so a caption decided at startup could be a session out of date.
        paused = autolaunch.paused()
        for caption, handler in (
            ("Refresh now", lambda *_: self.refresh()),
            ("Reset position", lambda *_: self.reset_position()),
            ("Reset size", lambda *_: self.reset_scale()),
            ("Resume auto-launch" if paused else "Pause auto-launch",
             lambda *_: self.set_autolaunch_paused(not paused)),
            ("Quit", lambda *_: Gtk.main_quit()),
        ):
            item = Gtk.MenuItem(label=caption)
            item.connect("activate", handler)
            menu.append(item)
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

    None means somebody else holds it — a panel started by hand alongside the
    one already up. That is allowed; it simply is not the one whose exit frees
    the lock.
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

    CostMeter().show_all()
    # Taken before the pid is written and given back after it is removed, so
    # there is no instant where a launcher can see the claim gone while this
    # panel is still on screen.
    handle = claim_liveness_lock()
    write_pid()
    try:
        Gtk.main()
    finally:
        clear_pid()
        if handle is not None:
            store.release(handle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
