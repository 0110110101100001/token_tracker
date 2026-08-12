#!/usr/bin/env python3
"""Declare a known subscription ceiling instead of deriving one from /usage.

calibrate.py turns a percentage /usage reported into a ceiling by dividing this
installation's recorded spend by it. That pairs two numbers from different
scopes -- the spend is this machine's, the percentage is the whole account's --
which holds up only while this machine's share of the account stays roughly
constant. Where the work is split across machines habitually rather than moved
occasionally, no reading of /usage yields a correct ceiling at all: the number
is not approximate, it is unobtainable.

A limit you already know sidesteps the ratio. The percentage then means this
installation's measured share of a known bound rather than an estimate of
account-wide consumption -- a smaller claim, and a true one. It stays marked
`~` on the panel, because it understates the account by however much the other
machines contribute.

Usage (the bare -- keeps pixi from reading --5h as one of its own flags):
    pixi run limit -- --week 2000    # the weekly cap, in USD-equivalent
    pixi run limit -- --5h 130       # the current 5-hour block's cap
    pixi run limit -- --clear        # forget both, back to dollar figures only
"""

import argparse
import math
import sys

from cost_meter import ceilings, paths, store
from cost_meter.ceilings import CEILINGS
from tally import refresh


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--5h", dest="five_hour", type=float,
                        help="ceiling for the 5-hour block, in USD-equivalent")
    parser.add_argument("--week", dest="week", type=float,
                        help="ceiling for the week, in USD-equivalent")
    parser.add_argument("--clear", action="store_true",
                        help="forget both ceilings; the rows go back to dollar "
                             "figures with no percentage")
    parser.add_argument("--clear-5h", dest="clear_5h", action="store_true",
                        help="forget the 5-hour ceiling only")
    parser.add_argument("--clear-week", dest="clear_week", action="store_true",
                        help="forget the weekly ceiling only")
    return parser


def plan(parser, args):
    """Turn parsed arguments into `(to_set, to_clear)`, or exit via parser.error.

    Everything is validated here, before any file is touched, so a run either
    applies all of what it was asked for or changes nothing. The three clear
    flags mirror calibrate's exactly: there is one ceiling per window, so
    offering a subset would imply a distinction that does not exist downstream.
    """
    to_set = [
        (label, CEILINGS[label], value)
        for label, value in (("5h window", args.five_hour), ("week", args.week))
        if value is not None
    ]
    to_clear = [
        (label, CEILINGS[label])
        for label, flag in (("5h window", args.clear or args.clear_5h),
                            ("week", args.clear or args.clear_week))
        if flag
    ]

    if not to_set and not to_clear:
        parser.error("give at least one of --5h, --week, --clear, --clear-5h "
                     "or --clear-week")
    if to_set and to_clear:
        # Contradictory in one run, and worse than useless if half-applied:
        # rejected up front rather than resolved by argument order.
        parser.error("--clear flags cannot be combined with --5h or --week; "
                     "run them separately")

    for _, _, value in to_set:
        if not math.isfinite(value):
            # argparse's float() accepts "nan" and "inf": neither fails on the
            # way in, and both would poison summary._pct on the way out.
            parser.error("a ceiling must be a finite number")
        if value <= 0:
            parser.error("a ceiling must be greater than zero; it is the "
                         "divisor the percentage comes from")
    return to_set, to_clear


def main(argv=None):
    parser = build_parser()
    to_set, to_clear = plan(parser, parser.parse_args(argv))

    if to_clear:
        return ceilings.clear(to_clear, refresh,
                              warn=lambda line: print(line, file=sys.stderr))

    # Written first and reported second, so a run can never print a ceiling it
    # failed to persist. One lock hold for every window named, so a two-window
    # run cannot half-apply.
    try:
        ceilings.set_ceilings({key: value for _, key, value in to_set})
    except store.LockTimeout as exc:
        print(f"could not save the ceiling: {exc}", file=sys.stderr)
        return 1
    for label, _, value in to_set:
        print(f"{label}: ceiling set to ${value:.2f} (declared)")

    # The panel redraws from the file monitor, so without this the rows would
    # keep showing no percentage until the next assistant turn. A separate lock
    # hold: exclusive_lock is not reentrant, and set_ceilings has released.
    try:
        with store.exclusive_lock(paths.lock_path()):
            refresh(session_id="")
    except store.LockTimeout as exc:
        print(f"ceiling saved, but the refresh could not run: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
