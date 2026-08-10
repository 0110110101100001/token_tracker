# Windows support — design

**Date:** 2026-08-10
**Status:** approved

## Goal

Run the cost meter on Windows with the same experience Linux already has: one
`pixi install`, hooks registered by the installer, and the GTK panel on screen.
No system Python, no second toolkit, no separate repo layout.

## What is already portable

`parser.py`, `pricing.py`, `summary.py`, `tally.py` and `calibrate.py` are
standard library only and need no changes. Transcripts live under
`~/.claude/projects/` on both platforms, and `Path.home()` resolves it
correctly on Windows. `store.write_json_atomic` uses `os.replace`, which is
atomic on Windows as well.

## What blocks Windows today

| Location | Blocker |
| --- | --- |
| `cost_meter/store.py` | `fcntl.flock` is POSIX-only |
| `pixi.toml` | `platforms = ["linux-64"]`; `GDK_BACKEND=x11` on the `widget` task |
| `hooks/tally.sh`, `launch_widget.sh`, `run_widget.sh`, `install.sh`, `smoke.sh` | bash, plus `setsid`, `mktemp`, `kill -0`, `trap` |
| `cost_meter/install.py` | XDG `.desktop` autostart; case-sensitive path ownership check |
| `tests/test_install.py` | hardcodes POSIX paths and `.sh` hook names |

## Design

### 1. Environment

`platforms = ["linux-64", "win-64"]` with a single `pixi.lock`. Pixi solves
several platforms into one lock file natively; two lock files would be a
second source of truth for the same dependency set.

`gsettings-desktop-schemas` has no `win-64` build on conda-forge and moves to
`[target.linux-64.dependencies]`. Windows does not need it: `gtk3` ships the
`org.gtk.Settings` schemas itself, and GDK's win32 backend reads desktop
settings from the registry rather than from GSettings. `python`, `pygobject`,
`gtk3`, `librsvg` and `adwaita-icon-theme` all have `win-64` builds, pygobject
including a `cp314` variant, so the pinned `python = "3.14.*"` stands.

`GDK_BACKEND=x11` is correct on Linux and fatal on Windows, where the backend
is `win32`. The `widget` task splits into `[target.linux-64.tasks]` (with the
variable) and `[target.win-64.tasks]` (without it). The variable keeps exactly
one owner; it just becomes platform-scoped.

`widget.py` itself is not rewritten. It is plain GTK 3 with no X11 calls, and
`get_primary_monitor()` already falls back to `get_monitor(0)` — the branch a
win32 display may need.

### 2. Cross-process lock

`store.exclusive_lock` keeps its shape — bounded retry loop, `LockTimeout` on
expiry — and gains a platform-split `_lock`/`_unlock` pair:

- POSIX: `fcntl.flock(handle, LOCK_EX | LOCK_NB)` as today.
- Windows: `msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)`.

Three details make the Windows half correct:

- `msvcrt.locking` locks a byte range **from the current file position**, not
  the whole file. Both lock and unlock must `seek(0)` first, or a second
  acquisition locks a different byte and the mutual exclusion silently fails.
- The lock file is opened `"a+"` instead of `"w"`. Windows refuses to truncate
  a file another process holds a byte-range lock on, and `"a+"` still creates
  it on first use. On Linux this changes nothing: the file's contents are
  never read.
- `LK_NBLCK` raises `OSError` on contention, exactly like `flock(LOCK_NB)`, so
  the surrounding deadline loop is untouched.

### 3. Entry points

The logic moves into Python; the shell files become two-line wrappers.

New `cost_meter/launch.py` owns what `launch_widget.sh` does today:

| Concern | POSIX | Windows |
| --- | --- | --- |
| Is the pid alive? | `os.kill(pid, 0)` | `OpenProcess` + `GetExitCodeProcess` via `ctypes` |
| Is there a display? | `DISPLAY` / `WAYLAND_DISPLAY` | always true |
| Detach the child | `start_new_session=True` | `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP` |

`os.kill(pid, 0)` must **not** be used on Windows: CPython maps `os.kill` there
onto `TerminateProcess`, so the liveness probe would kill the panel it is
checking for. Hence `ctypes`. The probe waits on the process handle with a zero
timeout rather than reading its exit code, because `GetExitCodeProcess` reports
a running process as `STILL_ACTIVE` — the value 259, which a process that
genuinely exited with 259 is indistinguishable from.

