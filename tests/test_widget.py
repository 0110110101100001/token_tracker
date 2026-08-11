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
import unittest
from datetime import datetime

import widget
from cost_meter import launch

HAS_DISPLAY = launch.has_display()

if HAS_DISPLAY:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk

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


class WindowRowTest(unittest.TestCase):
    """A calibrated row carries the dollar figure as well as the percentage.

    It used to show the percentage alone, so calibrating traded the dollar figure
    away: there was no way to see both, and no way back short of hand-editing
    data/config.json.
    """

    def test_an_uncalibrated_row_shows_dollars_and_claims_no_colour(self):
        self.assertEqual(widget.window_row({"usd": 6.4, "pct": None}),
                         ("$6.40", "muted"))

    def test_a_calibrated_row_shows_both(self):
        self.assertEqual(widget.window_row({"usd": 6.4, "pct": 31}),
                         ("$6.40 ~31 %", "green"))

    def test_the_colour_follows_the_percentage_across_both_thresholds(self):
        self.assertEqual(widget.window_row({"usd": 1.0, "pct": 59})[1], "green")
        self.assertEqual(widget.window_row({"usd": 1.0, "pct": 60})[1], "amber")
        self.assertEqual(widget.window_row({"usd": 1.0, "pct": 84})[1], "amber")
        self.assertEqual(widget.window_row({"usd": 1.0, "pct": 85})[1], "red")

    def test_a_missing_dollar_figure_does_not_read_as_zero(self):
        # An em dash rather than $0.00: no recorded spend and no spend are
        # different claims, and the second one is a lie the panel must not tell.
        self.assertEqual(widget.window_row({})[0], "—")


class ResetTimeTest(unittest.TestCase):
    """The 5-hour row names the time its block resets.

    The limit is a fixed block, so there is an actual clock time to show, and it
    is the same one /usage prints. Having it on the row is what makes a
    calibration checkable: the percentage is only trustworthy if the panel and
    /usage agree about which block they are describing.
    """

    def setUp(self):
        # Built from a local wall-clock time so the assertion does not depend on
        # the machine's zone: the row renders in local time, as /usage does.
        self.iso = datetime(2026, 8, 11, 19, 4, 0).astimezone().isoformat()

    def test_a_calibrated_row_names_the_reset_time(self):
        self.assertEqual(
            widget.window_row({"usd": 51.04, "pct": 6, "resets_at": self.iso})[0],
            "$51.04 ~6 % · 19:04")

    def test_an_uncalibrated_row_still_names_the_reset_time(self):
        self.assertEqual(
            widget.window_row({"usd": 51.04, "pct": None, "resets_at": self.iso}),
            ("$51.04 · 19:04", "muted"))

    def test_a_row_with_no_open_block_says_nothing_about_resetting(self):
        # No block is open, so there is no reset time to name and inventing one
        # would be a claim about a limit that is not currently running.
        self.assertEqual(
            widget.window_row({"usd": 0.0, "pct": 0, "resets_at": None})[0],
            "$0.00 ~0 %")

    def test_the_week_row_is_unaffected(self):
        # The weekly cap has no block boundary, so its dict carries no key at all.
        self.assertEqual(widget.window_row({"usd": 805.23, "pct": 40})[0],
                         "$805.23 ~40 %")

    def test_an_unparseable_reset_time_is_dropped_rather_than_shown_raw(self):
        self.assertEqual(
            widget.window_row({"usd": 1.0, "pct": 5, "resets_at": "not a time"})[0],
            "$1.00 ~5 %")


if __name__ == "__main__":
    unittest.main()
