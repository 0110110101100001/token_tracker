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
if pgrep -f "python3 widget.py" >/dev/null 2>&1; then
    exit 0
fi

# No graphical session to draw on (SSH, console, headless): do nothing.
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
    exit 0
fi

setsid ./run_widget.sh >/dev/null 2>&1 </dev/null &

exit 0
