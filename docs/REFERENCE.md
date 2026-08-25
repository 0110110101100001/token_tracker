# Reference

What the installer writes and why, every file the tool owns, the settings you
can put in `data/config.json`, what to check when something looks wrong, and how
to run the tests.

← [README](../README.md) · [Metering](METERING.md) · [The panel](PANEL.md)

## Install, in detail

`install-hooks` edits `~/.claude/settings.json` for you because the hooks must
be registered by absolute path, which is the one thing that cannot be committed
to the repo — it differs on every machine. It backs the file up first, merges
rather than replacing, and is safe to run twice. The result looks like this,
with your own path:

```json
"Stop": [
  {"hooks": [{"type": "command", "command": "\"<repo>/hooks/tally.sh\"", "timeout": 20}]}
],
"SessionStart": [
  {
    "matcher": "startup|resume|clear|compact",
    "hooks": [{"type": "command", "command": "\"<repo>/launch_widget.sh\"", "timeout": 30}]
  }
]
```

The `matcher` is not decoration: a `SessionStart` group registered without one
is not reliably run, and the symptom is indistinguishable from a broken panel —
the hook writes nothing and exits 0 whatever happens. The 30-second budget is
for the worst case rather than the normal one, which is about a second: the
first run after a reboot pays for pixi starting, an interpreter booting and a
virus scanner reading a few hundred megabytes of environment, and a hook killed
on its timeout dies before it ever reaches the spawn.

The path is written with forward slashes and in quotes on both platforms, and
neither is cosmetic. Claude Code does not run a registered hook itself — it
hands the string to `bash -c`, on Windows too, through Git Bash. A backslash is
an escape character there, so a native Windows path arrives with every
separator eaten (`C:UsersyouCode...launch_widget.cmd`) and the hook dies with
`command not found`. Windows accepts forward slashes wherever it takes a path,
so the one spelling suits both shells. The quotes are for the other half of the
same problem: unquoted, a repo under `Program Files` or any `My Projects`
directory would have the shell run the first half of its own path and pass the
rest as arguments.

Neither failure announces itself — a hook error is non-blocking and both
wrappers exit 0 by design — so the whole symptom is a panel that never appears
and numbers that stop moving.

On Windows the entries name `hooks/tally.cmd` and `launch_widget.cmd`. The
installer picks the pair that matches the platform it is run on; what the two
wrap is identical, being the same pixi tasks. They differ because their
contents must: only the Windows pair needs the `PATH` repair that finds pixi
for a session started before pixi was installed.

