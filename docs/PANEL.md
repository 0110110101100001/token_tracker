# The panel

What the window on screen shows, why it looks and behaves the way it does, and
what the graphical session has to provide for it to appear at all. What the
figures *mean* is in [What it counts, and how](METERING.md).

← [README](../README.md) · [Metering](METERING.md) · [Reference](REFERENCE.md)

## What it is

The panel is a small borderless window anchored to the bottom-right corner of
your screen. It shows these rows:

- **last turn** — USD cost of the assistant turn that just finished
- **session** — USD cost of the current Claude Code session
- **today** — USD cost since local midnight
- *(separator)*
- **5h window** — how much of the account's 5-hour limit is used, as a floor:
  `≈12 % · 18:30`, where `18:30` is the clock time that block resets. Hover for
  what this machine spent in the block. `$67.67` alone means no account figure was
  available.
- **week** — the same for the weekly limit: `≈17 % · Sat 02:59`. The weekday
  appears because that reset is days out, where a bare `02:59` would read as
  tonight.
- **this machine** — USD this installation put into that same week: `$1000.00`.
  It sits directly under the percentage because the two describe one window by
  different measures — the account's share of its limit, and your own
  contribution to it. It empties when the percentage does, since both are bounded
  by the reset the server reports.
- *(separator)*
- **billing** — how this session is paying: `team · max 5x` for a seat, `API`
  when it is billed per token, `—` when neither could be established

The 5-hour limit is a **fixed block, not a trailing five hours**. A block opens
on the first message sent after the previous one expired and runs five hours from
that message, which is why `/usage` names a reset time instead of counting down
continuously. The server tells the panel when the open block ends, and the
tooltip's dollar figure is bounded by that same window, so the two halves of the
row describe the same five hours.

That matters more than it sounds: left to guess, this installation would open the
block on its own first message, and a block opened on another machine — or in the
browser — began earlier than anything it can see. Taking the boundary from the
server removes the guess.

The reset time rides with the percentage, as it always has: what it is *for* is
saying which window the figure describes. When that time passes, the row is
withdrawn rather than left standing — the window it described is gone, and no
floor survives it — and the row shows dollars alone until the next figure arrives.

Only the percentage carries the green/amber/red colour, and the colour comes from
the server's own severity rather than from thresholds compiled in here. The
thresholds move: this account is currently carrying a +50 % weekly promotion,
which no hardcoded number would follow.

One more row appears only when needed. It carries two kinds of warning:

- `! stale 1 h 37 min` — `state.json` has not been rewritten for over ten
  minutes, so the **dollar** figures are that old, and every dollar row greys
  out with the marker. This is what a broken `Stop` hook looks like: the hook
  always exits 0 by design, so without this marker the panel would keep showing
  hours-old numbers as though they were current. It also shows up after a
  genuinely idle stretch, which is honest — the numbers really are ten minutes
  old.

  **The limit rows keep their colour**, because nothing about them went stale:
  they come from the server on the panel's own poll, so a dead hook does not age a
  percentage by a second. Their own freshness is answered separately — the age is
  in the tooltip, and a row whose window has reset is withdrawn outright. A limit
  row does grey out when there is no account figure at all, but then it is showing
  dollars like the rest.
