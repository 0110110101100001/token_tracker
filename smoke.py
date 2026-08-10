#!/usr/bin/env python3
"""Full check: unit tests, a real GTK render, and a proof that a genuine
failure is logged rather than escaping.

Run as `pixi run smoke`, which is what makes this a check of the environment a
new machine actually gets from pixi.lock rather than of whatever happens to be
installed system-wide. Subprocesses reuse sys.executable, which is that same
environment's interpreter.

Python rather than bash so Windows runs this check too: mktemp, trap and the
test for a display all have portable equivalents in the standard library.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cost_meter import launch, paths

ROOT = paths.project_root()


def run(args, env=None, stdin=subprocess.DEVNULL):
    """Run a command in this environment's interpreter. Returns the exit code."""
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.call([sys.executable, *args], cwd=str(ROOT),
                           env=merged, stdin=stdin)


def unit_tests():
    print("== unit tests ==", flush=True)
    return run(["run_tests.py"])


def widget_selftest():
    print("\n== widget selftest ==", flush=True)
    # GTK needs a display even to render off-screen, so on a headless box this
    # step cannot run. It is skipped out loud rather than silently passing: a
    # green smoke run that never drew anything would be the more expensive lie.
    # `xvfb-run pixi run smoke` exercises it without a desktop, using your
    # distribution's Xvfb -- conda-forge has no Xvfb package, so it is not a
    # dependency of this project. Windows always has a display to draw on.
    if not launch.has_display():
        print("skipped: no display (DISPLAY and WAYLAND_DISPLAY are both unset)")
        return 0

    handle, png = tempfile.mkstemp(suffix=".png")
    os.close(handle)
    try:
        code = run(["widget.py", "--selftest", png])
        if code != 0:
            return code
        if Path(png).stat().st_size == 0:
            print("selftest wrote an empty file", file=sys.stderr)
            return 1
    finally:
        Path(png).unlink(missing_ok=True)
    return 0


def tally_survives_a_fault():
    print("\n== tally survives a genuine fault ==", flush=True)
    # The fault is a missing pricing.json, not a bad transcripts path:
    # Path.rglob returns empty for a missing directory and never raises, so a
    # bad path would never reach the logging code and would prove nothing. A
    # missing pricing.json does raise, deep inside tally's always-exit-0
    # guarantee.
    pricing = paths.pricing_path()
    hidden = pricing.with_suffix(pricing.suffix + ".smoke-hidden")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
        # try/finally rather than a trap: an interrupted run must not leave the
        # user's tree without pricing.json.
        pricing.rename(hidden)
        try:
            code = run(["tally.py"], env={"COST_METER_HOME": home})
        finally:
            hidden.rename(pricing)

        if code != 0:
            print(f"tally exited {code}, expected 0", file=sys.stderr)
            return 1
        log = Path(home) / "cost-meter.log"
        if not log.exists() or log.stat().st_size == 0:
            print("no log written for a real fault", file=sys.stderr)
            return 1
    return 0


def main():
    for step in (unit_tests, widget_selftest, tally_survives_a_fault):
        code = step()
        if code != 0:
            return code
    print("\nsmoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
