#!/usr/bin/env bash
# Full check: unit tests against throwaway data, then a GTK render, then a
# proof that a genuine failure is logged rather than escaping.
set -euo pipefail
cd "$(dirname "$0")"

echo "== unit tests =="
COST_METER_HOME="$(mktemp -d)" python3 -m unittest discover -s tests -v

echo
echo "== widget selftest =="
png="$(mktemp --suffix=.png)"
GDK_BACKEND=x11 python3 widget.py --selftest "$png"
test -s "$png"
rm -f "$png"

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
COST_METER_HOME="$home" ./tally.py < /dev/null
code=$?
set -e
mv pricing.json.smoke-hidden pricing.json
trap - EXIT
test "$code" -eq 0 || { echo "tally exited $code, expected 0"; exit 1; }
test -s "$home/cost-meter.log" || { echo "no log written for a real fault"; exit 1; }

echo
echo "smoke OK"
