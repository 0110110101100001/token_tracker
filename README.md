# Claude cost meter

A bottom-right, always-on-top GTK panel that shows what your Claude Code work
costs. It refreshes itself after every assistant turn, driven by a `Stop`
hook — you never have to run anything by hand for the numbers to move.

## What it is

The panel is a small borderless window anchored to the bottom-right corner of
your screen. It shows six rows:

- **last turn** — USD cost of the assistant turn that just finished
- **session** — USD cost of the current Claude Code session
- **today** — USD cost since local midnight
- *(separator)*
- **5h window** — USD spent in the trailing 5 hours, with a `%` of your
  calibrated 5-hour subscription limit once calibrated
- **week** — USD spent in the trailing 7 days, with a `%` of your calibrated
  weekly limit once calibrated

A seventh row appears only when needed. It carries two kinds of warning:

- `! stale 1 h 37 min` — `state.json` has not been rewritten for over ten
  minutes, so the figures above it are that old. Every value row is greyed
  out at the same time, including the limit rows, which lose their
  green/amber/red colour. This is what a broken `Stop` hook looks like: the
  hook always exits 0 by design, so without this marker the panel would keep
  showing hours-old numbers as though they were current. It also shows up
  after a genuinely idle stretch, which is honest — the numbers really are
  ten minutes old.
- `? claude-something` — a model with no entry in `pricing.json`, so an
  unpriced model is visible instead of silently costing nothing.

Cost is computed from the same token counts Claude Code already writes to
its own transcript files under `~/.claude/projects/`; the tool reads those,
prices each assistant message, and keeps a rolling ledger of the result.

## Requirements

- **Linux or Windows, with a graphical session.**
  - On **Linux** the panel runs as an X11 client, under Xorg or XWayland. It
    has to position itself in a corner and raise itself above other windows,
    which a pure Wayland client may not do.
  - On **Windows** (10 or newer, 64-bit) it runs on GDK's win32 backend, which
    needs no such coaxing.
  - There is **no macOS support**. Nothing here is hostile to it any more —
    the file lock is portable and the entry points are Python — but no `osx-*`
    platform has been solved or smoke-tested, so it is not claimed.
