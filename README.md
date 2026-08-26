# Claude cost meter

Always-on-top GTK panel that shows what your Claude Code work
costs. It refreshes itself after every assistant turn, driven by a `Stop`
hook — you never have to run anything by hand for the numbers to move.

It runs on **Linux and Windows**. Its **dollar figures** count only the Claude
Code work done on the machine it runs on, which is the first thing to understand
about it; its **limit percentages** come from the server and cover the whole
account — see [What it counts](docs/METERING.md#what-it-counts).

This file is the commands you need day to day: installing, autostart, keeping
the panel closed, uninstalling. Everything else is next door:

- **[What it counts, and how](docs/METERING.md)** — what each figure is
  measuring, where the limit percentages come from, `pricing.json`, and the
  known limitations.
- **[The panel](docs/PANEL.md)** — the rows on screen and what they mean, why
  the figures roll, moving and resizing it, and what the graphical session has
  to provide.
- **[Reference](docs/REFERENCE.md)** — what the installer writes into
  `~/.claude/settings.json`, starting and stopping the panel by hand, every file
  under `data/`, the `config.json` settings, troubleshooting, and the tests.

## Known issue in this version: the Claude Desktop app

**The two limit percentages keep themselves up to date only if Claude Code is run
in a terminal.** The panel's fetch needs a token that only a terminal writes; the
Claude Desktop app keeps its own elsewhere. In the desktop app `/usage` still
refreshes the percentages once, but they stand still again afterwards. The dollar
rows are unaffected. Detail in
[Where the limit figures come from](docs/METERING.md#where-the-limit-figures-come-from). Similar issue is for billing row (API/team)

## Known issue in this version: Windows Smart App Control

**On Windows with Smart App Control enforcing, `epoxy-0.dll` is blocked and the
panel dies on `import gi`** — `pixi.toml` pins `epoxy` on win-64 to a build
revision it trusts, so if the panel stops starting again, read
`data/widget-output.log` and move that pin (`pixi search epoxy --platform
win-64`).

## Install

You need [pixi](https://pixi.sh) and Claude Code. Nothing else has to be
installed system-wide: pixi supplies Python and the GTK 3 bindings itself, so in
particular you do **not** need a distribution's `python3-gi`.

### Getting pixi

If you do not have it yet, one command installs it, for your user only — no root,
nothing system-wide. On **Linux**:

```bash
curl -fsSL https://pixi.sh/install.sh | sh
```

On **Windows**, in PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm -useb https://pixi.sh/install.ps1 | iex"
```

Either one drops pixi into your home directory — `~/.pixi/bin`, or
`%USERPROFILE%\.pixi\bin` — and adds that to `PATH`. The `PATH` change reaches
only shells started afterwards, so open a new terminal and check it took:

```bash
pixi --version
```

If that says *command not found*, the `PATH` line landed in a startup file your
shell does not read; add `~/.pixi/bin` to `PATH` yourself. A package manager
works just as well if you prefer one: `winget install prefix-dev.pixi`, or
`cargo install --locked pixi`.

### The project

**Linux** — any normal desktop session, Xorg or Wayland. A Wayland session runs
the panel through XWayland, which every mainstream desktop already ships, so
there is nothing to install or switch on for the display either; the one setup
that fails is a compositor deliberately running without XWayland, where the panel
comes up with
[no visible effect](docs/PANEL.md#what-the-graphical-session-has-to-provide).
Four commands:

```bash
git clone <this repo> && cd token_calculator
pixi install                # solve and fetch Python + GTK 3 (a few hundred MB)
pixi run install-hooks      # register the two hooks in ~/.claude/settings.json
pixi run smoke              # prove it works before relying on it
```

**Windows** — 10 or newer, 64-bit. The same four commands, in PowerShell or
`cmd`:

```powershell
git clone <this repo>
cd token_calculator
pixi install
pixi run install-hooks
pixi run smoke
```

Then **restart Claude Code** and start a new session. The panel comes up on its
own, and the numbers start moving after your first assistant turn.

Then run
```bash
/usage  # in claude session
```

once in a **terminal** Claude Code session, even if you work in the Claude
Desktop app — especially then. Starting that session writes the token the panel's
own fetch needs, and `/usage` fills the limit percentages in straight away rather
than leaving the **5h window** and **week** rows on dollars until the first fetch
lands. Repeat it occasionally if the terminal is not where you normally work: see
[the known issue](#known-issue-in-this-version-the-claude-desktop-app).

On Windows that restart is not optional, and skipping it is the one failure that
looks like nothing happening at all: the hooks have to find `pixi` on `PATH`, and
a `PATH` change does not reach a process that was already running when the change
was made. So the hooks run, find no pixi, and exit 0 exactly as they are
designed to. On Linux you only need it if you installed pixi from the same shell
Claude Code was started from.

Optionally, also bring the panel up on login rather than only per Claude Code
session:

```bash
./install.sh --autostart      # Linux;  install.cmd --autostart on Windows
```

There is **no macOS support**. Nothing here is hostile to it any more — the file
lock is portable and the entry points are Python — but no `osx-*` platform has
been solved or smoke-tested, so it is not claimed.

What the installer writes, and why the hook paths look the way they do, is in
[Install, in detail](docs/REFERENCE.md#install-in-detail).

There is nothing to set up for the **limit percentages**. The **5h window** and
**week** rows show the account's own figures — the same ones `/usage` prints —
and the panel asks the server for them itself, once a minute. No calibration,
and nothing to redo when your plan changes or a promotion moves the ceiling.

## Keep the panel closed

Close the panel with right click → *Quit*. By default, though, every Claude Code
session opens it again if one is not already up — including a panel you closed on
purpose ten minutes earlier. To make closing it stick:

```bash
pixi run autolaunch -- --off      # sessions stop opening the panel
pixi run autolaunch -- --on       # they open it again
pixi run autolaunch -- --status   # which of the two is in force
```

The panel's own right-click menu carries the same toggle, as **Pause
auto-launch** / **Resume auto-launch**.

Pausing suspends the launch and nothing else. **Recording continues** — the
`Stop` hook still prices every turn, so a paused week leaves no gap in the
figures and they are all there when you bring the panel back.

**To open the panel while it stays paused:**

```bash
cd token_calculator
pixi run start
```

That is the command for exactly this: it opens a panel whatever the flag says,
leaves the flag alone, and detaches, so the panel outlives the shell you typed
it in. The other ways to start and stop one by hand are under
[Starting and stopping](docs/REFERENCE.md#starting-and-stopping).

## Uninstall

```bash
cd token_calculator
./install.sh --uninstall      # Linux;  install.cmd --uninstall on Windows
```

That removes both hooks from `~/.claude/settings.json` and the autostart entry
if you made one. Your ledger is untouched — delete `data/` as well for a full
reset (see [Files](docs/REFERENCE.md#files)).