The two flags from [Install](../README.md#install) and
[Uninstall](../README.md#uninstall) work on the
same file. `--autostart` writes an XDG `.desktop` entry on Linux and a `.cmd` in
your Startup folder on Windows; `--uninstall` removes both hooks and that entry
again, but only after reading it back and confirming it launches *this*
checkout.

If you move or rename the repo, re-run `pixi run install-hooks`. It recognises
its own stale entries and replaces them, which matters: two live `Stop` hooks
would both run, and the second would find no new messages and overwrite
**last turn** with `$0.00`.

## Starting and stopping

The two halves are independent of each other: the hook keeps counting whether or
not the panel is on screen, and the panel keeps rendering whether or not the
hook is registered (it marks itself stale after ten minutes without an update).

Both do, however, need the pixi environment — every entry point runs through
`pixi run --frozen`, which uses `pixi.lock` exactly as committed and never
touches the network. If the environment is missing or out of date, the hook
stops producing numbers and the panel says so with its `! stale` row rather than
failing visibly. After editing `pixi.toml`, run `pixi install`.

Start the panel by hand:

```bash
pixi run start        # detached, whatever auto-launch is set to — the usual one
pixi run widget       # in the foreground
pixi run launch       # detached, but obeys a paused auto-launch and stays silent
```

`pixi run show` is the old name for `start` and still works.

None of the three opens a second panel while one is up: `start` and
`launch` say `launch: already running` and stop, and `widget` — which has
no launcher in front of it — exits as soon as it finds `widget.lock`
taken, before it draws anything.

On Linux `pixi run widget` is also how you see the panel's errors. On Windows it
is not — the task runs `pythonw`, which has nowhere to write them ([why](PANEL.md#how-the-panel-detaches-from-what-started-it)); use
`pixi run python widget.py` when you want the output.

`pixi run launch` is what the SessionStart hook ends up running, through
`launch_widget.sh` or `launch_widget.cmd`. It is also the half that
[can be paused](../README.md#keep-the-panel-closed), so a panel you closed stays closed; the
hook then logs `launch: paused` and does nothing else, and `pixi run widget`
still brings the panel up by hand. `run_widget.sh` / `run_widget.cmd`
start the panel in the foreground from outside the pixi environment, which is
what an autostart entry wants.

How the launcher detaches the panel from the shell that started it — a transient
systemd scope on Linux, a `DETACHED_PROCESS` child on Windows — is under
[How the panel detaches](PANEL.md#how-the-panel-detaches-from-what-started-it).

Stop it with right click → *Quit*. A panel you close stays closed for the rest of
the session: the running panel holds `data/widget.lock`, and the launcher starts
one only when it can take that lock itself. See
[The lock](PANEL.md#the-lock-that-keeps-a-closed-panel-closed).

`data/widget.pid` is still written, now purely so a stuck panel can be found. If
you ever need to kill it from a shell, use that file rather than matching on the
process name:

```bash
kill "$(cat data/widget.pid)"                             # Linux
```
```powershell
Stop-Process -Id (Get-Content data\widget.pid).Trim()     # Windows
```

Force a recount without waiting for a turn to finish:

```bash
pixi run tally < /dev/null    # Linux
```
```powershell
$null | pixi run tally        # Windows
```

Either way the point is the same: `tally` reads the hook payload from stdin, so
it must be given one that ends rather than a terminal it will wait on.

## Files

Everything runtime-owned lives under `data/`, and is safe to delete wholesale
(see below):

- `events.jsonl` — append-only ledger of priced messages, pruned to the
  trailing 8 days.
- `state.json` — the dollar figures the widget displays, the `billing` mode the
  hook observed in its own session's environment, and a copy of the account's
  limit rows under `limits` (with the age of the figures they came from). The
  widget does not read that copy — it goes to the two live sources below;
  `tally` keeps it because the reset times in it anchor the dollar windows.
- `usage.json` — the account's limit percentages as the panel last fetched them
  from the server, in the same shape Claude Code caches in `~/.claude.json`, with
  the account it belongs to and when it was fetched. The panel writes it once a
  minute and reads whichever of the two files is newer.
- `offsets.json` — per-transcript file read positions, so re-running the
  scan never re-reads or double-counts a line.
- `session_marks.json` — one bookmark per session recording the newest event
  already reported as that session's **last turn**. This is what keeps the
  row correct when several Claude Code sessions run at once: whichever hook
  fires first picks up every session's new messages, so "new since my own
  last run" has to be remembered per session rather than inferred from which
  run happened to append the events. Bookmarks for sessions idle longer than
  the 8-day prune window are dropped.
- `config.json` — the panel's own settings: its last window position and size, a
  paused auto-launch, and a custom poll interval for the limit fetch. Every key is
  listed under [Configuration](#configuration).
- `widget.lock` — held exclusively by the running panel for as long as it
  lives. This is how the launcher tells whether one is already up, and it is what
  keeps there being exactly one: a panel that cannot take the lock at startup
  exits before it draws anything. The launcher's check cannot stand alone, because
  the panel it spawns takes seconds to reach that claim and a second hook firing
  inside that gap finds the lock free. The kernel drops it however the panel dies,
  including a hard kill.
- `widget.pid` — the running panel's pid, so a stuck one can be found and
  killed. Diagnostic only: a pid cannot answer the liveness question honestly on
  Windows, which reuses the numbers.
- `cost-meter.log` — where the `Stop` hook and the `SessionStart` hook record
  what they swallowed to keep your critical path unbroken. The launcher also logs
  the boring outcomes (`launch: spawned`,
  `launch: already running`, `launch: paused`), because a hook that ran and
  decided to do nothing and a hook that never ran are otherwise
  indistinguishable. A panel that found the lock taken and stood down says so
  too (`widget: another panel holds the lock, exiting`, and
  `launch: another panel won the race` from the launcher that started it) —
  expect one of each per session on a front end that starts two sessions at
  once, which is what Claude Desktop does in code mode.

Deleting `data/` entirely is a safe full reset: the next run rebuilds
`events.jsonl` and `state.json` from the real transcripts under
`~/.claude/projects/` from scratch. You lose the saved window position and size,
a paused auto-launch and a custom poll interval, nothing else — the limit
percentages are unaffected, since the next fetch is a minute away and Claude
Code's own cache answers in the meantime. With the bookmarks
gone too, the first turn after a reset reads as the whole session's cost, since
there is no earlier mark to measure from; it corrects itself on the next turn.

## Configuration

`data/config.json` is written by the panel and by `pixi run autolaunch`, and you
can edit it by hand. Every key is optional; a missing one means the default.
Delete the file to reset all of them.

| key | what it does |
| --- | --- |
| `widget_position` | Where the panel sits, as saved by dragging it. Absent until you move it, and the panel then anchors itself to the bottom-right corner. Right click → **Reset position** removes it. |
| `widget_scale` | How large the panel is, from `0.7` to `3.0`, as saved by dragging an edge. Right click → **Reset size** removes it. See [Moving and resizing it](PANEL.md#moving-and-resizing-it). |
| `autolaunch_paused` | `true` when Claude Code sessions are not allowed to open the panel. Set it with `pixi run autolaunch -- --off`, or the panel's own **Pause auto-launch**. Recording is unaffected — see [Keep the panel closed](../README.md#keep-the-panel-closed). |
| `usage_poll_seconds` | How often the panel asks the server for the account's limit figures, in seconds. Defaults to `60`; `0` switches the fetch off and leaves the rows on Claude Code's own cache. Below about ten seconds the endpoint refuses outright — see [Where the limit figures come from](METERING.md#where-the-limit-figures-come-from). |

## Troubleshooting

- **The widget stops updating** — the panel says so itself: every dollar row
  greys out and the warning row shows how long it has been (`! stale 1 h
  37 min`). The limit percentages carry on regardless; they do not come from the
  hook. Check `data/cost-meter.log`. Every fault on the `Stop` hook's
  critical path is caught and logged there rather than shown, by design, so a
  stuck panel almost always has a line waiting there. If the log is empty, the
  hook is not reaching Python at all: run `hooks/tally.sh` (or `hooks\tally.cmd`)
  by hand and check that `pixi run --frozen tally` works from the repo.
- **Numbers look lower than expected** — check the panel for a `?` warning
  row. It means at least one model you used has no entry in `pricing.json`
  and is being excluded from the totals rather than counted as zero.
- **The panel vanishes mid-session and comes back later, on Linux** — it was
  killed with the terminal tab that started it, and came back at the next
  `SessionStart` (a new session, a resume, a `/clear`, or an auto-compact, which
  is why it can look like it returns when a long task finishes). Fixed by
  launching into a scope of the panel's own; if you still see it, check whether
  the launcher had to fall back — `data/cost-meter.log` says
  `launch: scope spawn exited immediately` when it did — and compare
  `systemctl --user status "$(cat data/widget.pid)"` against the tab you are
  typing in. A panel sharing a `ptyxis-spawn-…` or `vte-spawn-…` scope with a
  shell is one that will die with that shell.
- **The panel is invisible, on Linux** — confirm the pixi `widget` task's
  `GDK_BACKEND=x11` took effect and you are on Xorg or XWayland. A pure Wayland
  client cannot position itself in a corner or raise itself above other windows,
  so under plain Wayland the panel can end up running with no visible effect.
  There is no such setting on Windows and none is wanted: GDK's only backend
  there is win32, and forcing x11 would leave the panel no display to open.
- **Nothing happens at session start** — read `data/cost-meter.log` first. The
  launcher exits 0 and prints nothing whatever happens, deliberately, so the log
  is the only place that distinguishes the three cases: `launch: spawned` (it
  started something, and the problem is further along), `launch: already
  running` (it decided a panel was up), and `launch: failed` with the exception.
  **No line at all means the hook never ran** — check that `SessionStart` in
  `~/.claude/settings.json` carries its `matcher`, and that its `command` is a
  quoted forward-slash path rather than a native Windows one. A backslash path
  is eaten by the shell Claude Code runs hooks in, which reports
  `command not found` where only the session transcript can see it. Re-run
  `pixi run install-hooks` for either; it recognises and replaces the old
  entry rather than adding a second one.
  On Windows the other usual cause is `pixi` not being on `PATH` for the hook,
  because Claude Code was started before pixi was installed and a `PATH` change
  does not reach a running process. Restart Claude Code. That one also leaves no
  log line, and you can tell the two apart by running the wrapper by hand:
  `launch_widget.cmd` writes a line if it reaches Python, and returns in well
  under a second without one if it never found pixi.
- **A black console window next to the panel, on Windows** — the panel is being
  run by `python.exe` instead of `pythonw.exe`. Check the `widget` task under
  `[target.win-64.tasks]` in `pixi.toml`. A detached process has no console to
  inherit, and Windows gives any console-subsystem program one of its own.
- **A taskbar button for the panel, on Windows** — the `UTILITY` type hint in
  `widget.py` is not reaching the window. `pixi run test` says so directly:
  `test_windows_gives_it_no_taskbar_button` reads the real window back and fails
  if it is not a tool window. Note that changing the hint to the more accurate
  `DOCK` reintroduces exactly this, since win32 gives `DOCK` no tool-window
  style.

## Development

```bash
pixi run test     # unit tests only, against a throwaway data directory
pixi run smoke    # tests, a real GTK render, and the fault-logging check
```

`pixi run smoke` skips the render step, out loud, when there is no display, so
it stays usable over SSH. `xvfb-run pixi run smoke` exercises it anyway, using
your distribution's Xvfb — conda-forge has no Xvfb package, so it is not one of
this project's dependencies. On Windows there is always a display, so the step
always runs.

Both tasks are Python (`run_tests.py`, `smoke.py`) rather than shell, so the two
platforms run the same check rather than a pair of twins that can drift.
