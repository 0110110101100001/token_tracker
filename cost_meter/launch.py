# cost_meter/launch.py
"""Bring the cost meter up if it is not already running.

Two callers, and the difference between them is the whole shape of this module:

- The **SessionStart hook**, through launch_widget.sh / launch_widget.cmd or
  `pixi run --frozen launch`. It obeys a paused auto-launch and speaks only to
  the log.
- **A human**, through `pixi run start`, which passes `--force`. Pausing is a
  statement about what *sessions* do, so somebody typing a command overrides it
  without changing it — and gets told what happened, on stdout.

That last part is not a nicety. A paused hook printing nothing and exiting 0 is
indistinguishable from a panel that opened fine, which is exactly how somebody
loses half an hour wondering where their window went.

This is on the critical path of starting a Claude Code session, so every path
returns 0 and the hook writes nothing to stdout or stderr. A missing display, a
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

import argparse
import os
import shutil
import subprocess
import sys

from . import autolaunch, log, paths, store

# Nested `pixi run` rather than a direct `python widget.py`, deliberately: the
# task owns GDK_BACKEND on Linux and picks pythonw over python on Windows, and
# pixi.toml is meant to be the single owner of both. Spawning an interpreter
# directly would mean repeating that here, and the two would drift.
WIDGET_TASK = ("run", "--frozen", "widget")

# What actually detaches the panel on a systemd Linux desktop. setsid escapes the
# process group and the session, but not the cgroup, and terminal emulators put
# each tab in a transient scope with KillMode=control-group -- so closing the tab
# that happened to win the launch race killed the panel with it, however detached
# it was. `--scope` moves the process into a scope of its own and only then execs,
# which is why the pid we log is still the real one.
#
# The unit is deliberately left unnamed. A fixed name reads better in the log, but
# it collides with any scope a previous panel left behind, and on this path a
# collision costs the panel; systemd's generated name cannot collide.
SCOPE_PREFIX = ("systemd-run", "--user", "--scope", "--quiet", "--collect")
# Long enough for systemd-run to have failed and exited (it takes tens of
# milliseconds), short enough to be free on a hook that runs once per session.
SCOPE_WAIT_SECONDS = 0.5


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


def systemd_available():
    """True when this session can put the panel in a transient scope of its own.

    /run/systemd/system is the documented test for "booted with systemd" (see
    sd_booted(3)); it is a directory only on the real thing, so a container with
    the binaries but no manager reads as False.
    """
    if os.name == "nt":
        return False
    return (os.path.isdir("/run/systemd/system")
            and shutil.which("systemd-run") is not None)


def spawn_detached(command, cwd):
    """Start the panel so it outlives both this hook and the session itself.

    On a systemd Linux desktop that means a transient scope of the panel's own,
    because the terminal tab this hook runs in is itself a scope that takes its
    whole cgroup down with it. Everywhere else, and if the scope will not start,
    a plainly detached child is the best available and still the old behaviour.
    """
    if systemd_available():
        child = _spawn([*SCOPE_PREFIX, *command], cwd)
        try:
            status = child.wait(timeout=SCOPE_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            return child  # still running, so the scope took and it is the panel
        # Exited already. Whether systemd-run could not reach the user manager or
        # the panel itself died on startup is not knowable from here, so the log
        # says what happened rather than guessing why, and we try the plain spawn
        # instead of leaving the user with no panel at all.
        log.write(f"launch: scope spawn exited immediately (rc {status}), "
                  f"retrying without a scope")
    return _spawn(command, cwd)


def _spawn(command, cwd):
    """Detach a child as far as this platform alone allows."""
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


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="open the panel even when auto-launch is paused, "
                             "and report the outcome on stdout. What "
                             "`pixi run start` passes; the hook never does.")
    return parser


def main(argv=None):
    # Parsed outside the try, and strictly. The hook passes no arguments at all,
    # so it cannot reach an error exit here; the flag arrives only from the
    # fixed argv in pixi.toml, which no user types by hand.
    args = build_parser().parse_args(argv)

    def announce(line):
        """Log every outcome; print it too when a human asked for this run."""
        log.write(line)
        if args.force:
            print(line)

    try:
        if autolaunch.paused() and not args.force:
            # First, before the liveness probe: a hook the user has switched off
            # should do nothing at all, not take locks to decide it.
            announce("launch: paused")
            return 0
        if panel_is_running():
            # Already up -- including when the user closed it deliberately
            # mid-session, which stays closed until the next session starts.
            announce(f"launch: already running (pid {read_pid()})")
            return 0
        if not has_display():
            announce("launch: no display")
            return 0
        child = spawn_detached([shutil.which("pixi") or "pixi", *WIDGET_TASK],
                               paths.project_root())
        # The pixi shim's pid, not the panel's -- the panel records its own in
        # widget.pid once it is up. Both are worth having: if the second never
        # appears, the first says the spawn was not where it broke.
        announce(f"launch: spawned pixi pid {child.pid}")
    except Exception as exc:
        # A panel is never worth costing somebody their session, but a swallowed
        # exception with no trace of it is how this went undiagnosed before.
        announce(f"launch: failed: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
