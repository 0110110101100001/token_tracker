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

**Handled -- but worth reading, because the handling is a workaround and not a
fix.** On Windows with Smart App Control enforcing, one file in the GTK stack is
blocked and the panel cannot start without it. `pixi.toml` pins around that file,
so a clone and `pixi install` gets you a working panel with nothing to configure.
Verified from an empty `.pixi/` on a machine with Smart App Control On: GTK
imports, `--selftest` renders, the panel comes up and holds its lock.

What it works around: Smart App Control blocks code it has no reputation for, and
it decides per *file*. The current conda-forge build of `epoxy-0.dll` has none;
GDK links libepoxy and cannot load without it, so the panel used to die on
`import gi` before drawing anything. It is that one file and no more -- measured,
`gtk-3-0`, `gio`, `glib`, `pango`, `cairo` and `gdk_pixbuf` all load fine.

The pin is an older build revision of the same library, which Smart App Control
does trust:

```toml
[target.win-64.dependencies]
epoxy = { version = "==1.5.10", build = "*_1" }
```

1.5.10 is the newest upstream libepoxy, so `_0`, `_1` and `_2` are one source
built three times. The pin therefore costs no library fixes, only a newer
toolchain, and being under `target.win-64` it costs the other platforms nothing.

**It rests on a reputation verdict, not a guarantee.** If some future Windows
stops trusting this build too, the panel stops starting again -- but it will say
so now: `data/widget-output.log` gets the `AssertionError` from
`gi/overrides/Gdk.py`, with the blocked file named in the line above it. The
remedy is another build revision; `pixi search epoxy --platform win-64` lists
them.

One thing here is deliberately left broken. `_ctypes.pyd` is blocked as well, and
`tests/test_widget.py` imports `ctypes` to assert Windows window styles through
`user32` -- so on a Smart App Control machine `pixi run smoke` reports one error
and the panel's own tests do not run. The panel does not care: `widget.py` uses no
`ctypes` at all. **Do not fix it the way the epoxy pin was done.** A `_ctypes.pyd`
taken from another Python build is linked against a different libffi (`>=3.7.0`
against this environment's `>=3.5.2`, behind the same `ffi-8.dll` soname). It
loads, it works, and it corrupts the heap: the test run then segfaults inside the
garbage collector, in a different innocent-looking place each time. That is a
worse thing to own than a test suite that will not start. The difference between
the two swaps is the whole lesson -- epoxy is another build of the *same version*
with the same ABI; that one was built against a different version of its own
dependency.

It bites machines where nothing changed, because Smart App Control turns itself
on and the registry is not what enforces it. It ships in an evaluation mode that
only watches, and Windows promotes it to enforcing by itself once it judges the
machine compatible -- no prompt. But the kernel enforces the policy it loaded at
**boot**, so the promotion sits in the registry doing nothing until the next
restart.

That gap is why this looks impossible when it happens. On the machine this was
diagnosed on, the registry recorded the promotion at 14:19 and the panel was
still demonstrably alive at 14:35 the same day -- `launch: already running`, which
only a panel holding `widget.lock` can produce. It stayed working for two weeks,
across no restarts, and died on the first launch after the next reboot. Nothing
in this project moved in between: `pixi.lock` was untouched, so the bytes being
blocked were the same bytes that had loaded fine.

Two weeks is not the surprise it looks like, because with Fast Startup on --
`HiberbootEnabled` under `Session Manager\Power`, the Windows default -- shutting
the machine down and switching it on again is a resume from hibernation, not a
boot, and resuming does not reload the policy. Only a real cold boot does.
`Get-WinEvent -ProviderName Microsoft-Windows-Kernel-Boot -FilterXPath "*[System[EventID=27]]"`
tells the two apart: boot type `0x0` is a cold boot, `0x1` and `0x2` are resumes.
So a promotion can sit dormant through however many apparent shutdowns, and land
on whichever cold boot comes first. That is also why blaming the Windows update
sitting next to it in the timeline is usually wrong -- on this machine the updates
installed twenty minutes *after* the cold boot that started the blocking, and one
of them was still waiting for a restart of its own.

And do not look for the promotion in your update history, because it is not an
update. Nothing was installed at the minute it was written: the `Setup` log for
that whole day holds one entry, a package reaching `Staged` ten minutes later, and
the previous actual install was three weeks earlier. Smart App Control was already
on the machine, watching; what arrived was its own decision to start enforcing,
which it wrote to the registry itself. There is no package to identify and none to
remove.

To see where you stand, in PowerShell:

```powershell
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' |
  Select-Object VerifiedAndReputablePolicyState, SAC_PreviousState
```

`VerifiedAndReputablePolicyState` is `0` off, `1` enforcing, `2` evaluating. A `1`
beside a `SAC_PreviousState` of `2` is a machine that promoted itself rather than
one somebody configured.

If the pin above ever stops working, there are two fallbacks, in this order.

Turning Smart App Control off, in Windows Security under **App & browser
control**, makes all of this go away. **It is also a one-way door** -- it can be
turned back on only by reinstalling Windows -- so it is your call, and not one
this project will make for you.

Failing that, Smart App Control never blocked the environment wholesale, and a
panel on a different toolkit would not need the blocked file at all. Measured in
the same pixi environment and the same interpreter, with Smart App Control
enforcing:

| | under enforcing Smart App Control |
| --- | --- |
| GTK 3 (`epoxy-0.dll`, and so `gdk-3-0.dll`) | blocked |
| `_ctypes` | blocked |
| `_tkinter`, Tcl/Tk 8.6, a real `-topmost` window | **loads and draws** |
| `ssl`, `socket`, `sqlite3`, `zlib`, `lzma`, `_hashlib` | loads |

So a panel built on Tkinter rather than GTK would run here untouched, and
`widget.py` uses no `ctypes` at all -- only `tests/test_widget.py` does, to assert
Windows window styles through `user32`. That port is not written; this table is
recorded so whoever considers it does not have to rediscover which half of the
environment still works.

Until then, leaving Smart App Control on and keeping the `Stop` hook for the
figures is a perfectly good answer: every dollar row is standard library only, so
the numbers keep accruing with no panel to show them, and `/usage` reads them
back.

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
