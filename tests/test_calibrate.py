# tests/test_calibrate.py
"""Removing a calibration, which is the only way back to the dollar figures."""
import os
import tempfile
import unittest

import calibrate
from cost_meter import paths, store


class TempHome(unittest.TestCase):
    """Base for tests that write into COST_METER_HOME.

    The previous value is restored rather than unset: run_tests.py points the
    variable at one throwaway directory for the whole run, and a test that
    removed it would send every test after it at the real data/ directory.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        previous = os.environ.get("COST_METER_HOME")
        os.environ["COST_METER_HOME"] = self.tmp

        def restore():
            if previous is None:
                os.environ.pop("COST_METER_HOME", None)
            else:
                os.environ["COST_METER_HOME"] = previous

        self.addCleanup(restore)

    def config(self):
        return store.read_json(paths.config_path(), default={}) or {}

    def write_config(self, config):
        store.write_json_atomic(paths.config_path(), config)


class ClearCeilingsTest(TempHome):
    def test_a_calibrated_ceiling_is_removed_and_reported(self):
        self.write_config({"ceiling_5h_usd": 40.0, "ceiling_7d_usd": 120.0})
        self.assertEqual(calibrate.clear_ceilings(["ceiling_5h_usd"]),
                         ["ceiling_5h_usd"])
        # Only the window asked for: clearing the 5h row must leave the week
        # calibrated, or one flag would quietly undo both calibrations.
        self.assertEqual(self.config(), {"ceiling_7d_usd": 120.0})

    def test_both_can_go_at_once(self):
        self.write_config({"ceiling_5h_usd": 40.0, "ceiling_7d_usd": 120.0})
        self.assertEqual(
            calibrate.clear_ceilings(["ceiling_5h_usd", "ceiling_7d_usd"]),
            ["ceiling_5h_usd", "ceiling_7d_usd"])
        self.assertEqual(self.config(), {})

    def test_clearing_what_was_never_calibrated_is_not_an_error(self):
        # Same principle as install-hooks being safe to run twice: the flag
        # states the outcome you want, not a transition you must be mid-way
        # through for it to be legal.
        self.write_config({"ceiling_7d_usd": 120.0})
        self.assertEqual(calibrate.clear_ceilings(["ceiling_5h_usd"]), [])
        self.assertEqual(self.config(), {"ceiling_7d_usd": 120.0})

    def test_clearing_against_no_config_at_all_is_not_an_error(self):
        self.assertEqual(calibrate.clear_ceilings(["ceiling_5h_usd"]), [])

    def test_the_saved_window_position_survives(self):
        # config.json has two owners: the ceilings here and the panel's window
        # position. A wholesale rewrite rather than a read-modify-write under the
        # lock would drop whichever value this side does not know about.
        self.write_config({"ceiling_5h_usd": 40.0, "widget_position": [100, 200]})
        calibrate.clear_ceilings(["ceiling_5h_usd"])
        self.assertEqual(self.config(), {"widget_position": [100, 200]})


class ClearArgumentTest(unittest.TestCase):
    """Clearing and calibrating in one run is a contradiction, not a shortcut."""

    def test_clear_on_its_own_parses(self):
        args = calibrate.build_parser().parse_args(["--clear"])
        self.assertTrue(args.clear)

    def test_a_bare_run_is_still_rejected(self):
        with self.assertRaises(SystemExit):
            calibrate.main([])

    def test_clear_together_with_a_percentage_is_rejected(self):
        # Rejected before anything is read or written, so a contradictory run
        # cannot half-apply.
        with self.assertRaises(SystemExit):
            calibrate.main(["--clear", "--5h", "62"])

    def test_clearing_one_window_while_calibrating_the_other_is_rejected(self):
        with self.assertRaises(SystemExit):
            calibrate.main(["--clear-5h", "--week", "31"])


if __name__ == "__main__":
    unittest.main()
