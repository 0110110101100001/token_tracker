#!/usr/bin/env bash
# Stop hook: refresh the cost meter after an assistant turn.
#
# The hook is registered by absolute path and runs with an arbitrary working
# directory, so `pixi run` cannot be the registered command — it has to be told
# where the manifest is. That is all this wrapper does.
#
# stdin is deliberately not redirected: tally.py reads the hook payload from it
# to learn its own session_id, which is what keeps the `last turn` row correct
# when several Claude Code sessions run at once.
#
# It exits 0 unconditionally. This sits on the user's critical path, so a
# missing environment, an unsolved lock, or a crash inside tally.py must cost a
# number on screen and never the ability to work. When that happens the panel
# says so itself: every row greys out and the warning row shows `! stale <age>`.

cd "$(dirname "$0")/.." 2>/dev/null || exit 0

# --frozen: use the environment exactly as pixi.lock describes it. No solving,
# no network, no surprise install latency on a hook that runs after every turn.
pixi run --frozen tally >/dev/null 2>&1

exit 0
