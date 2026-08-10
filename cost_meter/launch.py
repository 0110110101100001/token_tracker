# cost_meter/launch.py
"""SessionStart hook: bring the cost meter up if it is not already running.

Run through launch_widget.sh / launch_widget.cmd, or `pixi run --frozen launch`.

This is on the critical path of starting a Claude Code session, so every path
returns 0 and nothing is written to stdout or stderr. A missing display, a
broken widget, or no desktop at all must cost the user a panel, never a session.

The logic lives here rather than in the shell wrappers because there are two of
them now. A pid liveness probe and a detached spawn are both things POSIX shell
and cmd.exe express completely differently, and maintaining that twice is how
the two platforms quietly drift apart.
"""

import os
import shutil
import subprocess
import sys

from . import paths

# Nested `pixi run` rather than a direct `python widget.py`, deliberately: the
# `widget` task owns GDK_BACKEND on Linux, and pixi.toml is meant to be that
# variable's single owner. Spawning the interpreter directly would mean setting
# it here as well, and the two would drift.
WIDGET_TASK = ("run", "--frozen", "widget")


def read_pid():
    """The pid the running panel claimed, or None if there is no usable claim.

    Empty, truncated and non-numeric files all read as no claim at all, which is
    what a hard kill mid-write leaves behind.
    """
    try:
        return int(paths.pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive_windows(pid):
    """Liveness via a wait with a zero timeout: still running means it times out.

    os.kill is unusable for this on Windows -- CPython maps it onto
    TerminateProcess there, so probing with signal 0 would kill the very panel
    we are checking for.

    Waiting is preferred over GetExitCodeProcess because that reports
    STILL_ACTIVE as exit code 259, which a process that genuinely exited with
    259 is indistinguishable from.
    """
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Without these the handle comes back through a c_int and is truncated to
    # 32 bits, so every probe on a 64-bit handle would answer at random.
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False  # gone, or not ours to look at
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def pid_is_alive(pid):
    """True when a process with this pid is still running."""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it simply is not ours to signal
    except OSError:
        return False
    return True


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
        # DETACHED_PROCESS gives the child no console at all, which is both the
        # detach and the reason no window flashes up; CREATE_NO_WINDOW on top of
        # it would be redundant. The new process group keeps a Ctrl-C in the
        # launching terminal from reaching the panel.
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True  # setsid: survive the hook's group
    subprocess.Popen(command, **kwargs)


def main(argv=None):
    try:
        if pid_is_alive(read_pid()):
            # Already up -- including when the user closed it deliberately
            # mid-session, which stays closed until the next session starts.
            return 0
        if not has_display():
            return 0
        spawn_detached([shutil.which("pixi") or "pixi", *WIDGET_TASK],
                       paths.project_root())
    except Exception:
        pass  # a panel is never worth costing somebody their session
    return 0


if __name__ == "__main__":
    sys.exit(main())
