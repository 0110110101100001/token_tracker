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

A seventh row appears only when needed: a warning listing any model that has
no entry in `pricing.json`, so an unpriced model is visible instead of
silently costing nothing.

Cost is computed from the same token counts Claude Code already writes to
its own transcript files under `~/.claude/projects/`; the tool reads those,
prices each assistant message, and keeps a rolling ledger of the result.

## Install

1. Register the `Stop` hook in `~/.claude/settings.json`, alongside any hooks
   you already have:

   ```json
   "Stop": [
     {
       "hooks": [
         {"type": "command", "command": "/home/martin/Desktop/token_calculator/tally.py", "timeout": 20}
       ]
     }
   ]
   ```

2. Start the panel:

   ```bash
   ./run_widget.sh
   ```

3. To have the panel start automatically on login, an autostart entry at
   `~/.config/autostart/claude-cost-meter.desktop` handles it for you — it is
   not part of this repo, so it survives a fresh checkout and is created
   once, separately, on the machine that runs the panel.

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
   ./calibrate.py --5h <pct>
   ./calibrate.py --week <pct>
   ```

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

A model that has no entry in this table is **never** treated as free. It is
excluded from the priced totals and instead surfaces as a `?` warning row on
the panel, so a pricing gap is visible rather than silently undercounting
your spend.

## Known limitations

- **USD here is an API-equivalent, not an invoice.** The account this tool
  was built for is on a subscription, not pay-per-token API billing — the
  dollar figures are a consistent way to compare and weight usage, not a
  bill you will actually receive.
- **Fast mode is not recorded in the transcripts.** A turn run in fast mode
  is not distinguishable from a normal turn in the data this tool reads, so
  it would be understated by roughly half. Fast mode is not currently in
  use, so this has no effect today, but it would if that changed.
- **The weekly row is a rolling 7 days**, not the fixed weekly window the
  real subscription limit resets on. This reads slightly pessimistic:
  the tool's week can include a leading tail of usage the real limit has
  already reset past.
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
- `config.json` — calibrated ceilings (`ceiling_5h_usd`, `ceiling_7d_usd`)
  and the widget's last window position.
- `cost-meter.log` — where the `Stop` hook and `calibrate.py` log faults
  that were swallowed to keep your critical path unbroken.

Deleting `data/` entirely is a safe full reset: the next run rebuilds
`events.jsonl` and `state.json` from the real transcripts under
`~/.claude/projects/` from scratch. You lose your calibrated ceilings and
the saved window position, nothing else — re-run `calibrate.py` to get the
percentages back.

## Troubleshooting

- **The widget stops updating** — check `data/cost-meter.log`. Every fault on
  the `Stop` hook's critical path is caught and logged there rather than
  shown, by design, so a stuck panel almost always has a line waiting there.
- **Numbers look lower than expected** — check the panel for a `?` warning
  row. It means at least one model you used has no entry in `pricing.json`
  and is being excluded from the totals rather than counted as zero.
- **The panel is invisible** — confirm it is actually running under XWayland
  (`GDK_BACKEND=x11`, which `run_widget.sh` sets for you). A pure Wayland
  client cannot position itself in a corner or raise itself above other
  windows, so under plain Wayland the panel can end up running with no
  visible effect.
