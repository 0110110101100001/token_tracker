# tests/test_calibrate.py
"""Calibrate's argument contract. The ceiling keys themselves live in
tests/test_ceilings.py, alongside the module that now owns them."""
import unittest

import calibrate


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
