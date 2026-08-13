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
import time
import unittest
from datetime import datetime

import widget
from cost_meter import autolaunch, launch, paths, roll, store

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
class AutolaunchToggleTest(unittest.TestCase):
    """The menu item writes the key the SessionStart hook actually reads.

    Both halves of the toggle are a couple of lines of lambda in `show_menu`,
    and getting them wrong fails silently in the worst direction: a menu that
    reads `Resume auto-launch` while every new session keeps opening the panel
    anyway. The panel's config writer is also the one that has to preserve the
    window position and the ceilings sharing that file.
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
    """The 5-hour row names the time its block resets, when it has a percentage.

    The limit is a fixed block, so there is an actual clock time to show, and it
    is the same one /usage prints. Having it beside the percentage is what makes
    a calibration checkable: the percentage is only trustworthy if the panel and
    /usage agree about which block they are describing.

    Without a percentage there is nothing for it to qualify, so it goes: an
    uncalibrated row is reporting one measured fact, not two unrelated ones.
    """

    def setUp(self):
        # Built from a local wall-clock time so the assertion does not depend on
        # the machine's zone: the row renders in local time, as /usage does.
        self.iso = datetime(2026, 8, 11, 19, 4, 0).astimezone().isoformat()

    def test_a_calibrated_row_names_the_reset_time(self):
        self.assertEqual(
            widget.window_row({"usd": 51.04, "pct": 6, "resets_at": self.iso})[0],
            "$51.04 ~6 % · 19:04")

    def test_an_uncalibrated_row_drops_the_reset_time(self):
        # The time qualifies the percentage: it says which block the estimate
        # describes. With no percentage on the row it qualifies nothing, and a
        # bare clock time beside a dollar figure reads as a second, unrelated
        # claim rather than as context for the first.
        self.assertEqual(
            widget.window_row({"usd": 51.04, "pct": None, "resets_at": self.iso}),
            ("$51.04", "muted"))

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


class ScaleSizeTest(unittest.TestCase):
    """One number sizes the whole panel.

    The panel is five fixed rows, so widening the frame on its own would buy
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

    def test_the_corners_are_handles_too(self):
        self.assertEqual(self.zone(1, 1), "north_west")
        self.assertEqual(self.zone(238, 1), "north_east")
        self.assertEqual(self.zone(1, 118), "south_west")
        self.assertEqual(self.zone(238, 118), "south_east")

    def test_a_corner_reaches_further_along_the_side_than_the_edge_is_wide(self):
        # A 6 px square is too small to hit. The corner claims a longer stretch
        # of the side, which is what every real window frame does.
        self.assertEqual(self.zone(1, 12), "north_west")

    def test_a_bare_top_or_bottom_edge_is_not_a_handle(self):
        # Height is a consequence of the content and the scale, never an input:
        # there are five rows and they are as tall as the font. A vertical-only
        # grab would have nothing to change, so it stays a move.
        self.assertIsNone(self.zone(120, 1))
        self.assertIsNone(self.zone(120, 119))


class ResizeDragTest(unittest.TestCase):
    """What a drag of so many pixels does to the scale and to the window's x.

    Pure arithmetic, deliberately: the pointer grab and the CSS reload around it
    need a display, and none of the decisions that can be wrong do.
    """

    def test_dragging_the_right_edge_outwards_grows_the_panel(self):
        self.assertEqual(widget.drag_scale("east", 1.0, widget.WIDTH), 2.0)

    def test_dragging_the_left_edge_outwards_grows_it_by_the_same_amount(self):
        # Outwards is leftwards on that side, so the sign of dx flips.
        self.assertEqual(widget.drag_scale("west", 1.0, -widget.WIDTH), 2.0)
        self.assertEqual(widget.drag_scale("north_west", 1.0, -widget.WIDTH), 2.0)

    def test_dragging_inwards_shrinks_it(self):
        self.assertEqual(widget.drag_scale("east", 2.0, -widget.WIDTH), 1.0)

    def test_a_runaway_drag_stops_at_the_limits(self):
        self.assertEqual(widget.drag_scale("east", 1.0, 100_000), widget.MAX_SCALE)
        self.assertEqual(widget.drag_scale("east", 1.0, -100_000), widget.MIN_SCALE)

    def test_dragging_the_left_edge_leaves_the_right_edge_where_it_was(self):
        # Grabbed at x=100 with a 240 px window, so the right edge is at 340 and
        # a 300 px window has to start at 40 to keep it there.
        self.assertEqual(widget.drag_origin("west", 100, 240, 300), 40)

    def test_dragging_the_right_edge_leaves_the_left_edge_where_it_was(self):
        self.assertEqual(widget.drag_origin("east", 100, 240, 300), 100)


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

    def __init__(self, button=1, x=0, y=0, x_root=0):
        self.button = button
        self.x = x
        self.y = y
        self.x_root = x_root
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

    def press_edge(self):
        # The right edge, at a height clear of both corners.
        self.window.on_click(None, StubEvent(x=self.width - 1, y=50,
                                             x_root=500))

    def test_pressing_an_edge_starts_a_resize(self):
        self.press_edge()
        self.assertIsNotNone(self.window._resize)

    def test_moving_after_that_press_rescales_the_panel(self):
        self.press_edge()
        self.window.on_motion(None, StubEvent(x_root=500 + widget.WIDTH // 2))
        self.assertAlmostEqual(self.window.scale, 1.5)

    def test_releasing_ends_the_drag_and_saves_the_size(self):
        self.press_edge()
        self.window.on_motion(None, StubEvent(x_root=500 + widget.WIDTH // 2))
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
class BillingRowOnThePanelTest(unittest.TestCase):
    """The row is built, filled from state.json, and greys out with the rest.

    Muted alongside the figures because it comes from the same snapshot: if the
    hook has been dead for a day, the billing mode it recorded is exactly as old
    as the dollar amounts above it, and a confident-looking `team · max 5x`
    beside five greyed-out rows would claim a freshness it does not have.
    """

    def setUp(self):
        self.window = widget.CostMeter()
        self.window.disconnect_by_func(Gtk.main_quit)
        self.addCleanup(self.window.destroy)

    def state(self, **extra):
        return {"updated_at": datetime.now().astimezone().isoformat(),
                "last_turn_usd": 0.0,
                "session": {"id": "s1", "usd": 1.0},
                "today_usd": 1.0,
                "window_5h": {"usd": 1.0, "pct": None},
                "window_7d": {"usd": 1.0, "pct": None},
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


@unittest.skipUnless(HAS_DISPLAY, "no display")
class ScaleMemoryTest(unittest.TestCase):
    """A resized panel opens at the size it was left at.

    The scale shares config.json with the ceilings and the window position, so
    it goes through the same locked read-modify-write those do; a panel that
    forgot its size on every restart would make the whole feature pointless.
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


if __name__ == "__main__":
    unittest.main()
