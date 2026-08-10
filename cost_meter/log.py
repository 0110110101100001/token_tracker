# cost_meter/log.py
"""The one channel anything in this tool has for saying what went wrong.

Both entry points Claude Code runs as hooks -- tally.py and cost_meter/launch.py
-- exit 0 unconditionally and write nothing to stdout or stderr, because a
broken cost meter must never cost somebody their session. That guarantee is also
what makes them impossible to debug from the outside: a hook that failed and a
hook that never ran look identical. This file is the difference.

It swallows its own failures for the same reason its callers do: a full disk, a
read-only tree or a file another process has open must not turn a silent hook
into a loud one.
"""

import time

from . import paths


def write(message):
    """Append one timestamped line to the log. Never raises."""
    try:
        path = paths.log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass
