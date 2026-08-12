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
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from cost_meter import autolaunch, paths, roll, store, summary  # noqa: E402

MARGIN = 24
WIDTH = 240
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
ROLL_KEYS = ("last_turn", "session", "today", "window_5h", "window_7d")
TURN_KEY = "last_turn"
WINDOW_KEYS = ("window_5h", "window_7d")
ROLL_FRAME_MS = 16
ROLL_MIN_DELTA = 0.01

CSS = b"""
window { background-color: #1e1e22; }
label { color: #d8d8dc; font-family: monospace; font-size: 11px; }
label.value { font-weight: bold; }
label.muted { color: #8a8a92; }
label.green { color: #78d178; }
label.amber { color: #e3b341; }
label.red { color: #f06a5a; }
label.warn { color: #f06a5a; font-size: 10px; }
"""


def _fmt_usd(value):
    return "—" if value is None else f"${value:,.2f}"


def _fmt_reset(value):
    """Local `HH:MM` for a block's reset time, or None if there isn't one.

    Local time because that is the clock /usage prints, and the row is only
    worth having if the two can be read against each other. An unparseable
    value is dropped rather than shown raw: a broken timestamp on the row would
    look like a limit resetting at a nonsense time.
    """
    epoch = summary.parse_updated_at(value)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


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


def window_row(window):
    """The text and style class for one limit row, as (text, class).

    Calibrated rows carry both figures. The percentage alone would mean
    calibrating traded the dollar amount away — and since the percentage is only
    ever an estimate against a ceiling you derived yourself, the dollars beside it
    are the part that is actually measured. The `~` is what marks the estimate;
    there is no room for the word as well once both numbers are on the row.

    The 5-hour row also names when its block resets; the weekly row carries no
    such key and is unchanged. The reset time is shown whether or not the row is
    calibrated, because it is measured rather than estimated — it is the one
    figure here that can be checked against /usage directly.
    """
    usd = _fmt_usd(window.get("usd"))
    resets = _fmt_reset(window.get("resets_at"))
    tail = "" if resets is None else f" · {resets}"
    pct = window.get("pct")
    if pct is None:
        # Not calibrated: dollars only, muted, rather than an invented number.
        return usd + tail, "muted"
    return (f"{usd} ~{pct} %{tail}",
            "red" if pct >= RED_AT else "amber" if pct >= AMBER_AT else "green")


def _row(grid, index, caption):
    left = Gtk.Label(label=caption, xalign=0.0)
    right = Gtk.Label(label="—", xalign=1.0)
    right.get_style_context().add_class("value")
    right.set_hexpand(True)
    grid.attach(left, 0, index, 1, 1)
    grid.attach(right, 1, index, 1, 1)
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
        self.set_resizable(False)
        self.set_default_size(WIDTH, -1)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self.on_click)
        self.connect("destroy", Gtk.main_quit)

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

        grid = Gtk.Grid(row_spacing=3, column_spacing=12)
        grid.set_border_width(10)
        self.add(grid)

        self.last_turn = _row(grid, 0, "last turn")
        self.session = _row(grid, 1, "session")
        self.today = _row(grid, 2, "today")
        grid.attach(Gtk.Separator(), 0, 3, 2, 1)
        self.window_5h = _row(grid, 4, "5h window")
        self.window_7d = _row(grid, 5, "week")
        # Split because `muted` has two owners: staleness for all five rows, and
        # draw_row for the two window rows when there is no calibration.
        self.usd_values = (self.last_turn, self.session, self.today)
        self.window_values = (self.window_5h, self.window_7d)
        self.rows = {"last_turn": self.last_turn,
                     "session": self.session, "today": self.today,
                     "window_5h": self.window_5h, "window_7d": self.window_7d}

        # Animation state. `windows` holds the last two window dicts because a
        # rolling limit row rebuilds composite text — dollars, percentage and
        # reset time — from a dollar figure the roll owns and two fields it does
        # not. `_roll_source` is a single timer for the whole panel, retargeted
        # in place rather than stacked; `_roll_began` is what progress is
        # measured from, so a slow frame shortens the roll instead of stretching
        # it past its duration, and `_roll_ms` is how long this particular roll
        # was given — it depends on the distance, so it is decided per roll.
        self.roll = roll.Roll(min_delta=ROLL_MIN_DELTA)
        self.windows = {key: {} for key in WINDOW_KEYS}
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
        grid.attach(self.warning, 0, 6, 2, 1)

        self.watch()
        self.refresh()  # before place(), so the first anchor sees real content
        self.place()
        GLib.timeout_add_seconds(STALE_POLL_SECONDS, self.refresh)

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

        calibrate.py writes ceilings into the same file; without the lock a
        drag could clobber a ceiling written moments earlier and silently send
        the display back to dollars. calibrate.py uses the same helper, so both
        sides hold the lock across the read as well as the write.
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
        rolling = self.roll.retarget({
            "session": (state.get("session") or {}).get("usd"),
            "today": state.get("today_usd"),
            **{key: self.windows[key].get("usd") for key in WINDOW_KEYS},
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

        # After draw_row, which owns `muted` for the uncalibrated case.
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
        # When fresh, the window rows are left alone: draw_row has already
        # set or cleared their `muted` for the uncalibrated case, and clearing it
        # here would present an uncalibrated dollar figure as a calibrated one.

    def draw_row(self, key):
        """Paint one value row from whatever the roll says it is showing.

        The dollar figure comes from the roll rather than from state.json, so a
        frame mid-tween and a settled row go through exactly one code path.
        For the limit rows that figure is substituted into the window dict and
        `window_row` builds the composite text as usual: the percentage and the
        reset time hold still while the dollars move, which is right — the
        percentage is a rounded estimate against a ceiling you derived yourself,
        and animating it would put motion on the least measured thing on screen.

        Colour is whatever the row's own state says it is, mid-roll included:
        the only thing a frame changes is the text. Dimming the digits while
        they moved was tried and read as a blink, not as blur.
        """
        label = self.rows[key]
        context = label.get_style_context()
        for name in LIMIT_CLASSES + ("muted",):
            context.remove_class(name)

        value = self.roll.shown(key)
        if key in WINDOW_KEYS:
            text, style = window_row({**self.windows[key], "usd": value})
            context.add_class(style)
        elif key == TURN_KEY:
            text = turn_text(value, self.roll.moving(key))
        else:
            text = _fmt_usd(value)
        label.set_text(text)

    def on_click(self, _widget, event):
        if event.button == 1:
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

    offscreen = Gtk.OffscreenWindow()
    offscreen.set_size_request(WIDTH, -1)
    offscreen.add(content)
    offscreen.show_all()
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)

    pixbuf = offscreen.get_pixbuf()
    if pixbuf is None:
        print("selftest failed: offscreen render produced no pixbuf",
              file=sys.stderr)
        return 1
    if pixbuf.get_width() < WIDTH or pixbuf.get_height() < 40:
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
