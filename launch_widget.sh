#!/usr/bin/env bash
# SessionStart hook: bring the cost meter up if it is not already running.
#
# This runs on the critical path of starting a Claude Code session, so every
# path exits 0 immediately and nothing is ever written to stdout or stderr.
# A missing display, a broken widget, or no desktop at all must cost the user
# a panel, never a session.
#
# `setsid` detaches the widget from the hook's process group, so it survives
# both this hook returning and the Claude Code session ending. Without it the
# widget is killed along with its parent.

cd "$(dirname "$0")" 2>/dev/null || exit 0

# Already up? Leave it alone — including when the user closed it deliberately
# mid-session, which stays closed until the next session starts.
#
# The pid file is what makes this decidable. Matching the command line does not
# work any more: under pixi the panel runs as `.pixi/envs/default/bin/python
# widget.py`, so a pattern naming python3 finds nothing and every session would
# stack another panel on the screen. The path mirrors paths.pid_path(), which
# puts it in COST_METER_HOME when that is set and in data/ otherwise.
pid_file="${COST_METER_HOME:-data}/widget.pid"
if [ -r "$pid_file" ]; then
    pid=$(cat "$pid_file" 2>/dev/null) || pid=""
    case "$pid" in
        # Empty, truncated, or not a number: treat as no claim at all and start.
        ''|*[!0-9]*) ;;
        # A live pid means a panel is on screen. A dead one means the last panel
        # was killed hard enough to skip its own cleanup, so the file is stale
        # and gets overwritten by the panel we start below.
        *) kill -0 "$pid" 2>/dev/null && exit 0 ;;
    esac
fi

# No graphical session to draw on (SSH, console, headless): do nothing.
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
    exit 0
fi

setsid ./run_widget.sh >/dev/null 2>&1 </dev/null &

exit 0
