#!/usr/bin/env python3
"""Always-on-top cost meter, anchored bottom-right.

Reads data/state.json and nothing else. Run it through run_widget.sh, which
sets GDK_BACKEND=x11 so the window can place and raise itself.
"""

import argparse
import sys

import gi

gi.require_version("Gtk", "3.0")
# Gdk 4.0 is also installed here; without this the bare import picks 4.0 and
# then collides with the Gtk 3.0 requirement above.
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from cost_meter import paths, store  # noqa: E402

MARGIN = 24
WIDTH = 240
AMBER_AT = 60
RED_AT = 85

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

        self.warning = Gtk.Label(label="", xalign=0.0)
        self.warning.get_style_context().add_class("warn")
        self.warning.set_no_show_all(True)
        grid.attach(self.warning, 0, 6, 2, 1)

        self.place()
        self.watch()
        self.refresh()

    def place(self):
        config = store.read_json(paths.config_path(), default={}) or {}
        position = config.get("widget_position")
        if position:
            self.move(int(position[0]), int(position[1]))
            return
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()
        # Before realize the preferred width underreports (200 here), but
        # set_default_size already fixed the real width at WIDTH, so anchoring
        # on the smaller number pushes the right edge off the monitor.
        width = max(self.get_preferred_size()[1].width, WIDTH)
        height = 140
        self.move(area.x + area.width - width - MARGIN,
                  area.y + area.height - height - MARGIN)

    def update_config(self, mutate):
        """Read-modify-write config under the lock.

        calibrate.py writes ceilings into the same file; without the lock a
        drag could clobber a ceiling written moments earlier and silently send
        the display back to dollars.
        """
        try:
            with store.exclusive_lock(paths.lock_path()):
                config = store.read_json(paths.config_path(), default={}) or {}
                mutate(config)
                store.write_json_atomic(paths.config_path(), config)
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

        unknown = state.get("unknown_models") or []
        if unknown:
            self.warning.set_text("? " + ", ".join(unknown))
            self.warning.show()
        else:
            self.warning.hide()
        return True

    def set_window_row(self, label, window):
        context = label.get_style_context()
        for name in ("green", "amber", "red", "muted"):
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
            GLib.timeout_add(500, self._store_position_once)
            return True
        if event.button == 3:
            self.show_menu(event)
            return True
        return False

    def _store_position_once(self):
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", metavar="PNG",
                        help="render one frame to PNG and exit")
    args = parser.parse_args()

    if args.selftest:
        return selftest(args.selftest)

    CostMeter().show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
