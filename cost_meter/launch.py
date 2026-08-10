# cost_meter/launch.py
"""SessionStart hook: bring the cost meter up if it is not already running.

Run through launch_widget.sh / launch_widget.cmd, or `pixi run --frozen launch`.

This is on the critical path of starting a Claude Code session, so every path
returns 0 and nothing is written to stdout or stderr. A missing display, a
broken widget, or no desktop at all must cost the user a panel, never a session.

Every path does write one line to cost-meter.log, though. Without it a hook that
ran and decided to do nothing is indistinguishable from a hook that never ran at
all, and telling those two apart is most of the work when the panel fails to
appear.

The logic lives here rather than in the shell wrappers because there are two of
them now. A liveness probe and a detached spawn are both things POSIX shell and
cmd.exe express completely differently, and maintaining that twice is how the
two platforms quietly drift apart.
"""

import os
import shutil
import subprocess
import sys

from . import log, paths, store

# Nested `pixi run` rather than a direct `python widget.py`, deliberately: the
# task owns GDK_BACKEND on Linux and picks pythonw over python on Windows, and
# pixi.toml is meant to be the single owner of both. Spawning an interpreter
# directly would mean repeating that here, and the two would drift.
WIDGET_TASK = ("run", "--frozen", "widget")


def read_pid():
    """The pid the running panel claimed, or None if there is no usable claim.

    Diagnostic only -- what goes in the log line so a stuck panel can be found
    in Task Manager. Whether a panel is running is decided by the lock, not by
    this: empty, truncated and non-numeric files all read as no claim at all,
    which is what a hard kill mid-write leaves behind.
    """
    try:
        return int(paths.pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def panel_is_running():
    """True when a panel already holds the lock it keeps for its whole run.

    This used to probe the pid file for liveness, which is a question a pid
    cannot answer: Windows reuses pid numbers, so any unrelated process that
    happened to land on the number a dead panel left behind would suppress the
    launch -- not once, but every session from then on, silently.

    A lock cannot lie that way. The kernel drops it when the holder exits,
    however it exits, so failing to take it means a live panel and nothing else.
    """
    handle = store.try_acquire(paths.widget_lock_path())
    if handle is None:
        return True
    store.release(handle)
    return False


def has_display():
    """False when there is nothing to draw on, so we should not even try.

    On Windows a session that can run this hook can also open a window, so there
    is nothing to test. On POSIX this is what keeps SSH and console sessions
    from spawning a panel that can never appear.
    """
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def spawn_detached(command, cwd):
    """Start the panel so it outlives both this hook and the session itself."""
    kwargs = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # DETACHED_PROCESS gives the child no console at all, which is the
        # detach; CREATE_NO_WINDOW on top of it would be redundant. The new
        # process group keeps a Ctrl-C in the launching terminal from reaching
        # the panel.
        #
        # Note what this does *not* buy: having no console to inherit is exactly
        # what makes Windows hand a console-subsystem child one of its own, so
        # the panel must be started by pythonw rather than python or a black
        # console window appears beside it. pixi.toml's win-64 `widget` task is
        # where that is arranged.
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True  # setsid: survive the hook's group
    return subprocess.Popen(command, **kwargs)


def main(argv=None):
    try:
        if panel_is_running():
            # Already up -- including when the user closed it deliberately
            # mid-session, which stays closed until the next session starts.
            log.write(f"launch: already running (pid {read_pid()})")
            return 0
        if not has_display():
            log.write("launch: no display")
            return 0
        child = spawn_detached([shutil.which("pixi") or "pixi", *WIDGET_TASK],
                               paths.project_root())
        # The pixi shim's pid, not the panel's -- the panel records its own in
        # widget.pid once it is up. Both are worth having: if the second never
        # appears, the first says the spawn was not where it broke.
        log.write(f"launch: spawned pixi pid {child.pid}")
    except Exception as exc:
        # A panel is never worth costing somebody their session, but a swallowed
        # exception with no trace of it is how this went undiagnosed before.
        log.write(f"launch: failed: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
