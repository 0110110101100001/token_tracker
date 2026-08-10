# token_calculator — design

Date: 2026-08-10
Status: approved, ready for implementation planning

## Purpose

A small always-on-top desktop widget, anchored bottom-right, that shows what the
current Claude Code work is costing. It updates after every assistant turn.

It answers two questions the user has today no cheap way to answer:

1. How much USD would this session cost at API rates?
2. How close am I to the subscription rate limits?

The user pays for a subscription, so the USD figure is an API-equivalent, not an
invoice. Both numbers are wanted: USD for a sense of scale, limits for control.

## Constraints discovered during design

These are measured facts about this machine, not assumptions.

| Fact | Consequence |
|---|---|
| Transcripts under `~/.claude/projects/**/*.jsonl` carry a per-message `usage` block but **no `costUSD`** | Cost must be computed locally from token counts and a pricing table |
| `usage.cache_creation` splits into `ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` | Cache writes can be priced exactly (1.25x vs 2x input rate) |
| Transcripts total ~153 MB | A full rescan per turn is too slow; parsing must be incremental |
| Some messages carry `"model": "<synthetic>"` (locally injected, 19 occurrences) | They must be excluded from pricing |
| The same `message.id` repeats across streamed chunks | Events must be deduplicated by `message.id` |
| No rate-limit state is stored locally (checked transcripts and `policy-limits.json`) | Subscription limits cannot be read, only estimated against a manual calibration |
| Desktop is GNOME on Wayland | A Wayland client may not position itself or set always-on-top; the widget runs as an X11 client under XWayland instead |
| PyGObject with GTK 3 is present system-wide | No new dependencies are needed |

Models observed across all transcripts: `claude-fable-5` (9388 messages),
`claude-sonnet-5` (2770), `claude-opus-5` (1795), `claude-opus-4-8` (190),
`<synthetic>` (19).

## Architecture

```
Stop hook --> tally.py --> events.jsonl --> state.json --> widget.py
              (incremental) (append-only,   (small        (Gio.FileMonitor)
                             pruned to 8d)   summary)
```

The pipeline is deliberately decoupled at `state.json`. The hook never waits on
the widget, and the widget never parses history. Either side can be absent
without breaking the other: the widget can be closed and reopened at any time,
and the hook keeps writing regardless.

Splitting raw `events.jsonl` from computed `state.json` also keeps the raw
history available for recomputing under a different pricing table, or for ad-hoc
reporting later.

### Location and runtime

All code lives in `~/Desktop/token_calculator/`.

This is deliberately **not** a pixi project, a documented departure from the
standing rule that all projects use pixi. It is tooling for the Claude Code
harness rather than a project, and it runs on the system Python 3 with the
system PyGObject. A pixi environment carrying GTK bindings would add
significant friction for no benefit. The smoke test is `smoke.sh` rather than a
pixi task.

## Components

### `tally.py`

Runs from the `Stop` hook after every assistant turn. This requires registering
a `Stop` entry in `~/.claude/settings.json` alongside the existing `PostToolUse`
hook — the one edit this project makes outside its own folder. The hook receives
the session id on stdin, which becomes the `session` row.

Reads incrementally. `offsets.json` records `{size, mtime, offset}` per
transcript file; each run seeks to the stored offset and reads only new bytes,
so a typical run reads a few kB rather than 153 MB. If a file's current size is
smaller than the stored offset, the file was truncated or rotated and is
reparsed from zero.

For each new line it keeps only entries with `message.usage`, skips
`"model": "<synthetic>"`, and deduplicates by `message.id`.

Appends one compact record per accepted message to `events.jsonl`:

```
[timestamp, session_id, model, input, output, cache_write_5m, cache_write_1h, cache_read]
```

Then recomputes `state.json` and prunes `events.jsonl` to the last 8 days,
which keeps it under roughly 1 MB.

**Concurrency.** Several Claude Code sessions run at once on this machine. Two
turns finishing simultaneously would race on `events.jsonl` and `offsets.json`,
so `tally.py` holds an `fcntl.flock` on a lockfile for the whole run; a second
invocation waits rather than interleaving writes.

**Failure policy.** `tally.py` always exits 0. Every operation is wrapped, and
errors go to `cost-meter.log`. A parsing bug must cost the user a number in the
widget, never the ability to keep working.

