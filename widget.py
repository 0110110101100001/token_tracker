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

import gi

gi.require_version("Gtk", "3.0")
# Gdk 4.0 is also installed here; without this the bare import picks 4.0 and
# then collides with the Gtk 3.0 requirement above.
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from cost_meter import paths, store, summary  # noqa: E402

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
        # set_window_row for the two window rows when there is no calibration.
        self.usd_values = (self.last_turn, self.session, self.today)
        self.window_values = (self.window_5h, self.window_7d)

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

        delta = state.get("last_turn_usd") or 0.0
        self.last_turn.set_text(f"+{_fmt_usd(delta)}" if delta else "—")
        self.session.set_text(_fmt_usd((state.get("session") or {}).get("usd")))
        self.today.set_text(_fmt_usd(state.get("today_usd")))
        self.set_window_row(self.window_5h, state.get("window_5h") or {})
        self.set_window_row(self.window_7d, state.get("window_7d") or {})

        # A broken tally exits 0 and simply stops rewriting state.json, so
        # without this the panel would keep showing hours-old figures as though
        # they were current — the same invisible-gap failure the `?` row exists
        # to prevent, reached from the other side.
        stale, age = summary.staleness(state, time.time())

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

        # After set_window_row, which owns `muted` for the uncalibrated case.
        self.set_stale(stale)
        return True

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
        # When fresh, the window rows are left alone: set_window_row has already
        # set or cleared their `muted` for the uncalibrated case, and clearing it
        # here would present an uncalibrated dollar figure as a calibrated one.

    def set_window_row(self, label, window):
        context = label.get_style_context()
        for name in LIMIT_CLASSES + ("muted",):
            context.remove_class(name)

        pct = window.get("pct")
        if pct is None:
            # Not calibrated yet: show dollars rather than an invented number.
            label.set_text(_fmt_usd(window.get("usd")))
            context.add_class("muted")
            return
        label.set_text(f"~{pct} % est.")
        context.add_class("red" if pct >= RED_AT else
                          "amber" if pct >= AMBER_AT else "green")

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
        for caption, handler in (
            ("Refresh now", lambda *_: self.refresh()),
            ("Reset position", lambda *_: self.reset_position()),
            ("Quit", lambda *_: Gtk.main_quit()),
        ):
            item = Gtk.MenuItem(label=caption)
            item.connect("activate", handler)
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        self.menu = menu  # keep a reference so it is not collected mid-display

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


def write_pid():
    """Record our pid so cost_meter/launch.py can tell whether a panel is up.

    Process-name matching cannot do this job any more: under pixi the command
    line is `.pixi/envs/default/bin/python widget.py`, so a pattern naming
    python3 never matches and every session would start another panel.
    """
    path = paths.pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def clear_pid():
    """Remove the pid file, but only while it is still ours.

    Checked rather than unlinked blindly: if this panel was killed with SIGKILL
    and a later one took over the file, that survivor's claim must outlive us.
    A kill that skips this cleanup leaves the file behind, which is exactly why
    the launcher probes the pid for liveness instead of trusting the file.
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

    # A selftest is not a running panel, so it deliberately claims no pid file:
    # writing one would make the launcher skip a real panel afterwards.
    if args.selftest:
        return selftest(args.selftest)

    CostMeter().show_all()
    write_pid()
    try:
        Gtk.main()
    finally:
        clear_pid()
    return 0


if __name__ == "__main__":
    sys.exit(main())
