#!/usr/bin/env python3
"""Calibrate the subscription-limit estimate against Claude Code's /usage.

Limit state is not stored locally, so it cannot be read — only estimated.
Consumption is measured in USD-equivalent rather than tokens, because Opus
draws harder on the limit than Sonnet and pricing weights the models for free.

Usage:
    ./calibrate.py --5h 62      # /usage reported 62% for the 5-hour window
    ./calibrate.py --week 31    # /usage reported 31% for the week
"""

import argparse
import sys

from cost_meter import paths, store
from tally import refresh


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--5h", dest="five_hour", type=float,
                        help="percentage /usage reports for the 5-hour window")
    parser.add_argument("--week", dest="week", type=float,
                        help="percentage /usage reports for the week")
    args = parser.parse_args()

    if args.five_hour is None and args.week is None:
        parser.error("give at least one of --5h or --week")
    for value in (args.five_hour, args.week):
        if value is not None and not 1.0 <= value <= 100.0:
            parser.error("percentages must be between 1 and 100")

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
        requested.append(("5h window", "window_5h", "ceiling_5h_usd", args.five_hour))
    if args.week is not None:
        requested.append(("week", "window_7d", "ceiling_7d_usd", args.week))

    for label, window, _, _ in requested:
        if state[window]["usd"] <= 0:
            print(f"no spend recorded in the {label}; nothing to calibrate against",
                  file=sys.stderr)
            return 1

    config = store.read_json(paths.config_path(), default={}) or {}
    for label, window, key, pct in requested:
        usd = state[window]["usd"]
        config[key] = usd / (pct / 100.0)
        print(f"{label}: ${usd:.2f} = {pct:g}% -> ceiling ${config[key]:.2f}")

    store.write_json_atomic(paths.config_path(), config)

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
