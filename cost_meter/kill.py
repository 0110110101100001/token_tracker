# cost_meter/kill.py
"""Stop the panel that is running, and -- with --restart -- open a fresh one.

The panel has always been closable from its own right-click menu, which is the
normal way. This is for the case the menu cannot reach: a panel that is running
by every test the launcher applies and is nowhere on screen. An off-screen
window after a monitor changed, a GTK main loop wedged behind a modal, a panel
left over in another desktop session -- all of them look alike from a shell, and
all of them end with `launch: already running (pid N)` every session until
somebody kills the process by hand.

REFERENCE.md used to spell that out by hand for both platforms, one `kill` line
and one `Stop-Process` line. Two things are wrong with it as an instruction. It
asks the user to trust `data/widget.pid` blindly, and that file is deliberately
a *diagnostic* rather than a claim (see widget.write_pid): a panel killed hard
leaves it behind, and Windows reuses pid numbers, so the number in it can name
somebody else's process entirely. And it stops at "signal sent", which is not
the same as "panel gone" -- the interesting failure is precisely the panel that
does not go.

So this checks before and after. Before: the lock says whether a panel is up at
all, and the operating system says whether the pid in the file is still a
process that could be ours. After: the lock again, because the kernel drops it
when the holder dies however it dies, which makes "the lock is free" the only
honest proof the panel is really gone.

Unlike launch.py this is on no hook's path -- only a human runs it -- so it
reports on stdout and returns non-zero when it did not do what it was asked.
"""

import argparse
import csv
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import launch, log, paths

# How long to give the panel after each signal. GTK exits on SIGTERM without
# running teardown of its own, so this is process exit rather than a clean
# shutdown, and seconds of it is generous. It matters only when the panel is
# wedged -- which is the case this command exists for.
TERM_WAIT_SECONDS = 5.0
KILL_WAIT_SECONDS = 5.0
POLL_SECONDS = 0.1

# tasklist on a healthy machine answers in tens of milliseconds. The timeout is
# here so a sick one costs a bounded wait and an UNKNOWN rather than a hang.
TASKLIST_TIMEOUT_SECONDS = 10.0

# What a panel's process is called on Windows. pythonw is what pixi.toml's
# win-64 `widget` task runs; python.exe is what `pixi run python widget.py`
# leaves behind, which is the documented way to see the panel's output there.
WINDOWS_IMAGES = ("python.exe", "pythonw.exe")

# The verdicts on "is this pid still our panel?".
GONE = "gone"        # no such process; the pid file is a leftover
PANEL = "panel"      # a live process that could be the panel
OTHER = "other"      # a live process that certainly is not
UNKNOWN = "unknown"  # the question could not be answered on this machine


