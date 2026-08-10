#!/usr/bin/env bash
# Full check: unit tests against throwaway data, then a GTK render, then a
# proof that a genuine failure is logged rather than escaping.
#
# Everything runs through `pixi run --frozen`, so this checks the environment a
# new machine actually gets from pixi.lock -- not whatever happens to be
# installed system-wide.
set -euo pipefail
cd "$(dirname "$0")"

echo "== unit tests =="
COST_METER_HOME="$(mktemp -d)" pixi run --frozen python -m unittest discover -s tests -v

echo
echo "== widget selftest =="
# GTK needs a display even to render off-screen, so on a headless box this step
# cannot run. It is skipped out loud rather than silently passing: a green smoke
# run that never drew anything would be the more expensive lie. `xvfb-run
# ./smoke.sh` exercises it without a desktop, using your distribution's Xvfb --
# conda-forge has no Xvfb package, so it is not a dependency of this project.
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
    echo "skipped: no display (DISPLAY and WAYLAND_DISPLAY are both unset)"
else
    png="$(mktemp --suffix=.png)"
    pixi run --frozen python widget.py --selftest "$png"
    test -s "$png"
    rm -f "$png"
fi

echo
echo "== tally survives a genuine fault =="
home="$(mktemp -d)"
# The fault is a missing pricing.json, not a bad transcripts path: Path.rglob
# returns empty for a missing directory and never raises, so a bad path would
# never reach the logging code and would prove nothing. A missing pricing.json
# does raise, deep inside tally's always-exit-0 guarantee.
#
# The mv/restore must survive the script dying in between, or an interrupted
# run leaves the user's tree without pricing.json. The trap restores it on
# any exit (normal or not).
trap 'mv -f pricing.json.smoke-hidden pricing.json 2>/dev/null || true' EXIT
mv pricing.json pricing.json.smoke-hidden
set +e
COST_METER_HOME="$home" pixi run --frozen tally < /dev/null
code=$?
set -e
mv pricing.json.smoke-hidden pricing.json
trap - EXIT
test "$code" -eq 0 || { echo "tally exited $code, expected 0"; exit 1; }
test -s "$home/cost-meter.log" || { echo "no log written for a real fault"; exit 1; }

echo
echo "smoke OK"