`DETACHED_PROCESS` already denies the child a console, which is both the detach
and the reason no window flashes up, so `CREATE_NO_WINDOW` on top of it would
be redundant. A hand-run `pixi run widget` keeps its console and stays noisy,
which is what you want when debugging.

`launch.py` keeps `launch_widget.sh`'s contract: every path exits 0, nothing
reaches stdout or stderr.

`smoke.sh` becomes `smoke.py` (`tempfile.mkdtemp` for `mktemp`, `try/finally`
for `trap`), and the `test` task stops needing `bash -c`. Both become plain
pixi tasks running Python, shared by the two platforms.

Wrappers on disk: the existing `.sh` files stay, `smoke.sh` goes (superseded by
`smoke.py`), and `hooks/tally.cmd`, `launch_widget.cmd`, `run_widget.cmd` and
`install.cmd` join them. Each does one thing — `cd` to the repo and hand off to
`pixi run --frozen <task>`.

A `.gitattributes` pins `*.sh` to LF and `*.cmd` to CRLF. With a checkout on
each platform, `core.autocrlf` would otherwise decide per machine and get one of
them wrong, and bash fails on a CRLF shebang with a bare `\r: command not found`
— an error that reads as a broken tool rather than as wrong newlines.

**Accepted cost:** `launch_widget` now pays a `pixi run` startup even when a
panel is already up, where the bash version decided that for free. Hundreds of
milliseconds against the hook's 10 s timeout.

### 4. Installer

`HOOKS` becomes platform-dependent, registering the `.cmd` wrappers on Windows
and the `.sh` ones elsewhere.

Autostart gains a Windows counterpart: a `.cmd` in
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\` rather than an XDG
`.desktop`. The Startup folder is chosen over the `Run` registry key precisely
so `_autostart_is_ours` still works — the entry is a readable file whose target
can be checked against this checkout, which keeps the "never delete an entry we
did not write" regression test meaningful. Reading that target generalises:
the `Exec=` line from a `.desktop`, the invoked path from a `.cmd`.

`_owned_by_us` compares case-insensitively on Windows. Windows paths are
case-insensitive, so a repo moved between differently-cased paths would
otherwise leave the old `Stop` hook registered alongside the new one — the
exact double-hook failure that function exists to prevent, where the second
hook finds no new messages and overwrites `last turn` with `$0.00`.

### 5. Tests and docs

`tests/test_install.py` stops hardcoding `/opt/cost-meter` and
`hooks/tally.sh`, deriving expected commands from `install.HOOKS` and using a
platform-appropriate root. New tests cover the pid liveness probe and the
Windows autostart ownership branch. The lock's existing tests exercise the new
Windows path automatically when run there.

`README.md` gets per-platform Requirements and Install sections. Its current
claim that there is "no macOS or Windows support" is corrected; the macOS half
of that sentence stands, since this design does not cover it.

## Out of scope

- macOS. `store.py` becomes portable enough for it, but no `osx-*` platform is
  added, solved or tested.
- `linux-aarch64`, unchanged from before.
- Any change to how costs are computed, priced or displayed.

## Verification

Done on a real Windows 10 machine, not asserted. All of the following passed:

1. `pixi install` resolved one lock covering `linux-64` and `win-64`, with
   `gsettings-desktop-schemas` appearing only under Linux.
2. `pixi run test` — 104 tests, OK.
3. `pixi run smoke` — OK, GTK render included (284×122, 745 distinct colours).
4. `install.cmd` registered both `.cmd` hooks and preserved every unrelated
   setting; a second run left exactly one hook per event.
5. The panel drew bottom-right, stayed on top, and showed real figures.
6. The launcher started a panel in about five seconds, was a no-op on a second
   run, and replaced the claim of a panel that had been killed hard.

One caveat found by running it: on Windows the hooks need `pixi` on `PATH`, and
a `PATH` change does not reach an already-running process, so Claude Code has to
be restarted after pixi is installed. It fails silently and fast when it is not —
the wrappers exit 0 by design. This is documented in the README's Install and
Troubleshooting sections.
