# tests/test_ceilings.py
"""The two ceiling keys, and the one clear path both front ends share."""
import unittest

from support import TempHome

from cost_meter import ceilings


class ClearCeilingsTest(TempHome):
    def test_a_calibrated_ceiling_is_removed_and_reported(self):
        self.write_config({"ceiling_5h_usd": 40.0, "ceiling_7d_usd": 120.0})
        self.assertEqual(ceilings.clear_ceilings(["ceiling_5h_usd"]),
                         ["ceiling_5h_usd"])
        # Only the window asked for: clearing the 5h row must leave the week
        # calibrated, or one flag would quietly undo both calibrations.
        self.assertEqual(self.config(), {"ceiling_7d_usd": 120.0})

    def test_both_can_go_at_once(self):
        self.write_config({"ceiling_5h_usd": 40.0, "ceiling_7d_usd": 120.0})
        self.assertEqual(
            ceilings.clear_ceilings(["ceiling_5h_usd", "ceiling_7d_usd"]),
            ["ceiling_5h_usd", "ceiling_7d_usd"])
        self.assertEqual(self.config(), {})

    def test_clearing_what_was_never_calibrated_is_not_an_error(self):
        self.write_config({"ceiling_7d_usd": 120.0})
        self.assertEqual(ceilings.clear_ceilings(["ceiling_5h_usd"]), [])
        self.assertEqual(self.config(), {"ceiling_7d_usd": 120.0})

    def test_clearing_against_no_config_at_all_is_not_an_error(self):
        self.assertEqual(ceilings.clear_ceilings(["ceiling_5h_usd"]), [])

    def test_the_saved_window_position_survives(self):
        # config.json has three owners: the two ceilings and the panel's window
        # position. A wholesale rewrite rather than a read-modify-write under the
        # lock would drop whichever value this side does not know about.
        self.write_config({"ceiling_5h_usd": 40.0, "widget_position": [100, 200]})
        ceilings.clear_ceilings(["ceiling_5h_usd"])
        self.assertEqual(self.config(), {"widget_position": [100, 200]})


class WordingTest(unittest.TestCase):
    """Neither message may name calibration: a declared ceiling never was."""

    def test_neither_message_mentions_calibration(self):
        self.assertNotIn("calibrat", ceilings.REMOVED)
        self.assertNotIn("calibrat", ceilings.NOT_SET)

    def test_both_messages_name_the_ceiling(self):
        self.assertIn("ceiling", ceilings.REMOVED)
        self.assertIn("ceiling", ceilings.NOT_SET)


class ClearTest(TempHome):
    """`clear` reports per window and refreshes once, whatever it removed."""

    def setUp(self):
        super().setUp()
        self.refreshed = []

    def refresh(self, session_id):
        self.refreshed.append(session_id)

    def test_a_removed_ceiling_is_reported_as_removed(self):
        self.write_config({"ceiling_7d_usd": 2000.0})
        lines = []
        code = ceilings.clear([("week", "ceiling_7d_usd")], self.refresh,
                              report=lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [f"week: {ceilings.REMOVED}"])
        self.assertEqual(self.config(), {})

    def test_an_absent_ceiling_is_reported_as_absent(self):
        lines = []
        ceilings.clear([("week", "ceiling_7d_usd")], self.refresh,
                       report=lines.append)
        self.assertEqual(lines, [f"week: {ceilings.NOT_SET}"])

    def test_the_panel_is_refreshed_exactly_once(self):
        # state.json still carries the percentage this call invalidated, and the
        # panel redraws from the file monitor: without the refresh the row would
        # keep showing it until the next assistant turn.
        self.write_config({"ceiling_5h_usd": 40.0, "ceiling_7d_usd": 2000.0})
        ceilings.clear([("5h window", "ceiling_5h_usd"),
                        ("week", "ceiling_7d_usd")], self.refresh,
                       report=lambda line: None)
        self.assertEqual(self.refreshed, [""])


if __name__ == "__main__":
    unittest.main()