- **[pixi](https://pixi.sh).** It supplies everything else, including Python
  and the GTK 3 bindings. Nothing has to be installed system-wide — in
  particular you do **not** need a distribution's `python3-gi`.
- **Claude Code**, since the whole input is its transcripts.

## Install

```bash
git clone <this repo> && cd token_calculator
pixi install                # solve and fetch Python + GTK 3 (a few hundred MB)
pixi run install-hooks      # register the two hooks in ~/.claude/settings.json
pixi run smoke              # prove it works before relying on it
```

Those four commands are the same on Linux and Windows.

**On Windows, restart Claude Code after installing pixi** — before the hooks
can run `pixi`, it has to be on `PATH`, and a `PATH` change does not reach
processes that were already running when it was made. Skipping this is the one
failure that looks like nothing happening at all: the hooks run, find no `pixi`,
and exit 0 exactly as they are designed to.

Then start a new Claude Code session. The panel comes up on its own, and the
numbers start moving after your first assistant turn.

`install-hooks` edits `~/.claude/settings.json` for you because the hooks must
be registered by absolute path, which is the one thing that cannot be committed
to the repo — it differs on every machine. It backs the file up first, merges
rather than replacing, and is safe to run twice. The result looks like this,
with your own path:

```json
"Stop": [
  {"hooks": [{"type": "command", "command": "<repo>/hooks/tally.sh", "timeout": 20}]}
],
"SessionStart": [
  {
    "matcher": "startup|resume|clear|compact",
    "hooks": [{"type": "command", "command": "<repo>/launch_widget.sh", "timeout": 30}]
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

On Windows the very same entries name `hooks\tally.cmd` and
`launch_widget.cmd`, because Claude Code runs the registered command directly
and a `.sh` is not executable there. The installer picks the pair that matches
the platform it is run on; what the two wrap is identical, being the same pixi
tasks.

Two flags worth knowing:

```bash
./install.sh --autostart    # also start the panel on login, not just per session
./install.sh --uninstall    # remove both hooks and the autostart entry again
```

On Windows that is `install.cmd --autostart` and `install.cmd --uninstall`.
`--autostart` writes an XDG `.desktop` entry on Linux and a `.cmd` in your
Startup folder on Windows; `--uninstall` removes it again, but only after
reading it back and confirming it launches *this* checkout.

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
pixi run widget       # in the foreground
pixi run launch       # detached, and only if one is not already up
```

On Linux `pixi run widget` is also how you see the panel's errors. On Windows it
is not — the task runs `pythonw`, which has nowhere to write them (see below);
use `pixi run python widget.py` when you want the output.

`pixi run launch` is what the SessionStart hook ends up running, through
`launch_widget.sh` or `launch_widget.cmd`. `run_widget.sh` / `run_widget.cmd`
start the panel in the foreground from outside the pixi environment, which is
what an autostart entry wants.

On Windows the panel runs under `pythonw.exe`, not `python.exe`. The hook spawns
it detached, with no console to inherit, and Windows answers that by allocating
a brand new console for any console-subsystem program — so `python` would leave
a black window and a `conhost.exe` on screen beside the panel for its whole life.
`pythonw` is the same interpreter built as a GUI-subsystem binary. The cost is
that it discards stdout and stderr, which is why `pixi run widget --selftest`
still goes through `python`.

Stop it with right click → *Quit*.

A running panel holds an exclusive lock on `data/widget.lock` for as long as it
lives, and the launcher starts one only when it can take that lock itself. So
closing the panel keeps it closed for the rest of the session, and the next
session brings it back. The lock rather than a pid, because the kernel retracts
it however the panel dies: a pid file outlives a hard kill, and Windows will
hand that same number to something else, which used to suppress the launch in
every session afterwards.

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

## Calibration

Claude Code's subscription limit state (the percentages `/usage` reports)
is not stored anywhere locally, so this tool cannot read it — only estimate
it against your own spend. Before calibration, the **5h window** and **week**
rows show dollar amounts only, with no percentage: showing no number is the
honest answer when the real ceiling is unknown, and inventing one would be
worse than showing nothing.

To calibrate:

1. Run `/usage` inside Claude Code and read the percentage it reports for
   each window.
2. Feed those percentages back in:

   ```bash
   pixi run calibrate -- --5h <pct>
   pixi run calibrate -- --week <pct>
   ```

   The bare `--` matters: without it pixi reads `--5h` as one of its own flags.

   Each flag derives a ceiling from your currently-recorded spend divided by
   the reported percentage, and stores it in `data/config.json`. From then
   on the corresponding row shows a `%` alongside the dollar figure.

Spend is measured in USD-equivalent rather than raw tokens because the
models draw on the subscription limit unevenly — an Opus token costs the
limit more than a Sonnet token — and pricing already carries those per-model
weights, so converting through USD folds that difference in automatically
instead of requiring a second, separate weighting scheme.

## Pricing

`pricing.json`, at the repo root, carries the current published rate per
million tokens (input and output) for each model you use. Edit it directly
whenever Anthropic changes its rates.

One rate in that table has a known expiry date: `claude-sonnet-5` is priced
at its **introductory** rate of $2.00 / $10.00, which is in force only
through **2026-08-31**. From 2026-09-01 the sticker price of $3.00 / $15.00
applies and the two numbers need editing by hand. Nothing in the tool tracks
this — there is deliberately no dated-override mechanism, so the table says
what you tell it and the calendar is yours to watch.

The lookup is an **exact string match** against whatever the transcript
records — there is no alias resolution, and none is wanted: guessing that
`claude-haiku-4-5-20251001` means the same thing as `claude-haiku-4-5` is
exactly the kind of inference that would silently misprice a model one day.
Claude Code is not consistent about which form it writes, which is why Haiku
4.5 appears twice at the same published rate: the dated id is what the
transcripts carry today, and the bare alias is there so the panel keeps
pricing it rather than falling back to a `?` if that ever changes.

A model that has no entry in this table is **never** treated as free. It is
excluded from the priced totals and instead surfaces as a `?` warning row on
the panel, so a pricing gap is visible rather than silently undercounting
your spend.

## Known limitations

- **USD here is an API-equivalent, not an invoice.** The account this tool
  was built for is on a subscription, not pay-per-token API billing — the
  dollar figures are a consistent way to compare and weight usage, not a
  bill you will actually receive.
- **Fast mode is priced as though it were standard.** The transcripts do record
  which speed served each message, in `usage.speed`, but nothing here reads that
  field and `pricing.json` carries no fast-mode rates. Since Opus 5 fast mode
  costs $10.00 / $50.00 against the standard $5.00 / $25.00, a fast turn would
  be understated by half. Fast mode is not currently in use — every record in
  the transcripts reads `standard` — so this costs nothing today, but it would
  if that changed.
- **The weekly row is a rolling 7 days**, not the fixed weekly window the
  real subscription limit resets on. This reads slightly pessimistic:
  the tool's week can include a leading tail of usage the real limit has
  already reset past.
- **Server-side tool use is not counted.** Web search is billed per thousand
  searches rather than per token, and those counts (`usage.server_tool_use`)
  are ignored. They are zero across every transcript on the machine this was
  built for.
- **`/usage` remains the authoritative source for limit state.** Everything
  this tool shows for the 5h and week windows is an estimate calibrated
  against your own reported spend, not a read of the real limit.

## Files

Everything runtime-owned lives under `data/`, and is safe to delete wholesale
(see below):

- `events.jsonl` — append-only ledger of priced messages, pruned to the
  trailing 8 days.
- `state.json` — the numbers the widget actually reads and displays.
- `offsets.json` — per-transcript file read positions, so re-running the
  scan never re-reads or double-counts a line.
- `session_marks.json` — one bookmark per session recording the newest event
  already reported as that session's **last turn**. This is what keeps the
  row correct when several Claude Code sessions run at once: whichever hook
  fires first picks up every session's new messages, so "new since my own
  last run" has to be remembered per session rather than inferred from which
  run happened to append the events. Bookmarks for sessions idle longer than
  the 8-day prune window are dropped.
- `config.json` — calibrated ceilings (`ceiling_5h_usd`, `ceiling_7d_usd`)
  and the widget's last window position.
- `widget.lock` — held exclusively by the running panel for as long as it
  lives. This is how the launcher tells whether one is already up; the kernel
  drops it however the panel dies, including a hard kill.
- `widget.pid` — the running panel's pid, so a stuck one can be found and
  killed. Diagnostic only: a pid cannot answer the liveness question honestly on
  Windows, which reuses the numbers.
- `cost-meter.log` — where the `Stop` hook, the `SessionStart` hook and
  `calibrate.py` record what they swallowed to keep your critical path
  unbroken. The launcher also logs the boring outcomes (`launch: spawned`,
  `launch: already running`), because a hook that ran and decided to do nothing
  and a hook that never ran are otherwise indistinguishable.

Deleting `data/` entirely is a safe full reset: the next run rebuilds
`events.jsonl` and `state.json` from the real transcripts under
`~/.claude/projects/` from scratch. You lose your calibrated ceilings and
the saved window position, nothing else — re-run calibration to get the
percentages back. With the bookmarks gone too, the first turn after a reset
reads as the whole session's cost, since there is no earlier mark to measure
from; it corrects itself on the next turn.

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

## Troubleshooting

- **The widget stops updating** — the panel says so itself: every value row
  greys out and the warning row shows how long it has been (`! stale 1 h
  37 min`). Check `data/cost-meter.log`. Every fault on the `Stop` hook's
  critical path is caught and logged there rather than shown, by design, so a
  stuck panel almost always has a line waiting there. If the log is empty, the
  hook is not reaching Python at all: run `hooks/tally.sh` (or `hooks\tally.cmd`)
  by hand and check that `pixi run --frozen tally` works from the repo.
- **Numbers look lower than expected** — check the panel for a `?` warning
  row. It means at least one model you used has no entry in `pricing.json`
  and is being excluded from the totals rather than counted as zero.
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
  `~/.claude/settings.json` carries its `matcher`, and re-run
  `pixi run install-hooks` if it does not.
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
