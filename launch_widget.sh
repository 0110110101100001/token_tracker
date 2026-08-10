#!/usr/bin/env bash
# SessionStart hook: bring the cost meter up if it is not already running.
#
# The decisions -- is a panel already up, is there a display, how to detach the
# one we start -- live in cost_meter/launch.py, which launch_widget.cmd shares.
# A pid liveness probe and a detached spawn are expressed completely differently
# by POSIX shell and cmd.exe, and maintaining that logic twice is how the two
# platforms quietly drift apart. This script only locates the repo.
#
# It exits 0 unconditionally, like the module it calls. This runs on the
# critical path of starting a Claude Code session, so a missing environment, a
# broken widget, or no desktop at all must cost the user a panel, never a
# session. Nothing is written to stdout or stderr.
#
# --frozen: use the environment exactly as pixi.lock describes it. No solving,
# no network, no surprise install latency at session start.

cd "$(dirname "$0")" 2>/dev/null || exit 0

pixi run --frozen launch >/dev/null 2>&1

exit 0
