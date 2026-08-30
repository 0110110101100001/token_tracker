# tests/test_widget.py
"""The panel: the text of its two limit rows, and where the window itself lands.

Two unrelated concerns share this file because they share a subject. The row
tests are pure text assertions against `window_row`, which is why that text is
built by a function of its own rather than inline in the widget -- they need no
display and run everywhere. The taskbar test needs a real GTK window, because
the question there is not what the panel asks for but what the window manager
gives it, and on Windows those two came apart: set_skip_taskbar_hint() is
silently ignored by GDK's win32 backend, so the panel sat in the taskbar for its
whole run while the code read as though it had opted out.

`widget` imports at module scope: importing it only loads GTK, which works
headless, and the row tests need it. Creating a window is the part that needs a
display, so only the taskbar test is skipped without one -- as in the widget
selftest in smoke.py, where a headless box has no taskbar to be in either.
"""

import ctypes
import os
import sys
import time
import unittest
import unittest.mock
from datetime import datetime, timezone

import widget
from tests.support import TempHome
from cost_meter import autolaunch, launch, paths, roll, store

HAS_DISPLAY = launch.has_display()

if HAS_DISPLAY:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
GWL_EXSTYLE = -20
GW_OWNER = 4


def own_hwnds_titled(title):
    """Every top-level window this process owns under `title`.

    Both halves of that are load-bearing. By title alone this would find the
    panel that is very likely running while the tests are -- same title, other
    process -- and measure a window the test did not build. By pid alone it
    would find GTK's own hidden helper top-levels too, which are not windows
    anybody asked about.
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
            if buffer.value == title:
                found.append(hwnd)
        return True

    user32.EnumWindows(visit, None)
    return found


@unittest.skipUnless(HAS_DISPLAY, "no display")
class AutolaunchToggleTest(unittest.TestCase):
    """The menu item writes the key the SessionStart hook actually reads.

    Both halves of the toggle are a couple of lines of lambda in `show_menu`,
    and getting them wrong fails silently in the worst direction: a menu that
    reads `Resume auto-launch` while every new session keeps opening the panel
    anyway. The panel's config writer is also the one that has to preserve the
    window position and the scale sharing that file.
    """

    def setUp(self):
        self.window = widget.CostMeter()
        # No main loop here, so the close handler would turn teardown into a
        # Gtk-CRITICAL; the config keys go back as they were because this class
        # shares one COST_METER_HOME with the rest of the run.
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)
        self.addCleanup(self.window.update_config,
                        lambda c: c.pop("widget_position", None))
        self.addCleanup(self.window.set_autolaunch_paused, False)

    def config(self):
        return store.read_json(paths.config_path(), default={}) or {}

    def test_the_toggle_pauses_and_resumes_what_the_hook_reads(self):
        self.window.set_autolaunch_paused(True)
        self.assertTrue(autolaunch.paused())
        self.window.set_autolaunch_paused(False)
        self.assertFalse(autolaunch.paused())

    def test_toggling_leaves_the_window_position_alone(self):
        self.window.update_config(
            lambda c: c.__setitem__("widget_position", [7, 9]))
        self.window.set_autolaunch_paused(True)
        self.window.set_autolaunch_paused(False)
        self.assertEqual(self.config().get("widget_position"), [7, 9])

    def test_resuming_leaves_no_key_behind(self):
        self.window.set_autolaunch_paused(True)
        self.window.set_autolaunch_paused(False)
        self.assertNotIn(autolaunch.KEY, self.config())


@unittest.skipUnless(HAS_DISPLAY, "no display")
class SpawnPositionTest(unittest.TestCase):
    """Where a fresh panel opens, and why the saved spot no longer decides it.

    A saved position carries the monitor it was chosen on. Restore it and a panel
    once dragged onto a second screen opens there for every session after --
    correctly, and invisibly to anyone watching the first screen. It is the worst
    kind of failure to look into, because nothing is broken: `pixi run start`
    reports a spawned pid, the process runs, the window is mapped and
    `IsWindowVisible` says true, and the log has nothing to report.

    The origin sits on the primary monitor, which is the screen somebody who
    just started the panel is certain to be watching.
    """

    def setUp(self):
        self.window = widget.CostMeter()
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)
        self.addCleanup(self.window.update_config,
                        lambda c: c.pop("widget_position", None))

    def test_it_opens_at_the_origin(self):
        self.window.place()
        self.assertEqual(tuple(self.window.get_position()),
                         widget.SPAWN_POSITION)

    def test_a_saved_position_does_not_move_it(self):
        self.window.update_config(
            lambda c: c.__setitem__("widget_position", [640, 480]))
        self.window.place()
        self.assertEqual(tuple(self.window.get_position()),
                         widget.SPAWN_POSITION)

    def test_a_position_on_another_monitor_does_not_follow_it(self):
        # Measured off the panel this rule came out of, sitting on the second
        # monitor. GTK's own figures, and worth keeping as such: Win32 put the
        # same window at (3412, 527), and the 1.25 between the pairs is the
        # display scaling. Read as Win32 coordinates these look like a panel
        # dragged off the end of the desktop, which is exactly the wrong
        # conclusion -- there was never anything off-screen about it.
        self.window.update_config(
            lambda c: c.__setitem__("widget_position", [4265, 659]))
        self.window.place()
        self.assertEqual(tuple(self.window.get_position()),
                         widget.SPAWN_POSITION)

    def test_the_automatic_anchor_does_not_pull_it_back(self):
        """The origin has to survive the first resize.

        on_size_allocate re-anchors to the bottom-right corner whenever the
        panel has not been positioned by the user, and it runs on every size
        change -- so a place() that left that flag alone would put the window at
        the origin and have it slide away again a frame later.
        """
        self.window.place()
        self.window.on_size_allocate(None, None)
        self.assertEqual(tuple(self.window.get_position()),
                         widget.SPAWN_POSITION)


@unittest.skipUnless(HAS_DISPLAY, "no display")
class TaskbarTest(unittest.TestCase):
    def setUp(self):
        self.window = widget.CostMeter()
        # The panel quits the main loop when its window closes, and there is no
        # main loop here -- leaving it connected turns every teardown into a
        # Gtk-CRITICAL on stderr.
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)

    def test_asks_to_be_a_utility_window(self):
        """UTILITY is the hint both backends act on.

        DOCK would read as the more accurate description of this panel and is
        the obvious thing to reach for -- but GDK's win32 backend leaves a DOCK
        window in the taskbar, and only UTILITY, TOOLBAR and MENU earn the
        tool-window style that keeps it out.
        """
        self.assertEqual(self.window.get_type_hint(),
                         Gdk.WindowTypeHint.UTILITY)

    @unittest.skipUnless(os.name == "nt", "Windows taskbar rules")
    def test_windows_gives_it_no_taskbar_button(self):
        """The condition Windows itself uses, checked on the real window.

        A top-level window with no owner gets a taskbar button unless it is a
        tool window; WS_EX_APPWINDOW would force one back on regardless. This
        asserts the outcome rather than the hint, because the hint is only a
        request and the mapping belongs to whichever GTK build is installed.
        """
        self.window.realize()  # creates the HWND, without putting it on screen
        hwnds = own_hwnds_titled(self.window.get_title())
        self.assertEqual(len(hwnds), 1, f"expected one window, found {hwnds}")
        hwnd = hwnds[0]
        exstyle = ctypes.windll.user32.GetWindowLongW(ctypes.c_void_p(hwnd),
                                                     GWL_EXSTYLE) & 0xFFFFFFFF
        owner = ctypes.windll.user32.GetWindow(ctypes.c_void_p(hwnd), GW_OWNER)
        self.assertTrue(exstyle & WS_EX_TOOLWINDOW,
                        f"not a tool window (exstyle 0x{exstyle:08X}, "
                        f"owner {owner})")
        self.assertFalse(exstyle & WS_EX_APPWINDOW,
                         f"WS_EX_APPWINDOW forces a taskbar button back on "
                         f"(exstyle 0x{exstyle:08X})")


class LimitRowTest(unittest.TestCase):
    """The limit rows carry the account's percentage, not this machine's dollars.

    The two figures describe different things -- the percentage the whole account,
    the dollars this installation -- and side by side on one row they read as one
    claim, which invites dividing one by the other. That division is exactly what
    the old calibrated percentage got wrong. The dollars moved to the tooltip.
    """

    def setUp(self):
        # Fixed instants rather than the wall clock, so nothing here depends on
        # when the suite runs: a reset an hour out is open, one in the past is
        # not, and one two days out is on another date. Built through astimezone()
        # so the assertions do not depend on the machine's zone either -- the row
        # renders in local time, as /usage does.
        self.now = datetime(2026, 8, 13, 17, 0, 0).astimezone().timestamp()
        self.iso = datetime(2026, 8, 13, 18, 30, 0).astimezone().isoformat()
        self.other_day = datetime(2026, 8, 15, 2, 59, 0).astimezone().isoformat()

    def row(self, window, limit):
        return widget.window_row(window, limit, now=self.now)

    def test_a_row_with_no_account_figure_shows_dollars_and_claims_no_colour(self):
        self.assertEqual(widget.window_row({"usd": 6.4}, None),
                         ("$6.40", "muted"))

    def test_a_row_with_an_account_figure_shows_the_percentage_alone(self):
        self.assertEqual(
            widget.window_row({"usd": 6.4}, {"pct": 31, "severity": "normal"}),
            ("≈31 %", "green"))

    def test_the_percentage_is_always_marked_as_approximate(self):
        # Unconditional: the figure is only re-fetched at session start and on
        # /usage, and usage within a window only grows, so even a seconds-old one
        # has had time to rise. A marker that came and went would imply the
        # unmarked form is exact, and it never is.
        text = widget.window_row({"usd": 1.0}, {"pct": 5, "severity": "normal"})[0]
        self.assertTrue(text.startswith("≈"), text)

    def test_the_reset_time_rides_with_the_percentage(self):
        self.assertEqual(
            self.row({"usd": 51.04},
                     {"pct": 6, "severity": "normal", "resets_at": self.iso})[0],
            "≈6 % · 18:30")

    def test_a_reset_on_another_date_names_the_day(self):
        # The weekly window resets days out, where a bare `02:59` reads as
        # tonight. The 5-hour row is same-day and keeps the short form.
        expected = datetime(2026, 8, 15, 2, 59, 0).strftime("%a 02:59")
        self.assertEqual(
            self.row({"usd": 1.0},
                     {"pct": 17, "severity": "normal",
                      "resets_at": self.other_day})[0],
            f"≈17 % · {expected}")

    def test_a_window_that_has_already_reset_is_withdrawn(self):
        # The figure describes a window that no longer exists, so no bound
        # survives it -- and the cache's age cannot detect that.
        past = datetime.fromtimestamp(1000.0, timezone.utc).isoformat()
        self.assertEqual(
            widget.window_row({"usd": 6.4},
                              {"pct": 31, "severity": "normal",
                               "resets_at": past}, now=2000.0),
            ("$6.40", "muted"))

    def test_a_row_with_no_reset_time_never_expires(self):
        self.assertEqual(
            widget.window_row({"usd": 6.4},
                              {"pct": 31, "severity": "normal",
                               "resets_at": None}, now=2000.0)[0],
            "≈31 %")

    def test_an_unparseable_reset_time_is_dropped_rather_than_shown_raw(self):
        self.assertEqual(
            widget.window_row({"usd": 1.0},
                              {"pct": 5, "severity": "normal",
                               "resets_at": "not a time"})[0],
            "≈5 %")

    def test_a_percentage_that_is_not_a_whole_number_is_treated_as_absent(self):
        for pct in (None, "31", True, 31.5):
            self.assertEqual(
                widget.window_row({"usd": 6.4}, {"pct": pct}),
                ("$6.40", "muted"), pct)

    def test_a_missing_dollar_figure_does_not_read_as_zero(self):
        # An em dash rather than $0.00: no recorded spend and no spend are
        # different claims, and the second one is a lie the panel must not tell.
        self.assertEqual(widget.window_row({}, None)[0], "—")


class SeverityTest(unittest.TestCase):
    """Colour comes from the server, which knows where the thresholds are."""

    def test_the_servers_severity_decides_the_colour(self):
        for severity, expected in (("normal", "green"), ("warning", "amber"),
                                   ("critical", "red")):
            self.assertEqual(widget.severity_class(severity, 5), expected)

    def test_an_unknown_severity_falls_back_to_the_percentage(self):
        # A word this panel has never seen must not be what paints a row at 95 %
        # as safe.
        self.assertEqual(widget.severity_class("brand-new-word", 95), "red")

    def test_no_severity_falls_back_across_both_thresholds(self):
        # The older cache shape carries no severity at all.
        self.assertEqual(widget.severity_class(None, 59), "green")
        self.assertEqual(widget.severity_class(None, 60), "amber")
        self.assertEqual(widget.severity_class(None, 84), "amber")
        self.assertEqual(widget.severity_class(None, 85), "red")


class LimitTooltipTest(unittest.TestCase):
    """Where the dollar figure went, and the only place the scopes are named."""

    def setUp(self):
        # Same fixed instants as LimitRowTest, and for the same reason.
        self.now = datetime(2026, 8, 13, 17, 0, 0).astimezone().timestamp()
        self.iso = datetime(2026, 8, 13, 18, 30, 0).astimezone().isoformat()

    def test_the_tooltip_names_the_scope_of_each_figure(self):
        text = widget.window_tooltip(
            {"usd": 71.46},
            {"pct": 20, "severity": "normal", "resets_at": self.iso}, 1800.0,
            now=self.now)
        self.assertIn("$71.46 on this machine", text)
        self.assertIn("at least 20 %", text)
        self.assertIn("resets 18:30", text)
        self.assertIn("30 min", text)

    def test_the_tooltip_says_what_refreshes_the_figure(self):
        # The one thing a reader can act on: nothing else moves it.
        text = widget.window_tooltip(
            {"usd": 71.46}, {"pct": 20, "severity": "normal"}, 1800.0,
            now=self.now)
        self.assertIn("/usage", text)

    def test_the_tooltip_says_so_when_there_is_no_account_figure(self):
        text = widget.window_tooltip({"usd": 71.46}, None, None)
        self.assertIn("$71.46 on this machine", text)
        self.assertIn("no account figure", text)

    def test_the_tooltip_says_so_when_the_window_has_reset(self):
        past = datetime.fromtimestamp(1000.0, timezone.utc).isoformat()
        text = widget.window_tooltip({"usd": 71.46},
                                     {"pct": 20, "resets_at": past}, 60.0,
                                     now=2000.0)
        self.assertIn("has reset", text)
        self.assertNotIn("at least 20 %", text)


class TurnTextTest(unittest.TestCase):
    """The `last turn` row, which counts up from zero on every turn.

    A delta, not a running total, so it is the one row whose text has to tell
    `$0.00 on the way up` apart from `no turn recorded` -- the same em dash
    distinction the window rows make, but here it lasts only as long as the
    first frame of a roll.
    """

    def test_a_turn_carries_its_sign(self):
        self.assertEqual(widget.turn_text(4.2), "+$4.20")

    def test_no_turn_recorded_is_a_dash(self):
        self.assertEqual(widget.turn_text(0.0), "—")
        self.assertEqual(widget.turn_text(None), "—")

    def test_the_start_of_a_count_is_zero_dollars_not_a_dash(self):
        # The first frame of a roll draws exactly 0.0. Falling back to the dash
        # there would blank the row for a frame before the digits moved, which
        # is the blink this animation exists to avoid.
        self.assertEqual(widget.turn_text(0.0, moving=True), "+$0.00")

    def test_a_row_left_at_zero_by_a_finished_roll_is_still_a_dash(self):
        self.assertEqual(widget.turn_text(0.0, moving=False), "—")


class RollStyleTest(unittest.TestCase):
    """A roll changes the figure and nothing else about how the row looks.

    The digits used to be drawn dimmer while they moved, as a stand-in for
    motion blur; on screen that read as the row blinking. The classes and the
    CSS rules behind it are gone, and this is what keeps them from creeping
    back in -- a stray `label.roll1` would be invisible in review and obvious
    on the panel.
    """

    def test_no_dimming_classes_survive_in_the_stylesheet(self):
        css = widget.CSS.decode("utf-8")
        self.assertNotIn("roll1", css)
        self.assertNotIn("roll2", css)
        self.assertNotIn("opacity", css)

    def test_a_frame_carries_a_value_and_nothing_else(self):
        # draw_row takes the key alone now, and reads the figure back off the
        # roll; a frame handing over a second field would mean it had grown a
        # visual channel again.
        rolling = roll.Roll(min_delta=0.01)
        rolling.retarget({"today": 10.0})
        rolling.retarget({"today": 48.0})
        self.assertEqual(rolling.frame(0.5)["today"], roll.value_at(10.0, 48.0, 0.5))


@unittest.skipUnless(HAS_DISPLAY, "no display")
class MenuColourTest(unittest.TestCase):
    """Every caption in the right-click menu is black, whatever its state.

    The panel's stylesheet is added for the whole screen rather than for the
    panel, and the menu is a top-level of its own, so `label { color: #d8d8dc }`
    -- a pale grey picked to sit on the panel's near-black -- landed on the
    menu's light background too. Every item there is live, and grey text on
    white is how a menu says the opposite.

    Read off real widgets rather than out of `widget.CSS`, unlike the other
    stylesheet tests here: those guard rules that must not exist, which text can
    settle, and this guards an outcome. A rule that reads correctly can still
    lose the cascade to a theme, and the grep would stay green while the menu
    went back to grey.
    """

    def setUp(self):
        self.window = widget.CostMeter()  # puts the stylesheet on the screen
        # No main loop here, so the close handler would turn teardown into a
        # Gtk-CRITICAL.
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)
        self.menu = self.window.build_menu()
        self.menu.show_all()
        self.addCleanup(self.menu.destroy)

    def test_no_caption_is_grey_in_any_state(self):
        # PRELIGHT and INSENSITIVE as well as NORMAL: "always" is the whole
        # request, and the hover state is where a theme gets its say.
        states = (Gtk.StateFlags.NORMAL, Gtk.StateFlags.PRELIGHT,
                  Gtk.StateFlags.INSENSITIVE)
        items = self.menu.get_children()
        self.assertEqual(len(items), len(self.window.menu_entries()))
        for item in items:
            label = item.get_child()
            context = label.get_style_context()
            for state in states:
                colour = context.get_color(state)
                self.assertEqual(
                    (colour.red, colour.green, colour.blue), (0.0, 0.0, 0.0),
                    f"{label.get_text()!r} is not black in {state}")

    def test_the_panel_itself_keeps_its_pale_text(self):
        """The other direction of the same fix.

        Black belongs to the menu alone. Reaching the panel with it would put
        near-black text on the panel's near-black background and lose the rows
        entirely -- a far louder failure than the one being fixed.
        """
        colour = self.window.today.get_style_context().get_color(
            Gtk.StateFlags.NORMAL)
        self.assertNotEqual((colour.red, colour.green, colour.blue),
                            (0.0, 0.0, 0.0))


class ScaleSizeTest(unittest.TestCase):
    """One number sizes the whole panel.

    The panel is a fixed set of rows, so widening the frame on its own would buy
    nothing but blank space around numbers that stayed exactly as small. Scale
    1.0 has to reproduce the sizes the panel shipped with, or upgrading would
    silently resize a panel nobody asked to resize.
    """

    def test_scale_one_is_the_size_the_panel_has_always_been(self):
        self.assertEqual(widget.font_px(1.0), 11)
        self.assertEqual(widget.warn_px(1.0), 10)
        self.assertEqual(widget.width_for_scale(1.0), widget.WIDTH)

    def test_every_size_follows_the_scale(self):
        self.assertEqual(widget.font_px(2.0), 22)
        self.assertEqual(widget.warn_px(2.0), 20)
        self.assertEqual(widget.width_for_scale(2.0), widget.WIDTH * 2)

    def test_the_warning_row_stays_smaller_than_the_value_rows(self):
        # It is a footnote and has to keep reading as one. At the smallest scale
        # the two sizes are close enough that careless rounding closes the gap.
        for scale in (widget.MIN_SCALE, 1.0, widget.MAX_SCALE):
            self.assertLess(widget.warn_px(scale), widget.font_px(scale),
                            f"at scale {scale}")

    def test_the_stylesheet_names_no_font_size(self):
        """Font sizes must not come from the CSS, however natural that looks.

        Reloading a CssProvider already on the screen re-lays out nothing: the
        style context reports the new size, `style-updated` never fires, and
        every existing label keeps the layout it built at the old one. A
        font-size rule here would set the size at startup, ignore every resize
        after it, and look convincingly like it worked.
        """
        self.assertNotIn("font-size", widget.CSS.decode("utf-8"))


class ResizeZoneTest(unittest.TestCase):
    """Which part of an undecorated window is a resize handle.

    There is no frame drawn by anybody, so this band is the only handle the
    panel has. It has to stay off the middle, where the same button drag moves
    the window: a grab zone that swallowed an intended move would make the panel
    feel stuck.
    """

    # A panel roughly the shape of the real one.
    SIZE = (240, 120)

    def zone(self, x, y):
        return widget.resize_zone(x, y, *self.SIZE)

    def test_the_body_of_the_panel_is_not_a_handle(self):
        self.assertIsNone(self.zone(120, 60))

    def test_the_side_edges_are_handles(self):
        self.assertEqual(self.zone(1, 60), "west")
        self.assertEqual(self.zone(238, 60), "east")

    def test_the_top_and_bottom_edges_are_handles(self):
        # A vertical pull cannot set the height -- the rows are as tall as the
        # font makes them -- but it can say "bigger", which is the one number a
        # resize here has to produce anyway.
        self.assertEqual(self.zone(120, 1), "north")
        self.assertEqual(self.zone(120, 118), "south")

    def test_the_corners_are_handles_too(self):
        self.assertEqual(self.zone(1, 1), "north_west")
        self.assertEqual(self.zone(238, 1), "north_east")
        self.assertEqual(self.zone(1, 118), "south_west")
        self.assertEqual(self.zone(238, 118), "south_east")

    def test_a_corner_reaches_further_along_the_side_than_the_edge_is_wide(self):
        # An EDGE-square corner is too small to hit, so it claims a longer
        # stretch of both sides, as every real window frame does.
        self.assertEqual(self.zone(1, widget.EDGE + 1), "north_west")
        self.assertEqual(self.zone(widget.EDGE + 1, 1), "north_west")

    def test_the_pure_side_band_starts_where_the_corner_stops_reaching(self):
        self.assertEqual(self.zone(1, widget.CORNER), "west")
        self.assertEqual(self.zone(widget.CORNER, 1), "north")

    def test_the_band_is_wide_enough_to_find_without_looking(self):
        # The whole point of the width: an undecorated window offers no frame, so
        # anything the pointer misses is not a handle at all.
        self.assertEqual(self.zone(widget.EDGE - 1, 60), "west")
        self.assertEqual(self.zone(120, widget.EDGE - 1), "north")
        self.assertIsNone(self.zone(widget.EDGE, 60))
        self.assertIsNone(self.zone(120, widget.EDGE))


class ResizeDragTest(unittest.TestCase):
    """What a drag of so many pixels does to the scale and to the window's origin.

    Pure arithmetic, deliberately: the pointer grab and the CSS reload around it
    need a display, and none of the decisions that can be wrong do.
    """

    def test_dragging_the_right_edge_outwards_grows_the_panel(self):
        self.assertEqual(widget.drag_scale("east", 1.0, widget.WIDTH, 0), 2.0)

    def test_dragging_the_left_edge_outwards_grows_it_by_the_same_amount(self):
        # Outwards is leftwards on that side, so the sign of dx flips.
        self.assertEqual(widget.drag_scale("west", 1.0, -widget.WIDTH, 0), 2.0)

    def test_dragging_the_bottom_edge_down_grows_the_panel(self):
        self.assertEqual(widget.drag_scale("south", 1.0, 0, widget.WIDTH), 2.0)

    def test_dragging_the_top_edge_up_grows_it_by_the_same_amount(self):
        self.assertEqual(widget.drag_scale("north", 1.0, 0, -widget.WIDTH), 2.0)

    def test_a_pixel_means_the_same_thing_on_either_axis(self):
        # One divisor for both, so the identical gesture cannot mean two
        # different amounts depending on which way it is pulled.
        self.assertEqual(widget.drag_scale("east", 1.0, 60, 0),
                         widget.drag_scale("south", 1.0, 0, 60))

    def test_a_side_handle_ignores_the_axis_it_does_not_own(self):
        self.assertEqual(widget.drag_scale("east", 1.0, 0, 500), 1.0)
        self.assertEqual(widget.drag_scale("south", 1.0, 500, 0), 1.0)

    def test_a_corner_adds_both_components(self):
        # Which is why a diagonal pull grows about twice as fast as a straight
        # one, and why a corner dragged straight down does something at all.
        half = widget.WIDTH / 2
        self.assertEqual(widget.drag_scale("south_east", 1.0, half, half), 2.0)
        self.assertEqual(widget.drag_scale("north_west", 1.0, -half, -half), 2.0)
        self.assertEqual(widget.drag_scale("north_east", 1.0, 0, -widget.WIDTH), 2.0)

    def test_dragging_inwards_shrinks_it(self):
        self.assertEqual(widget.drag_scale("east", 2.0, -widget.WIDTH, 0), 1.0)

    def test_a_runaway_drag_stops_at_the_limits(self):
        self.assertEqual(widget.drag_scale("east", 1.0, 100_000, 0),
                         widget.MAX_SCALE)
        self.assertEqual(widget.drag_scale("east", 1.0, -100_000, 0),
                         widget.MIN_SCALE)

    # The sample taken when the drag began: a 240x120 window at (100, 50), so
    # its right edge is at 340 and its bottom edge at 170.
    START = {"x_window": 100, "y_window": 50, "width": 240, "height": 120}

    def test_dragging_the_left_edge_leaves_the_right_edge_where_it_was(self):
        # A 300 px window has to start at 40 to keep its right edge at 340.
        self.assertEqual(widget.drag_origin("west", self.START, 300, 120),
                         (40, 50))

    def test_dragging_the_top_edge_leaves_the_bottom_edge_where_it_was(self):
        # A 150 px window has to start at 20 to keep its bottom edge at 170.
        self.assertEqual(widget.drag_origin("north", self.START, 240, 150),
                         (100, 20))

    def test_dragging_a_top_left_corner_holds_both_far_edges(self):
        self.assertEqual(widget.drag_origin("north_west", self.START, 300, 150),
                         (40, 20))

    def test_the_far_edges_are_the_ones_the_window_already_grows_from(self):
        # East and south keep the origin, so there is nothing to correct.
        self.assertEqual(widget.drag_origin("south_east", self.START, 300, 150),
                         (100, 50))

    def test_dragging_the_right_edge_leaves_the_left_edge_where_it_was(self):
        # And leaves y alone even though the panel got taller: an east handle
        # owns neither far edge.
        self.assertEqual(widget.drag_origin("east", self.START, 300, 150),
                         (100, 50))


class BillingRowTest(unittest.TestCase):
    """The row that says which of the panel's figures are money you owe.

    Everything above it is a dollar amount whose meaning depends on this: on a
    seat they are notional, and on API billing they are a bill. That is why an
    unanswered question shows as a dash rather than defaulting to either one --
    guessing here would misrepresent every other row on the panel.
    """

    def test_a_seat_names_its_plan(self):
        self.assertEqual(
            widget.billing_text({"mode": "seat", "label": "team · max 5x"}),
            "team · max 5x")

    def test_api_billing_says_so(self):
        self.assertEqual(widget.billing_text({"mode": "api", "label": "API"}),
                         "API")

    def test_an_unknown_mode_is_a_dash(self):
        self.assertEqual(widget.billing_text({"mode": "unknown", "label": None}),
                         "—")

    def test_a_state_written_before_this_existed_is_a_dash(self):
        # Older state.json files carry no billing key at all, and the mode is
        # not in the transcripts, so it cannot be filled in after the fact.
        self.assertEqual(widget.billing_text(None), "—")
        self.assertEqual(widget.billing_text({}), "—")

    def test_a_mode_with_no_label_still_says_which_mode(self):
        # A login naming neither a subscription nor a tier: the plan is unknown
        # but the seat is not, and "seat" is the more useful half.
        self.assertEqual(widget.billing_text({"mode": "seat", "label": None}),
                         "seat")


class StubEvent:
    """The handful of fields the press and motion handlers actually read.

    A real Gdk.EventButton cannot be built from Python without a display and a
    window to aim it at, and none of what is under test here needs one: the
    handlers read coordinates and hand them to arithmetic that is already
    covered above. What this pins down is the wiring between the two.
    """

    def __init__(self, button=1, x=0, y=0, x_root=0, y_root=0):
        self.button = button
        self.x = x
        self.y = y
        self.x_root = x_root
        self.y_root = y_root
        self.time = 0


@unittest.skipUnless(HAS_DISPLAY, "no display")
class ResizeWiringTest(unittest.TestCase):
    """Pressing an edge starts a resize, and moving from there rescales.

    Both halves are short enough to look obviously right and fail silently: a
    press that fell through to begin_move_drag would leave the panel unresizable
    while every arithmetic test above stayed green.
    """

    def setUp(self):
        self.window = widget.CostMeter()
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)
        self.addCleanup(self.window.update_config,
                        lambda c: c.pop("widget_scale", None))
        self.window.apply_scale(1.0)
        self.width = self.window.get_size().width

    # This panel is never shown, so GTK never allocates it and get_size() reports
    # the height apply_scale asked for -- 1, which the rows only clamp up once
    # there is an allocation. Every y is therefore within CORNER of the bottom,
    # which makes a side press land on a corner rather than a pure edge. Only the
    # top band is unambiguous here, so that is the one the vertical case uses.
    # Both zones exercise the same wiring; the arithmetic per zone is covered
    # above without a display.

    def press_right_edge(self):
        # Lands on south_east, for the reason above. Its horizontal half is what
        # matters: the drag below moves in x only.
        self.window.on_click(None, StubEvent(x=self.width - 1, y=50,
                                             x_root=500, y_root=500))

    def press_top_edge(self):
        self.window.on_click(None, StubEvent(x=self.width // 2, y=1,
                                             x_root=500, y_root=500))

    def test_pressing_an_edge_starts_a_resize(self):
        self.press_right_edge()
        self.assertIsNotNone(self.window._resize)

    def test_pressing_the_top_edge_starts_one_too(self):
        # The half of this that arithmetic cannot catch: a press that fell through
        # to begin_move_drag would leave the new handles dead while every zone and
        # scale test stayed green.
        self.press_top_edge()
        self.assertEqual((self.window._resize or {}).get("zone"), "north")

    def test_a_vertical_drag_rescales_the_panel(self):
        self.press_top_edge()
        # Upwards on the top edge is outwards, so the panel grows.
        self.window.on_motion(None, StubEvent(x_root=500,
                                              y_root=500 - widget.WIDTH // 2))
        self.assertAlmostEqual(self.window.scale, 1.5)

    def test_moving_after_that_press_rescales_the_panel(self):
        self.press_right_edge()
        # y_root held at the press value, so this drag is horizontal only.
        self.window.on_motion(None, StubEvent(x_root=500 + widget.WIDTH // 2,
                                              y_root=500))
        self.assertAlmostEqual(self.window.scale, 1.5)

    def test_releasing_ends_the_drag_and_saves_the_size(self):
        self.press_right_edge()
        self.window.on_motion(None, StubEvent(x_root=500 + widget.WIDTH // 2,
                                              y_root=500))
        self.window.on_release(None, StubEvent())
        self.assertIsNone(self.window._resize)
        config = store.read_json(paths.config_path(), default={}) or {}
        self.assertAlmostEqual(config.get("widget_scale"), 1.5)

    def test_a_move_with_no_drag_in_progress_leaves_the_scale_alone(self):
        # Plain pointer motion across the panel only updates the cursor.
        self.window.on_motion(None, StubEvent(x=self.width // 2, y=50,
                                              x_root=900))
        self.assertEqual(self.window.scale, 1.0)


@unittest.skipUnless(HAS_DISPLAY, "no display")
class BillingRowOnThePanelTest(TempHome):
    """The row is built, filled from state.json, and greys out with the rest.

    Muted alongside the figures because it comes from the same snapshot: if the
    hook has been dead for a day, the billing mode it recorded is exactly as old
    as the dollar amounts above it, and a confident-looking `team · max 5x`
    beside five greyed-out rows would claim a freshness it does not have.
    """

    def setUp(self):
        # TempHome first, and it is load-bearing rather than tidiness: draw()
        # writes state.json, and without the redirect that is the user's real
        # data/state.json -- which this class did clobber when run as
        # `python -m unittest tests.test_widget` instead of through run_tests.py.
        super().setUp()
        self.window = widget.CostMeter()
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)

    def state(self, **extra):
        return {"updated_at": datetime.now().astimezone().isoformat(),
                "last_turn_usd": 0.0,
                "session": {"id": "s1", "usd": 1.0},
                "today_usd": 1.0,
                "window_5h": {"usd": 1.0},
                "window_7d": {"usd": 1.0},
                "limits": None,
                "unknown_models": [], **extra}

    def draw(self, state):
        store.write_json_atomic(paths.state_path(), state)
        self.window.refresh()

    def test_the_row_shows_what_the_state_recorded(self):
        self.draw(self.state(billing={"mode": "seat", "label": "team · max 5x"}))
        self.assertEqual(self.window.billing.get_text(), "team · max 5x")

    def test_a_state_with_no_billing_key_leaves_a_dash(self):
        self.draw(self.state())
        self.assertEqual(self.window.billing.get_text(), "—")

    def test_the_row_greys_out_when_the_figures_go_stale(self):
        old = datetime.fromtimestamp(time.time() - 86400).astimezone().isoformat()
        self.draw(self.state(updated_at=old,
                             billing={"mode": "api", "label": "API"}))
        self.assertTrue(
            self.window.billing.get_style_context().has_class("muted"))

    def test_a_fresh_row_is_not_greyed_out(self):
        self.draw(self.state(billing={"mode": "api", "label": "API"}))
        self.assertFalse(
            self.window.billing.get_style_context().has_class("muted"))

    def test_no_two_rows_are_attached_to_the_same_grid_row(self):
        """The warning row shared row 8 with `billing` and drew on top of it.

        Checked across the whole grid rather than for that one pair: the row
        indices in __init__ are literal, so the same collision is one inserted
        row away from happening again anywhere on the panel. Column 0 and
        column 1 of one row legitimately share a row, so the seats counted are
        (column, row) cells, not rows.
        """
        grid = self.window.grid
        seats = {}
        for child in grid.get_children():
            left = grid.child_get_property(child, "left-attach")
            top = grid.child_get_property(child, "top-attach")
            for column in range(left, left + grid.child_get_property(child, "width")):
                for row in range(top, top + grid.child_get_property(child, "height")):
                    self.assertNotIn((column, row), seats,
                                     f"{child} overlaps {seats.get((column, row))}")
                    seats[(column, row)] = child


@unittest.skipUnless(HAS_DISPLAY, "no display")
class LimitRowsOnThePanelTest(TempHome):
    """draw_limits, through a real panel: text, colour and the tooltip.

    The row text is covered by LimitRowTest without GTK; what this adds is the
    wiring -- that refresh() reaches draw_limits at all, that the tooltip is
    actually attached to the label rather than merely computed, and that the
    account figures are read from the right place. None of that is visible in the
    selftest render, which has no tooltip to photograph.

    The figures go into the stand-in for ~/.claude.json rather than into
    state.json, because that is where the panel now reads them from -- state.json
    still carries a copy, written by the hook, and the panel deliberately ignores
    it. test_state_files_copy_of_the_figures_is_ignored holds that line.
    """

    ACCOUNT = "acct-1"

    def setUp(self):
        super().setUp()
        self.window = widget.CostMeter()
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)

    def state(self, limits=None, stale=False):
        written = time.time() - (86400 if stale else 0)
        return {"updated_at": datetime.fromtimestamp(written).astimezone().isoformat(),
                "last_turn_usd": 0.0,
                "session": {"id": "s1", "usd": 1.0},
                "today_usd": 1.0,
                "window_5h": {"usd": 54.73},
                "window_7d": {"usd": 767.24},
                "limits": limits,
                "unknown_models": []}

    def draw(self, figures, state_limits=None, age_s=681.2, stale=False):
        """Put `figures` in the cache, a state.json beside it, and repaint.

        `figures` is the `limits` array Claude Code caches, or None for a machine
        with no usable cache at all. `stale` backdates state.json a day, which is
        the dead-hook case: the dollars stop being current, the cache does not.
        """
        if figures is None:
            self.write_claude_config({})
        else:
            self.write_claude_config({
                "oauthAccount": {"accountUuid": self.ACCOUNT},
                "cachedUsageUtilization": {
                    "accountUuid": self.ACCOUNT,
                    "fetchedAtMs": (time.time() - age_s) * 1000.0,
                    "utilization": {"limits": figures},
                },
            })
        store.write_json_atomic(paths.state_path(),
                                self.state(state_limits, stale=stale))
        self.window.refresh()

    def figures(self, pct_5h=12, pct_week=17, resets_at=None):
        return [{"kind": "session", "percent": pct_5h, "severity": "normal",
                 "resets_at": resets_at, "scope": None, "is_active": True},
                {"kind": "weekly_all", "percent": pct_week,
                 "severity": "critical", "resets_at": None, "scope": None,
                 "is_active": True}]

    def test_each_row_shows_its_own_limit(self):
        self.draw(self.figures())
        self.assertEqual(self.window.window_5h.get_text(), "≈12 %")
        self.assertEqual(self.window.window_7d.get_text(), "≈17 %")

    def test_the_figures_come_from_the_cache_not_from_state_json(self):
        # The gap this closes: Claude Code refreshes its cache on its own
        # schedule, and until the next turn the hook has not copied the new
        # figure into state.json. The panel must show the cache's 12 %, not the
        # copy's 99 %.
        self.draw(self.figures(),
                  state_limits={"age_s": 0.0, "rows": {
                      "session": {"pct": 99, "severity": "critical",
                                  "resets_at": None, "scope": None}}})
        self.assertEqual(self.window.window_5h.get_text(), "≈12 %")

    def test_state_files_copy_of_the_figures_is_ignored_when_the_cache_is_gone(self):
        # No fallback on purpose: utilization.read() returns None exactly when
        # the cache cannot be trusted, and state.json's copy came from that same
        # file -- so it is no more trustworthy than what was just rejected.
        self.draw(None,
                  state_limits={"age_s": 0.0, "rows": {
                      "session": {"pct": 99, "severity": "critical",
                                  "resets_at": None, "scope": None}}})
        self.assertEqual(self.window.window_5h.get_text(), "$54.73")

    def test_the_colour_comes_from_that_rows_severity(self):
        self.draw(self.figures())
        self.assertTrue(
            self.window.window_5h.get_style_context().has_class("green"))
        self.assertTrue(
            self.window.window_7d.get_style_context().has_class("red"))

    def test_the_tooltip_carries_the_dollars_that_left_the_row(self):
        self.draw(self.figures())
        tooltip = self.window.window_5h.get_tooltip_text()
        self.assertIn("$54.73 on this machine", tooltip)
        self.assertIn("at least 12 %", tooltip)
        self.assertIn("/usage", tooltip)

    def test_the_tooltips_age_is_the_caches_own_age(self):
        # The age travels with the figures, so reading them live has to have
        # brought a live age with it rather than whatever the hook last recorded.
        self.draw(self.figures(), age_s=7200.0)
        self.assertIn("figure 2 h 0 min old",
                      self.window.window_5h.get_tooltip_text())

    def test_without_account_figures_the_rows_fall_back_to_dollars(self):
        self.draw(None)
        self.assertEqual(self.window.window_5h.get_text(), "$54.73")
        self.assertTrue(
            self.window.window_5h.get_style_context().has_class("muted"))

    def test_the_machine_row_carries_the_weekly_dollars(self):
        # The same seven days as the percentage above it, measured the other way:
        # what this installation put into the account's week.
        self.draw(self.figures())
        self.assertEqual(self.window.week_local.get_text(), "$767.24")

    def test_the_machine_row_is_a_dollar_row_and_mutes_with_them(self):
        # It is not a limit row: no percentage, no colour, and it greys out with
        # the other measured figures rather than keeping a confident green.
        self.draw(self.figures())
        for name in widget.LIMIT_CLASSES:
            self.assertFalse(
                self.window.week_local.get_style_context().has_class(name), name)
        self.assertIn(self.window.week_local, self.window.usd_values)

    def test_a_stale_state_file_does_not_grey_out_the_percentages(self):
        # A dead tally hook makes the dollars old. It does not touch the account
        # figures -- those come from Claude Code's cache, re-read on every poll --
        # so greying them would claim an age they have not got. What does mute
        # them is having no figure at all, which the next test covers.
        self.draw(self.figures(), stale=True)
        self.assertTrue(
            self.window.window_5h.get_style_context().has_class("green"))
        self.assertFalse(
            self.window.window_5h.get_style_context().has_class("muted"))
        # The dollar rows beside them do grey out: that is what went stale.
        self.assertTrue(
            self.window.week_local.get_style_context().has_class("muted"))

    def test_a_limit_row_with_no_figure_is_muted_even_when_nothing_is_stale(self):
        # It is showing state.json's dollars at that point, so it mutes for the
        # same reason the dollar rows do -- draw_limits marks it, not set_stale.
        self.draw(None)
        self.assertTrue(
            self.window.window_5h.get_style_context().has_class("muted"))

    def test_a_window_that_has_reset_is_withdrawn_without_a_new_state_file(self):
        # The case that makes draw_limits reachable from the staleness poll: the
        # block turns over while nothing is writing state.json.
        past = datetime.fromtimestamp(time.time() - 60).astimezone().isoformat()
        self.draw(self.figures(resets_at=past))
        self.assertEqual(self.window.window_5h.get_text(), "$54.73")


@unittest.skipUnless(HAS_DISPLAY, "no display")
class LimitPollTest(TempHome):
    """Two independent things repaint the rows when only the cache has changed.

    Every other test here drives refresh() by hand, which proves what refresh()
    does and nothing about what calls it. These run a real main loop and touch
    nothing but the cache, so whatever moved the row was the wiring under test.
    Each is tested with the other one unable to interfere:

    - the **file monitor**, with the poll left at its full 60 seconds, so a row
      that moves inside the cap cannot have been the timer;
    - the **poll**, with the monitor deliberately dropped, so a row that moves
      cannot have been the monitor.

    Both matter. The monitor is what makes a /usage land on the panel while
    somebody is still looking at the /usage output; the poll is what catches a
    window reaching its `resets_at`, where no file changes at all.

    The loops quit the moment the text moves, so the normal cost is a fraction of
    the cap; the cap is only there so a genuine break fails instead of hanging.
    """

    ACCOUNT = "acct-1"
    CAP_SECONDS = 5

    def run_until_row_moves(self, window, before):
        """Spin a real main loop until the 5h row changes, or the cap runs out."""
        loop = GLib.MainLoop()
        GLib.timeout_add(50, lambda: loop.quit()
                         if window.window_5h.get_text() != before else True)
        GLib.timeout_add_seconds(self.CAP_SECONDS, loop.quit)
        loop.run()

    def build(self):
        window = widget.CostMeter()
        window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(window.destroy)
        return window

    def write_state(self):
        store.write_json_atomic(paths.state_path(), {
            "updated_at": datetime.now().astimezone().isoformat(),
            "last_turn_usd": 0.0, "session": {"id": "s1", "usd": 1.0},
            "today_usd": 1.0, "window_5h": {"usd": 1.0},
            "window_7d": {"usd": 1.0}, "limits": None, "unknown_models": []})

    def write_cache(self, pct):
        store.write_json_atomic(paths.claude_config_path(), {
            "oauthAccount": {"accountUuid": self.ACCOUNT},
            "cachedUsageUtilization": {
                "accountUuid": self.ACCOUNT,
                "fetchedAtMs": time.time() * 1000.0,
                "utilization": {"limits": [
                    {"kind": "session", "percent": pct, "severity": "normal",
                     "resets_at": None, "scope": None, "is_active": True}]},
            },
        })

    def test_the_monitor_puts_a_new_figure_up_without_waiting_for_the_poll(self):
        self.write_state()
        self.write_cache(1)
        window = self.build()
        self.assertEqual(window.window_5h.get_text(), "≈1 %")

        # The poll is left at 60 seconds, which is well past the cap, so it
        # cannot be what moves the row here. state.json is never written again
        # either, so its own monitor is out too.
        self.assertEqual(widget.STALE_POLL_SECONDS, 60)
        state_mtime = paths.state_path().stat().st_mtime
        self.write_cache(5)

        self.run_until_row_moves(window, "≈1 %")
        self.assertEqual(window.window_5h.get_text(), "≈5 %")
        self.assertEqual(paths.state_path().stat().st_mtime, state_mtime)

    def test_the_poll_puts_a_new_figure_up_when_no_monitor_fires(self):
        self.write_state()
        self.write_cache(1)
        # Read at construction, so it has to be patched before the window exists.
        self.enterContext(unittest.mock.patch.object(
            widget, "STALE_POLL_SECONDS", 1))
        window = self.build()
        self.assertEqual(window.window_5h.get_text(), "≈1 %")

        # Dropped rather than never created: a monitor stops delivering once it
        # is cancelled, which leaves the timer as the only way back to refresh().
        # This stands in for the case the poll is really there for -- a window
        # reaching its `resets_at`, where no file changes at all -- because that
        # one cannot be staged inside a five-second cap.
        for monitor in window.monitors:
            monitor.cancel()

        state_mtime = paths.state_path().stat().st_mtime
        self.write_cache(5)

        self.run_until_row_moves(window, "≈1 %")
        self.assertEqual(window.window_5h.get_text(), "≈5 %")
        self.assertEqual(paths.state_path().stat().st_mtime, state_mtime)


class UsageIntervalTest(TempHome):
    """How often the panel asks the server itself, out of a file a user can edit.

    Validated rather than trusted for the reason saved_scale() is: config.json is
    hand-editable, and a nonsense value here would either hammer the endpoint or
    silently stop the polling that the row's freshness now depends on.
    """

    def test_the_default_is_a_minute(self):
        # Measured, not chosen: five seconds earns `429 Retry-After: 196` from the
        # endpoint, and a minute was answered `200` for as long as it was tried.
        self.assertEqual(widget.usage_interval({}), 60)

    def test_a_configured_interval_is_honoured(self):
        self.assertEqual(widget.usage_interval({"usage_poll_seconds": 30}), 30)

    def test_zero_turns_the_fetch_off(self):
        # The one value that has to survive validation unchanged: it is the
        # off switch, and rounding it up to a second would defeat it.
        self.assertEqual(widget.usage_interval({"usage_poll_seconds": 0}), 0)

    def test_a_sub_second_interval_is_rounded_up_rather_than_into_the_off_switch(self):
        self.assertEqual(widget.usage_interval({"usage_poll_seconds": 0.2}), 1)

    def test_nonsense_falls_back_to_the_default(self):
        for value in ("often", None, True, -5, [5]):
            with self.subTest(value=value):
                self.assertEqual(widget.usage_interval({"usage_poll_seconds": value}),
                                 60)

    def test_the_interval_is_read_from_config(self):
        self.write_config({"usage_poll_seconds": 12})
        self.assertEqual(widget.CostMeter.saved_usage_interval(), 12)


@unittest.skipUnless(HAS_DISPLAY, "no display")
class UsageFetchTest(TempHome):
    """The panel's own poll of the server, with the network replaced.

    `usage_api.refresh` is what gets patched rather than the socket underneath it:
    the question these tests ask is about the panel's wiring -- does a timer fire,
    does the answer cross back from the worker thread, does the row repaint -- and
    that is answered without any HTTP at all. What refresh() itself does with a
    body is tests/test_usage_api.py's subject.
    """

    ACCOUNT = "acct-1"
    CAP_SECONDS = 5

    def build(self):
        window = widget.CostMeter()
        window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(window.destroy)
        return window

    def write_state(self):
        store.write_json_atomic(paths.state_path(), {
            "updated_at": datetime.now().astimezone().isoformat(),
            "last_turn_usd": 0.0, "session": {"id": "s1", "usd": 1.0},
            "today_usd": 1.0, "window_5h": {"usd": 1.0},
            "window_7d": {"usd": 1.0}, "limits": None, "unknown_models": []})

    def write_account(self):
        # No cachedUsageUtilization: the row starts with no account figure at all,
        # so a percentage appearing on it can only have come from our own fetch.
        store.write_json_atomic(paths.claude_config_path(),
                                {"oauthAccount": {"accountUuid": self.ACCOUNT}})

    def fake_refresh(self, pct):
        """Stand in for the fetch: writes what a real answer would have written."""
        def refresh(now=None, get=None):
            store.write_json_atomic(paths.usage_path(), {
                "accountUuid": self.ACCOUNT,
                "fetchedAtMs": time.time() * 1000.0,
                "utilization": {"limits": [
                    {"kind": "session", "percent": pct, "severity": "normal",
                     "resets_at": None, "scope": None, "is_active": True}]},
            })
            return True, None
        return refresh

    def run_until_row_moves(self, window, before):
        loop = GLib.MainLoop()
        GLib.timeout_add(50, lambda: loop.quit()
                         if window.window_5h.get_text() != before else True)
        GLib.timeout_add_seconds(self.CAP_SECONDS, loop.quit)
        loop.run()

    def test_a_figure_the_panel_fetched_itself_reaches_the_row(self):
        self.write_state()
        self.write_account()
        # A second rather than the default five, which would land on the cap
        # itself. What is under test is the wiring, not the interval.
        self.write_config({"usage_poll_seconds": 1})
        self.enterContext(unittest.mock.patch.object(
            widget.usage_api, "refresh", self.fake_refresh(7)))
        self.enterContext(unittest.mock.patch.object(
            widget, "STALE_POLL_SECONDS", 3600))  # so the row cannot move by poll
        window = self.build()
        self.assertEqual(window.window_5h.get_text(), "$1.00")

        # Nothing else can be responsible: state.json is never rewritten, the
        # cache holds no figure, and the staleness poll is an hour out.
        self.run_until_row_moves(window, "$1.00")
        self.assertEqual(window.window_5h.get_text(), "≈7 %")

    def test_the_fetch_is_not_started_at_all_when_it_is_switched_off(self):
        self.write_state()
        self.write_account()
        self.write_config({"usage_poll_seconds": 0})
        calls = []
        self.enterContext(unittest.mock.patch.object(
            widget.usage_api, "refresh",
            lambda **kw: (calls.append(1), (True, None))[1]))
        window = self.build()

        loop = GLib.MainLoop()
        GLib.timeout_add_seconds(2, loop.quit)
        loop.run()
        self.assertEqual(calls, [])
        self.assertEqual(window.window_5h.get_text(), "$1.00")

    def test_repeated_failures_back_the_fetch_off_instead_of_retrying_at_speed(self):
        self.write_state()
        self.write_account()
        window = self.build()

        window.usage_fetched(False)
        first = window.usage_wait
        window.usage_fetched(False)
        self.assertGreater(window.usage_wait, first)

    def test_the_wait_the_server_asked_for_beats_the_backoff(self):
        # A 429 names its own pace, and it is longer than any early backoff step.
        # Asking again before it has passed would only earn another 429.
        self.write_state()
        self.write_account()
        window = self.build()

        window.usage_fetched(False, 196.0)
        self.assertGreaterEqual(window.usage_wait, 196.0)

    def test_an_answer_clears_the_backoff(self):
        self.write_state()
        self.write_account()
        window = self.build()

        window.usage_fetched(False)
        self.assertGreater(window.usage_wait, 0.0)
        window.usage_fetched(True)
        self.assertEqual(window.usage_wait, 0.0)


@unittest.skipUnless(HAS_DISPLAY, "no display")
class ScaleMemoryTest(unittest.TestCase):
    """A resized panel opens at the size it was left at.

    The scale shares config.json with the window position and the paused
    auto-launch flag, so it goes through the same locked read-modify-write those
    do; a panel that forgot its size on every restart would make the whole
    feature pointless.
    """

    def setUp(self):
        self.window = widget.CostMeter()
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)
        self.addCleanup(self.window.update_config,
                        lambda c: c.pop("widget_scale", None))

    def test_a_saved_scale_is_what_the_next_panel_opens_at(self):
        self.window.apply_scale(1.8)
        self.window.remember_scale()

        reopened = widget.CostMeter()
        reopened.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(reopened.destroy)
        self.assertEqual(reopened.scale, 1.8)

    def test_resetting_the_size_leaves_no_key_behind(self):
        self.window.apply_scale(1.8)
        self.window.remember_scale()
        self.window.reset_scale()
        config = store.read_json(paths.config_path(), default={}) or {}
        self.assertNotIn("widget_scale", config)
        self.assertEqual(self.window.scale, 1.0)

    def test_the_rows_themselves_grow_with_the_scale(self):
        """The stylesheet reaches the real widgets, not just a string.

        Every other scale test reads the CSS text, which stays green if the
        provider is never reloaded or the grid keeps its old spacings -- the
        panel would then stay exactly the size it was while `scale` claimed
        otherwise. The natural width of the built widget tree is what actually
        answers that, so it is measured here and nowhere else.
        """
        before = self.natural_width_at(1.0)
        after = self.natural_width_at(2.0)
        self.assertGreater(after, before * 1.5,
                           f"rows did not grow: {before} -> {after}")

    def natural_width_at(self, scale):
        """How wide the built widget tree wants to be at `scale`.

        The grid rather than the window, and shown first: GTK reports zero for
        an invisible widget and a placeholder for a toplevel whose children are
        all invisible, so an unshown tree answers the same number at every scale
        and the measurement proves nothing. show_all() on the grid makes the
        rows measurable without mapping a window onto the user's screen.
        """
        self.window.grid.show_all()
        self.window.apply_scale(scale)
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        return self.window.grid.get_preferred_width().natural_width

    def test_saving_a_scale_leaves_the_window_position_alone(self):
        self.window.update_config(
            lambda c: c.__setitem__("widget_position", [7, 9]))
        self.addCleanup(self.window.update_config,
                        lambda c: c.pop("widget_position", None))
        self.window.apply_scale(1.5)
        self.window.remember_scale()
        config = store.read_json(paths.config_path(), default={}) or {}
        self.assertEqual(config.get("widget_position"), [7, 9])



class SecondPanelTest(TempHome):
    """A panel that cannot claim the liveness lock exits before it draws.

    The lock is the only honest answer to "is a panel already up", and the
    launcher cannot ask it atomically: it looks, finds the lock free, and spawns
    a panel that takes seconds -- a nested `pixi run` and GTK startup -- to
    claim it. Claude Desktop fires the SessionStart hook twice per code-mode
    session, close enough together that both invocations looked into that gap
    and both spawned. Two panels then stayed on screen, because a failed claim
    used to be tolerated here.

    So the claim decides, and it is made before anything is built: the loser
    costs a wasted interpreter, never a second window.
    """

    def setUp(self):
        super().setUp()
        # Gtk.main is stubbed for the whole class, including the tests that
        # expect never to reach it: a panel that wrongly kept going would
        # otherwise enter the real main loop and hang the run rather than fail
        # it, which is exactly how this bug looked from the outside.
        for patch in (unittest.mock.patch.object(sys, "argv", ["widget.py"]),
                      unittest.mock.patch.object(widget.Gtk, "main")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_panel_that_loses_the_lock_exits_without_building_a_window(self):
        handle = store.try_acquire(paths.widget_lock_path())
        self.addCleanup(store.release, handle)
        with unittest.mock.patch.object(widget, "CostMeter") as panel:
            self.assertEqual(widget.main(), 0)
        panel.assert_not_called()

    def test_a_panel_that_loses_the_lock_leaves_the_winners_pid_file_alone(self):
        paths.pid_path().write_text("4242\n", encoding="utf-8")
        handle = store.try_acquire(paths.widget_lock_path())
        self.addCleanup(store.release, handle)
        with unittest.mock.patch.object(widget, "CostMeter"):
            widget.main()
        self.assertEqual(paths.pid_path().read_text(encoding="utf-8").strip(),
                         "4242")

    def test_the_panel_that_takes_the_lock_builds_its_window(self):
        with unittest.mock.patch.object(widget, "CostMeter") as panel:
            self.assertEqual(widget.main(), 0)
        panel.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
