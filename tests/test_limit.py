# tests/test_limit.py
"""Limit's argument contract: what it accepts, and what it refuses to."""
import unittest

from support import TempHome

import limit


class ParseTest(unittest.TestCase):
    def test_both_windows_parse(self):
        args = limit.build_parser().parse_args(["--5h", "130", "--week", "2000"])
        self.assertEqual(args.five_hour, 130.0)
        self.assertEqual(args.week, 2000.0)

    def test_a_bare_run_is_rejected(self):
        parser = limit.build_parser()
        with self.assertRaises(SystemExit):
            limit.plan(parser, parser.parse_args([]))


class PlanTest(unittest.TestCase):
    def build(self, argv):
        parser = limit.build_parser()
        return limit.plan(parser, parser.parse_args(argv))

    def test_a_declared_week_becomes_one_write(self):
        self.assertEqual(self.build(["--week", "2000"]),
                         ([("week", "ceiling_7d_usd", 2000.0)], []))

    def test_both_windows_become_two_writes(self):
        to_set, to_clear = self.build(["--5h", "130", "--week", "2000"])
        self.assertEqual(to_set, [("5h window", "ceiling_5h_usd", 130.0),
                                  ("week", "ceiling_7d_usd", 2000.0)])
        self.assertEqual(to_clear, [])

    def test_clear_expands_to_both_windows(self):
        to_set, to_clear = self.build(["--clear"])
        self.assertEqual(to_set, [])
        self.assertEqual(to_clear, [("5h window", "ceiling_5h_usd"),
                                    ("week", "ceiling_7d_usd")])

    def test_clear_week_expands_to_one(self):
        self.assertEqual(self.build(["--clear-week"]),
                         ([], [("week", "ceiling_7d_usd")]))


class RejectionTest(unittest.TestCase):
    def reject(self, argv):
        parser = limit.build_parser()
        with self.assertRaises(SystemExit):
            limit.plan(parser, parser.parse_args(argv))

    def test_zero_is_rejected(self):
        # It becomes a divisor.
        self.reject(["--week", "0"])

    def test_a_negative_ceiling_is_rejected(self):
        self.reject(["--week", "-5"])

    def test_nan_is_rejected(self):
        # argparse's float() accepts the spelling; _pct would inherit the poison.
        self.reject(["--week", "nan"])

    def test_inf_is_rejected(self):
        self.reject(["--week", "inf"])

    def test_setting_and_clearing_in_one_run_is_rejected(self):
        # Contradictory, and worse than useless if half-applied.
        self.reject(["--week", "2000", "--clear"])

    def test_clearing_one_window_while_declaring_the_other_is_rejected(self):
        self.reject(["--clear-5h", "--week", "2000"])


class WriteTest(TempHome):
    """main() writes, reports, and refreshes -- in that order."""

    def test_a_declared_week_lands_in_config(self):
        self.assertEqual(limit.main(["--week", "2000"]), 0)
        self.assertEqual(self.config()["ceiling_7d_usd"], 2000.0)

    def test_both_windows_land_together(self):
        limit.main(["--5h", "130", "--week", "2000"])
        self.assertEqual(self.config(), {"ceiling_5h_usd": 130.0,
                                         "ceiling_7d_usd": 2000.0})

    def test_a_declaration_needs_no_spend(self):
        # The case calibrate refuses: with nothing recorded there is no ratio to
        # take, but a declared ceiling does not come from one. TempHome's
        # transcript directory is empty, so no event exists in any window.
        self.assertEqual(limit.main(["--5h", "130"]), 0)
        self.assertEqual(self.config()["ceiling_5h_usd"], 130.0)

    def test_the_saved_window_position_survives(self):
        self.write_config({"widget_position": [100, 200]})
        limit.main(["--week", "2000"])
        self.assertEqual(self.config(), {"widget_position": [100, 200],
                                         "ceiling_7d_usd": 2000.0})

    def test_a_declaration_replaces_a_derived_ceiling(self):
        # One ceiling per window: whichever tool wrote it, the other overwrites.
        self.write_config({"ceiling_7d_usd": 28.58})
        limit.main(["--week", "2000"])
        self.assertEqual(self.config()["ceiling_7d_usd"], 2000.0)

    def test_a_rejected_run_writes_nothing(self):
        self.write_config({"ceiling_7d_usd": 2000.0})
        with self.assertRaises(SystemExit):
            limit.main(["--week", "0"])
        self.assertEqual(self.config(), {"ceiling_7d_usd": 2000.0})

    def test_clearing_goes_through_the_shared_path(self):
        self.write_config({"ceiling_7d_usd": 2000.0})
        self.assertEqual(limit.main(["--clear-week"]), 0)
        self.assertEqual(self.config(), {})


if __name__ == "__main__":
    unittest.main()
