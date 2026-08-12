#!/usr/bin/env python3
"""Calibrate the subscription-limit estimate against Claude Code's /usage.

Limit state is not stored locally, so it cannot be read — only estimated.
Consumption is measured in USD-equivalent rather than tokens, because Opus
draws harder on the limit than Sonnet and pricing weights the models for free.

The 5-hour figure this divides into is the spend in the *current block* — the
one that opened on your first message after the previous block expired — not a
trailing five hours. Check that the panel's reset time matches the one /usage
prints before trusting the ceiling a run derives: if they disagree, the two
sides are dividing spend from different windows and the ceiling absorbs the
difference rather than the error being visible.

Usage (the bare -- keeps pixi from reading --5h as one of its own flags):
    pixi run calibrate -- --5h 62      # /usage reported 62% for the 5-hour block
    pixi run calibrate -- --week 31    # /usage reported 31% for the week
    pixi run calibrate -- --clear      # forget both, back to dollar figures only
"""

import argparse
import sys

from cost_meter import ceilings, paths, store
from cost_meter.ceilings import CEILINGS
from tally import refresh


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--5h", dest="five_hour", type=float,
                        help="percentage /usage reports for the 5-hour window")
    parser.add_argument("--week", dest="week", type=float,
                        help="percentage /usage reports for the week")
    parser.add_argument("--clear", action="store_true",
                        help="forget both calibrations; the rows go back to "
                             "dollar figures with no percentage")
    parser.add_argument("--clear-5h", dest="clear_5h", action="store_true",
                        help="forget the 5-hour calibration only")
    parser.add_argument("--clear-week", dest="clear_week", action="store_true",
                        help="forget the weekly calibration only")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    setting = [v for v in (args.five_hour, args.week) if v is not None]
    clearing = [(label, key) for label, key, flag in (
        ("5h window", CEILINGS["5h window"], args.clear or args.clear_5h),
        ("week", CEILINGS["week"], args.clear or args.clear_week),
    ) if flag]

    if not setting and not clearing:
        parser.error("give at least one of --5h, --week, --clear, --clear-5h "
                     "or --clear-week")
    if setting and clearing:
        # Contradictory in one run, and worse than useless if half-applied:
        # rejected up front rather than resolved by argument order.
        parser.error("--clear flags cannot be combined with --5h or --week; "
                     "run them separately")
    for value in setting:
        if not 1.0 <= value <= 100.0:
            parser.error("percentages must be between 1 and 100")

    if clearing:
        return ceilings.clear(clearing, refresh,
                              warn=lambda line: print(line, file=sys.stderr))

    try:
        with store.exclusive_lock(paths.lock_path()):
            state = refresh(session_id="")
    except store.LockTimeout as exc:
        print(f"could not calibrate: {exc}", file=sys.stderr)
        return 1

    # Validate every requested window up front, so a run either calibrates
    # everything it was asked for or changes nothing at all. Without this, a
    # mixed --5h/--week run could print a success for the first window and
    # then bail on the second's zero-spend guard before ever writing
    # config.json — a printed success that was never persisted.
    requested = []
    if args.five_hour is not None:
        requested.append(("5h window", "window_5h", CEILINGS["5h window"],
                          args.five_hour))
    if args.week is not None:
        requested.append(("week", "window_7d", CEILINGS["week"], args.week))

    for label, window, _, _ in requested:
        if state[window]["usd"] <= 0:
            print(f"no spend recorded in the {label}; nothing to calibrate against",
                  file=sys.stderr)
            return 1

    # Read and write under one lock hold: the widget stores widget_position in
    # this same file, so an unlocked read-modify-write on either side silently
    # drops the other's value. The refresh lock above is already released —
    # exclusive_lock is not reentrant, so the two must not nest.
    #
    # The lines are printed only after the write lands, so a run can never
    # report a ceiling it failed to persist.
    lines = []
    try:
        with store.update_json_locked(paths.config_path(), paths.lock_path()) as config:
            for label, window, key, pct in requested:
                usd = state[window]["usd"]
                config[key] = usd / (pct / 100.0)
                lines.append(
                    f"{label}: ${usd:.2f} = {pct:g}% -> ceiling ${config[key]:.2f}")
    except store.LockTimeout as exc:
        print(f"could not save the calibration: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)

    try:
        with store.exclusive_lock(paths.lock_path()):
            refresh(session_id="")
    except store.LockTimeout as exc:
        print(f"calibration saved, but the final refresh could not run: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