- `? claude-something` — a model with no entry in
  [`pricing.json`](METERING.md#pricing), so an unpriced model is visible instead
  of silently costing nothing.

Cost is computed from the same token counts Claude Code already writes to
its own transcript files under `~/.claude/projects/`; the tool reads those,
prices each assistant message, and keeps a rolling ledger of the result.

How the **billing** row is decided, and why it cannot be backfilled, is under
[The billing row](METERING.md#the-billing-row).

## Rolling figures

The three dollar rows roll to their new figure rather than snapping to it: slow
start, fast middle, slow settle. It is there so a turn that cost 4 USD and a turn
that cost 0.04 USD, stop looking alike; without it the number is simply different
next time you glance at it, and nothing draws the eye to the fact that anything
was added.

The two cumulative rows — **session** and **today** — roll from their previous
total. **last turn** is a delta rather than a running
total, so it counts up **from zero** on every turn instead: the distance between
what this turn cost and what the previous one cost is not a quantity anybody is
watching, and rolling between the two would run the row *downwards* to announce
a cheap turn after an expensive one. Counting from zero also means the figures
on the way up mean something on their own — what this turn has cost so far. A
turn that costs the same as the one before it still counts up again, because two
turns costing the same cent are still two turns; what marks a turn as new is
`state.json` being rewritten, not the figure changing.

The roll lasts **one second plus 25 ms for every dollar it covers**, so $40
added takes two seconds and a few cents take barely more than the base second.
A fixed duration would make the expensive turn — the one worth watching — the
one that blurs past fastest. The length is set from the longest of the three
rows, since they share a clock. Nothing about the row dims or fades while it
moves: only the figure changes.

What deliberately does **not** roll:

- **the two limit rows.** There is nothing to tween between `≈11 %` and `≈12 %`:
  the server reports whole percentages, so the row steps however often it is
  fetched. They are repainted when the figure changes and left alone in between.
- **any dollar row, while the state is stale.** Stale figures are not being
  presented as current, and rolling them would say the opposite. They land on
  their last known values and stay put.
- **the first paint.** Startup is not a change, and rolling up from zero would
  claim one that never happened.

Only genuinely new work animates, so the 60-second staleness poll re-reading the
same `state.json` does not re-run the animation, and a move smaller than a cent
is set outright. Nothing about the row's size changes, only the value — a
changing row height would re-anchor the whole panel on every turn.

## Patrik mode

Right click the panel and pick **Patrik mode**. Nothing happens straight away —
the next turn to land throws money glyphs out of the table and gives the panel a
flinch. It stays on until you pick **Patrik mode off**, and it survives a
restart, because it is saved in `data/config.json` alongside the position and the
scale.

A celebration lasts **two seconds plus 50 ms for every dollar the turn cost**, so
a few cents runs the base two seconds and a $40 turn runs four. That is the same
shape as the rolling figures and deliberately twice their numbers, so the rows
finish counting up around halfway through the glyphs and the two read as one
event. The panel's flinch takes the first 40 % of whatever that length is, and
wobbles at the same speed however long it runs — stretched to a fixed number of
cycles it would become a slow sway, which reads as the panel leaning about rather
than reacting.

Glyphs keep arriving for the whole of it, not just at the start: a handful lands
the instant the turn does, and about nine a second follow, so roughly 28 glyphs
cross a two-second celebration and 46 a four-second one. A single opening burst
was the first version, and on a long animation it left most of the time to glyphs
merely falling — the celebration visibly ran out before it ended. Each glyph is
given only the time that is left, so the animation really does finish when it
said it would; because a glyph fades across its own lifetime, a late one fades
quickly rather than blinking out. The spray stops a third of a second before the
end, where a new glyph would be a speck appearing and vanishing on the panel.

**How fast they arrive is the session's total, not the turn's.** Nine a second up
to $10, then eleven, thirteen at $20 and fifteen from $30 on. The turn already has
its say in the length, and the two answer different questions — how big was that,
against how deep are we in. The steps are steps rather than a slope because a rate
creeping up by the cent is a change nobody can see between one turn and the next,
and they stop at $30 because an unbounded rate would fill the overlay faster than
the glyphs could leave it.

**The glyphs are sized by the panel.** They are 13–24 px on a panel at its
shipped size and scale with it from there, so dragging the panel to three times
the size gets a spray three times the size rather than the same fixed speck
sitting on top of a much bigger meter. The size is read on every frame, so a drag
in the middle of a celebration reaches the glyphs still to come; the ones already
in the air keep the size they were thrown at, since re-sizing them mid-flight
would be the whole burst twitching at once.

Every glyph *starts* somewhere inside the panel, whatever the scale — it is money
leaving the table, and one that appeared out in the margin would belong to
nothing. Leaving is the point, and they are thrown up and pulled down so each one
is certain to cross an edge.

It is off until you ask for it, and only a genuinely new turn sets it off.
`refresh()` also runs from the file monitor, the 60-second staleness poll and
**Refresh now**, and none of those is a charge — a burst driven by anything but
`updated_at` moving would spray the panel every minute. The panel's own opening
refresh is skipped too: the figure already on disk at startup has not just been
charged, and auto-launch opens a panel at every session.

The glyphs are drawn in a second, transparent, always-on-top window covering the
panel and a wide margin round it, because the whole point is that they leave the
table rather than fading out inside it. That window ignores the mouse entirely,
so clicks in it reach the panel or the desktop underneath as though it were not
there.

**On a machine that cannot composite** — no RGBA visual, or no compositor — there
is no overlay and no glyphs, and the meter carries on exactly as before. An
opaque grey slab over the desktop would be far worse than no celebration, and
`data/widget-output.log` records the reason a burst never appeared.

The glyphs are 🤑, 💲, 💰 and 💵. 🪙 is the obvious fifth and is deliberately
absent: Segoe UI Emoji on Windows 10 has no glyph for it, so it draws as an empty
box.

## Sound

Right click the panel and pick **Set sound on**. From the next turn on, each one
plays a short sound, and *which* sound is what that turn cost — the figure on the
top row, not the session's running total: one tone under $10, two from $10, three
from $20 and four from $30. It stays on until you pick **Set sound off**, and it
survives a restart, in `data/config.json` under `sound`.

The turn rather than the session, because the sound answers "what did that one
cost" and the person who just pressed enter is waiting on exactly that figure. A
session total only climbs, so keyed to it the sound would ratchet up once and then
say the same thing about every turn until midnight, a two-cent one included. How
deep the day is already in has its own channel: the glyph rate, which does read
the session, and still steps on these same three figures. The numbers agreeing is
now a coincidence rather than a coupling, and nothing asserts it — rescaling one
is a change to that one alone.

Worth knowing before you turn it on: on real traffic the top two files are rare.
Turns here run around $0.50–$1.70 median depending on where you draw the boundary,
and fewer than one turn in a hundred reaches $10. Under $10 is the everyday sound
and $30 is close to a jackpot.

It is its own switch rather than part of Patrik mode, and that is the point:
glyphs play on your own screen and a sound plays in whatever room you are sitting
in. Either can be on without the other.

The same gating as the glyphs, and for the same reasons. Only a genuinely new
turn plays — the file monitor, the staleness poll and **Refresh now** all re-read
a `state.json` that has not changed, and a sound on those would be a panel
beeping every minute all day. The panel's own opening refresh is skipped too,
because auto-launch opens a panel at every session start and the figure already
on disk has not just been charged.

The four files live in `sounds/`. Two of them are placeholders: `pixi run sounds`
regenerates those from `cost_meter/sound.py`, which synthesises plain sine tones
with the standard library. `under-10.wav` and `over-10.wav` are not — they are
real recordings, a slot machine and a cash register, and both are a good deal
longer than the tones they sit beside.

They are a trial rather than a settled design, and it is worth knowing which way
they cut. The lengths no longer climb with the tiers: 3.5s under $10, 1.5s from
$10, then 0.36s and 0.48s for the two dearest turns. The rising series that
let the four be told apart by ear now runs backwards at the bottom, and the two
cheapest tiers are the loudest thing the panel does. And at 3.5 seconds
`under-10.wav` is no longer over before the next turn can land: turns that come
quickly talk over each other, and on Windows each new one cuts the previous off
where it stands, because `winsound` with `SND_ASYNC` replaces rather than mixes.

**`pixi run sounds` rewrites all four**, both recordings included. It does not
check whether a file is a placeholder before overwriting it, so it will silently
put sine tones back over them. Keep copies outside `sounds/` before running it.

To use your own, drop four WAVs of the same names over them — `under-10.wav`,
`over-10.wav`, `over-20.wav`, `over-30.wav`. WAV is not an
oversight: on Windows the player is `winsound` from the standard library, which
plays nothing else, and the alternative was a GStreamer stack whose every DLL is
another file for Smart App Control to have no opinion about.

On Linux the panel plays through whichever of `paplay`, `pw-play` or `aplay` it
finds first. **Where there is none of them, or no file, there is silence** and a
line in `data/cost-meter.log` — never a stalled panel. The sound is spawned
rather than waited on, so a slow sound server cannot freeze the figures, and it
is the same rule the overlay follows where a screen cannot composite: a
decoration must never take the meter down.

### Credits

`under-10.wav` is cut from *Slot Machine Sound Effects (Sample Pack)* by Played N
Faved — Sound Effects & Stock Footage, released under [CC BY
4.0](https://creativecommons.org/licenses/by/4.0/) and used under it.

`over-10.wav` is *Cash Register Purchase* by Zott820, uploaded to Freesound and
distributed through Pixabay's `freesound_community` account under the [Pixabay
Content License](https://pixabay.com/service/license-summary/). Leading and
trailing silence trimmed, nothing else altered.

The two are not on the same footing, and the difference matters whenever these
files are replaced. Crediting Played N Faved is a *condition* of CC BY: ship
`under-10.wav` without naming them and the feature is out of compliance. The
Pixabay licence asks for no attribution, so the second credit is a courtesy and
a record of where the file came from — worth keeping for whoever wonders later,
but not an obligation.

Anyone who regenerates a file with `pixi run sounds` can drop its credit along
with it: a synthesised sine tone is the project's own.

## Moving and resizing it

Drag the **middle** of the panel to move it. Drag **any edge or corner** to
resize it: the pointer turns into a resize arrow over the 16-pixel band around the
perimeter. Both are saved, and both have their own entry in the right-click
menu — **Reset position** puts the panel back in the bottom-right corner,
**Reset size** puts it back to its original size. The two are separate because
undoing one is rarely a reason to undo the other.

Resizing scales the whole panel — text, padding and width together — rather than
just stretching the frame. The content is a fixed set of rows, so a wider window
on its own would only add blank space around numbers that stayed exactly as small.
The range runs from 0.7× to 3×; a drag that would go past either end stops there.

Pulling any edge **outwards** grows the panel, so up on the top edge and down on
the bottom do what left and right already did. Height is never set directly — the
rows are as tall as the font makes them — but a vertical pull is still a perfectly
good way to say "bigger", which is the single number a resize here produces. A
pixel means the same amount whichever way you pull, and a corner adds both
directions, so a diagonal drag grows about twice as fast as a straight one.

While the panel is still anchored in the corner it grows leftwards and upwards
whichever edge you pull, since the corner owns its position until you move the
panel yourself. Once you have placed it, the edge you are *not* holding stays
where you left it.

## What the graphical session has to provide

The panel is an X11 client on Linux, which is why the pixi task sets
`GDK_BACKEND=x11`: it has to position itself in a corner and raise itself above
other windows, and a pure Wayland client may do neither. Under Xorg or XWayland
that works; under plain Wayland the panel can end up running with no visible
effect. On Windows it uses GDK's win32 backend, which needs no such coaxing —
there is no equivalent setting there and none is wanted, since forcing x11 would
leave the panel no display to open.

## How the panel detaches from what started it

On a systemd Linux desktop the launcher starts the panel inside a transient
scope of its own, via `systemd-run --user --scope`. This is not tidiness, it is
the only thing that actually detaches it: terminal emulators put each tab in a
scope with `KillMode=control-group`, and `setsid` escapes a process group and a
session but never a cgroup, so closing the tab that happened to start the panel
used to take the panel down with it — silently, since the kill is a SIGTERM from
systemd. The panel then reappeared at the next `SessionStart`, which looks from
the outside like it vanishing and returning at random. If the scope will not
start, the launcher logs that and falls back to a plainly detached child, which
is better than no panel. There is no equivalent problem on Windows, where a
`DETACHED_PROCESS` child belongs to nothing that can be closed underneath it.

You can see which scope a panel got with:

```bash
systemctl --user status "$(cat data/widget.pid)"
```

On Windows the panel runs under `pythonw.exe`, not `python.exe`. The hook spawns
it detached, with no console to inherit, and Windows answers that by allocating
a brand new console for any console-subsystem program — so `python` would leave
a black window and a `conhost.exe` on screen beside the panel for its whole life.
`pythonw` is the same interpreter built as a GUI-subsystem binary. The cost is
that it discards stdout and stderr, which is why `pixi run widget --selftest`
still goes through `python`.

The panel keeps itself out of the taskbar (and out of Alt-Tab on Windows) by
asking to be a `UTILITY` window, not by `set_skip_taskbar_hint`, which GDK's
win32 backend accepts and ignores. On X11 the skip hints do the job and are
still set; `tests/test_widget.py` checks the outcome on whichever platform it
runs on rather than trusting either hint.

## The lock that keeps a closed panel closed

A running panel holds an exclusive lock on `data/widget.lock` for as long as it
lives, and the launcher starts one only when it can take that lock itself. So
closing the panel keeps it closed for the rest of the session, and the next
session brings it back. The lock rather than a pid, because the kernel retracts
it however the panel dies: a pid file outlives a hard kill, and Windows will
hand that same number to something else, which used to suppress the launch in
every session afterwards.

Stop the panel with right click → *Quit*. `data/widget.pid` is written beside the
lock, purely so a stuck panel can be found and killed by hand — the commands are
in [Starting and stopping](REFERENCE.md#starting-and-stopping).