### `pricing.json`

Rates in USD per million tokens, current policy only. The user maintains this
file directly as pricing changes.

| Model | input | output |
|---|---|---|
| `claude-fable-5` | 10 | 50 |
| `claude-opus-5` | 5 | 25 |
| `claude-opus-4-8` | 5 | 25 |
| `claude-sonnet-5` | 3 | 15 |

Cache rates derive from the model's input rate: read is 0.1x, a 5-minute write
is 1.25x, a 1-hour write is 2x.

**Unknown models must fail visibly.** When a model is missing from the table its
tokens are not priced as zero. `state.json` carries an `unknown_models` list and
the widget renders a warning row (`? claude-xyz`). A visible gap is preferable
to a silently understated total.

### `state.json`

The only file the widget reads. Roughly:

```json
{
  "updated_at": "2026-08-10T09:14:22Z",
  "last_turn_usd": 0.09,
  "session": {"id": "...", "usd": 2.47},
  "today_usd": 8.13,
  "window_5h": {"usd": 4.30, "pct": 62},
  "window_7d": {"usd": 21.7, "pct": 31},
  "unknown_models": []
}
```

`pct` is null when no calibration exists yet. `today_usd` covers all sessions
since local midnight; the two windows roll backwards from the moment of the
write. All timestamps in `state.json` are UTC, but day and window boundaries are
computed in local time, which is what the user actually experiences.

### `calibrate.py`

Subscription limits cannot be read locally, so they are calibrated once by hand.

Consumption is measured in **USD-equivalent, not tokens**. Opus draws harder on
the limit than Sonnet, and pricing weights the models automatically; a single
shared token ceiling would drift as the model mix changes.

The user runs `/usage` in Claude Code, reads the reported percentage, and runs
`./calibrate.py --5h 62`. The script records the USD-equivalent currently in the
5-hour window and derives the ceiling. Recalibrating at any time refines it.

**Before calibration the widget shows dollars, not percentages** — `$4.30 / 5h`
rather than an invented number.

The weekly figure uses a rolling 7 days. The real weekly limit resets on a fixed
schedule rather than rolling, so this row reads slightly pessimistic; that is
documented in the README and in the widget tooltip.

### `widget.py`

GTK 3 window launched with `GDK_BACKEND=x11`, so it runs as an X11 client under
XWayland and may position itself.

Layout:

```
+------------------------------+
|  last turn          +$0.09   |
|  session             $2.47   |
|  today               $8.13   |
| ---------------------------- |
|  5h window      ~62 % est.   |
|  week           ~31 % est.   |
+------------------------------+
```

All widget strings are English, consistent with the rest of the project.

Window properties: `set_decorated(False)`, `set_keep_above(True)`,
`set_skip_taskbar_hint(True)`, `set_skip_pager_hint(True)`. The initial position
is computed from the primary monitor's work area, inset from the bottom-right
corner so it clears the dock.

Because the window is undecorated, dragging is implemented with
`begin_move_drag()`, and the last position persists in `config.json`. Right
click opens a minimal menu: Calibrate, Hide, Quit.

Colour is applied only to the two limit rows (green / amber / red). The USD rows
stay neutral — the limits are where control matters.

Updates arrive through a `Gio.FileMonitor` on `state.json`. There is no polling;
the widget redraws when the hook finishes writing.

If `state.json` is missing or corrupt the widget shows placeholder dashes and
keeps running.

## Known limitation

Fast mode is not recorded in the transcripts — a search for `"speed":"fast"`
across all of them returns nothing. If fast mode were enabled, Opus 5 would cost
$10/$50 rather than $5/$25 and the widget would understate that turn by half.
The user has confirmed fast mode is not in use. This is documented in the README
because it cannot be detected locally.

## Testing

`smoke.sh` covers three things:

1. `tally.py` against a fixture transcript with hand-computed token counts,
   asserting the resulting cost to the cent.
2. That `<synthetic>` messages and duplicate `message.id` values are excluded.
3. `widget.py --selftest`, which renders one frame to a PNG and exits, so GTK
   startup is verified without a display.

## Out of scope

- Reading authoritative rate-limit state. `/usage` remains the source of truth.
- A statusline integration. `state.json` makes this a few lines to add later if
  wanted, but it is not built now.
- Historical charts or reporting beyond the widget rows.
