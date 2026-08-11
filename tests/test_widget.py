"""The panel must stay off the taskbar, on both platforms.

This needs a real GTK window rather than a stub: the question is not what the
panel asks for but what the window manager gives it, and on Windows those two
came apart -- set_skip_taskbar_hint() is silently ignored by GDK's win32
backend, so the panel sat in the taskbar for its whole run while the code read
as though it had opted out.

Skipped where there is no display, like the widget selftest in smoke.py: GTK
cannot create a window at all then, and a headless box has no taskbar to be in.
"""

import ctypes
import os
import unittest

from cost_meter import launch

HAS_DISPLAY = launch.has_display()

if HAS_DISPLAY:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk

    import widget

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


if __name__ == "__main__":
    unittest.main()