def _posix_classify(pid):
    """Read /proc to see what pid is running, if /proc is there to read.

    The cmdline, not the process name: the panel runs as a plain `python`, and
    so does half the machine. `widget.py` among the arguments is what tells it
    apart.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError:
        # Absent for two different reasons: no such process, or no /proc at all
        # (a container, a non-Linux POSIX). Only the first is a verdict.
        return GONE if Path("/proc/self/cmdline").exists() else UNKNOWN
    except OSError:
        return UNKNOWN
    text = raw.replace(b"\0", b" ").decode("utf-8", "replace")
    if not text.strip():
        # A zombie has an empty cmdline. It is not a panel worth signalling,
        # but calling it OTHER would refuse over a process already dead.
        return UNKNOWN
    return PANEL if "widget.py" in text else OTHER


def _windows_classify(pid):
    """Ask tasklist what pid is, since Windows cannot be asked more cheaply.

    os.kill(pid, 0) is the POSIX way to test for a process without touching it,
    and on Windows it is not a test at all -- CPython implements os.kill there
    with TerminateProcess, so signal 0 would kill the very process being
    checked. Hence a subprocess.
    """
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True,
            timeout=TASKLIST_TIMEOUT_SECONDS,
            # No console flash if this is ever run from a windowed parent.
            creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    for row in csv.reader(completed.stdout.splitlines()):
        # A filter matching nothing still exits 0, printing an INFO line rather
        # than nothing at all -- so the pid column is checked rather than the
        # row merely being there.
        if len(row) >= 2 and row[1].strip() == str(pid):
            return PANEL if row[0].strip().lower() in WINDOWS_IMAGES else OTHER
    return GONE


def classify(pid):
    """Whether pid is still a process that could be the panel."""
    if os.name == "nt":
        return _windows_classify(pid)
    return _posix_classify(pid)


def signal_plan():
    """The signals to try, in order, and how long to wait after each.

    POSIX escalates: SIGTERM first, because a panel that can still act on it
    exits tidily, and SIGKILL only for one that cannot. Windows has no such
    ladder -- os.kill is TerminateProcess whatever signal it is handed, so the
    first attempt is already the last resort and a second would add nothing.
    """
    if os.name == "nt":
        return ((signal.SIGTERM, TERM_WAIT_SECONDS),)
    return ((signal.SIGTERM, TERM_WAIT_SECONDS),
            (signal.SIGKILL, KILL_WAIT_SECONDS))


def wait_for_exit(seconds):
    """True once no panel holds the lock any more, False if time runs out.

    The lock rather than the pid, for the reason launch.panel_is_running()
    gives: the kernel releases it when the holder dies, so it cannot report a
    dead panel as live or a live one as dead.
    """
    deadline = time.monotonic() + seconds
    while True:
        if not launch.panel_is_running():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def clear_pid_file(pid):
    """Drop data/widget.pid, but only while it still names the pid we killed.

    The same care widget.clear_pid takes, for the same reason: between the kill
    and this line a new panel may have started and written its own number, and
    erasing that one would leave the log saying `already running (pid None)`.
    """
    path = paths.pid_path()
    try:
        if int(path.read_text(encoding="utf-8").strip()) != pid:
            return False
        path.unlink()
        return True
    except (OSError, ValueError):
        return False


def announce(line):
    """Say it on stdout and keep it in the log.

    Both, because the launcher's side of the same story is in the log:
    `launch: already running (pid N)` from every session, and then the line that
    says what became of N.
    """
    log.write(line)
    print(line)


def stop():
    """Kill the running panel. 0 when none is left running, 1 when one is."""
    if not launch.panel_is_running():
        pid = launch.read_pid()
        stale = clear_pid_file(pid) if pid is not None else False
        announce("kill: nothing running"
                 + (f" (cleared stale pid {pid})" if stale else ""))
        return 0

    pid = launch.read_pid()
    if pid is None:
        # The lock is taken and the file cannot say by whom -- a panel killed
        # mid-write, or a pid file somebody deleted. Nothing here is safe to
        # kill, so say what the user can do instead.
        announce("kill: a panel holds data/widget.lock but data/widget.pid "
                 "does not say which process. Find it by hand "
                 + ("in Task Manager (pythonw.exe)"
                    if os.name == "nt" else "with `pgrep -f widget.py`"))
        return 1

    verdict = classify(pid)
    if verdict == GONE:
        # A contradiction rather than a choice: the lock is held, and the pid
        # file names a process that does not exist. That is a stale file over a
        # panel which never wrote its own number -- not something to guess at.
        announce(f"kill: data/widget.pid names pid {pid}, which is not "
                 "running, yet a panel holds data/widget.lock. Left alone; "
                 "find the panel by hand.")
        return 1
    if verdict == OTHER:
        # Exactly the accident the by-hand instruction could not prevent:
        # Windows reuses pid numbers, so a leftover file can name somebody
        # else's process.
        announce(f"kill: pid {pid} is not a panel -- refusing to kill it. "
                 "data/widget.pid is stale.")
        return 1
    if verdict == UNKNOWN:
        # Not a refusal. The lock says a panel is up, and the pid file is the
        # only lead there is; this line is so the log says the check was
        # skipped rather than passed.
        log.write(f"kill: could not confirm what pid {pid} is; killing anyway")

    for signum, wait in signal_plan():
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass  # died between the check and the signal; the wait settles it
        except PermissionError:
            announce(f"kill: not allowed to signal pid {pid}")
            return 1
        except OSError as exc:
            announce(f"kill: signalling pid {pid} failed: {exc!r}")
            return 1
        if wait_for_exit(wait):
            clear_pid_file(pid)
            announce(f"kill: panel gone (pid {pid})")
            return 0
        log.write(f"kill: pid {pid} still holds the lock {wait}s after "
                  f"signal {int(signum)}")

    announce(f"kill: pid {pid} still holds data/widget.lock. The panel did "
             "not stop.")
    return 1


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true",
                        help="open a fresh panel once the old one is gone, "
                             "whatever auto-launch is set to. What "
                             "`pixi run resurrect` passes.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    status = stop()
    if args.restart and status == 0:
        # --force, because resurrecting a panel is a human asking for one now;
        # a paused auto-launch is a statement about what *sessions* do and is
        # left exactly as it was, the same bargain `pixi run start` strikes.
        return launch.main(["--force"])
    return status


if __name__ == "__main__":
    sys.exit(main())
