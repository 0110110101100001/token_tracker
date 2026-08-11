# Declared limit — design

**Date:** 2026-08-11
**Status:** approved

## Goal

Let a known ceiling be written straight into `config.json` instead of being
derived from a percentage, so the 5h and week rows can show a percentage on an
installation where calibration cannot work at all.

## Why calibration cannot cover this

The `ceiling_*_usd` values are not the subscription's limit. They are the
divisor that makes locally-recorded spend come out at the percentage `/usage`
reported, which is why `calibrate.py` computes `usd / (pct / 100)`.

That ratio pairs two numbers from different scopes. The spend is this
installation's, read from `~/.claude/projects` on this machine; the percentage
is the account's, covering every machine and surface signed into it. The README
already records the consequence — a derived ceiling "quietly absorbs whatever
usage this installation cannot see, which holds up while that share stays
roughly constant and drifts as soon as it does not" — and prescribes
recalibration for work that moves between machines.

Recalibration is the wrong prescription when the work is split habitually
rather than moved occasionally. Two machines used in the same window give a
ratio that describes neither: recalibrating on one of them produces a ceiling
that is wrong the moment the other is touched, and there is no reading of
`/usage` from which a correct one can be recovered. The number is not
approximate, it is unobtainable.

A limit the user already knows — a plan's weekly cap, say — sidesteps the ratio
entirely. It changes what the percentage means: not an estimate of account-wide
consumption, but this installation's measured share of a known bound. That is a
smaller claim, and unlike the derived ceiling it is a true one.

## Design

### 1. Where the code lives

| File | Role |
| --- | --- |
| `limit.py` (new, repo root) | declares a ceiling: parse, validate, write, report, refresh |
| `cost_meter/ceilings.py` (new) | sole owner of `ceiling_*` mutations in `config.json` |
| `calibrate.py` | narrows to deriving a ceiling from a ratio; delegates writes and clears |
| `pixi.toml` | `limit = "python limit.py"` |

`ceilings.py` holds `CEILINGS`, `clear_ceilings(keys)` (moved verbatim from
`calibrate.py`) and a new `set_ceilings(mapping)`. Both mutate `config.json`
through `store.update_json_locked`, which is what keeps the panel's
`widget_position` from being dropped by a wholesale rewrite.

It deliberately does **not** own the refresh. `calibrate.py` reaches `refresh`
with `from tally import refresh`; a module inside `cost_meter/` doing the same
would make the package depend on a root-level script, inverting the layering
every other module in it observes. The cost is that the refresh-and-report
block appears in both front ends rather than once. Six duplicated lines are
cheaper than a package that imports upwards.

The two front ends stay thin: argument parsing, validation, the printed lines,
and their own refresh.

### 2. One ceiling per window

A second entry point that writes the same two keys raises an obvious question:
what does `calibrate --clear-week` clear, and how does it differ from
`limit --clear-week`? The answer is that it does not differ. There is one
ceiling per window; whichever tool set it, either tool clears it, and both
route into the same `ceilings.clear_ceilings`.

Making them identical is the point. Provenance could have been tracked and each
tool taught to refuse the other's ceiling, but that adds state to `config.json`
and a failure mode to explain, in exchange for enforcing a distinction that does
not exist downstream: `summary.py` divides by whatever it finds.

One existing line of output changes as a consequence. `calibrate.py` prints
`calibration removed, back to dollars`; a cleared ceiling may never have been
calibrated, so both tools print `ceiling removed, back to dollars`. This is the
only change to behaviour that already shipped.

### 3. CLI

```
pixi run limit -- --5h 130
pixi run limit -- --week 2000
pixi run limit -- --5h 130 --week 2000
pixi run limit -- --clear-5h
pixi run limit -- --clear-week
pixi run limit -- --clear
```

The bare `--` matters for the same reason it does on `calibrate`: without it
pixi reads `--5h` as one of its own flags.

The three clear flags mirror `calibrate`'s exactly — same names, same
semantics, same output — because per section 2 they are the same operation.
`limit` offering a subset would imply a distinction between the two tools'
ceilings that does not exist.

Rejected, before anything is read or written:

- A value that is not greater than zero. It becomes a divisor.
- `nan` and `inf`. `argparse` with `type=float` accepts both spellings —
  `float("nan")` succeeds — and either would poison `_pct` rather than fail.
- Setting and clearing in one run, matching `calibrate`'s existing refusal.
- A bare run with no flags.

A run that names both windows validates both first and then writes once, so it
either applies everything it was asked for or changes nothing. The printed lines
follow the write rather than preceding it, so a run can never report a ceiling
it failed to persist. Both properties are `calibrate.py`'s, kept deliberately.

`limit.py` never reads spend, which is what makes the split cleaner than adding
flags to `calibrate.py` would have been. `calibrate` refuses a window with no
recorded spend, because a ratio needs something to divide; a declared ceiling
does not, so the same file would have had to carry a guard that applies to half
its flags. Here the guard is absent because it is inapplicable, not because it
was excepted.

### 4. What does not change

`summary.py`'s `_pct`, `widget.py`, and the shape of `config.json`. No new key
is introduced and the read path is untouched: `_pct` divides by
`ceiling_5h_usd` and `ceiling_7d_usd` without caring how they got there.

### 5. The `~` marker stays

The panel marks the percentage with `~` to say it is an estimate. A declared
ceiling looks like grounds for dropping it — the bound is known exactly, and the
spend is measured — but the inference runs the other way.

Against a derived ceiling, the percentage tracked `/usage` at the moment of
calibration, because the ceiling had absorbed the unseen usage. Against a
declared one there is nothing absorbing it, so the percentage understates
account-wide consumption by however much the other machines contribute. It is a
larger underestimate than before, not a smaller one. The estimate marker is
more warranted, and no `ceiling_*_source` key is stored.

### 6. Tests

`ClearCeilingsTest` moves from `tests/test_calibrate.py` to a new
`tests/test_ceilings.py`, following the code. Its
`test_the_saved_window_position_survives` case matters more after this change,
not less: there are now two writers of the ceilings and still a third of
`config.json`.

`tests/test_limit.py` covers the boundary rejections above, the both-windows
write being atomic, `widget_position` surviving a declaration, and set-plus-clear
being refused.

`tests/test_calibrate.py` keeps `ClearArgumentTest` and has its imports
corrected.

### 7. Documentation

`README.md`'s Calibration section gains a subsection on declaring a known
limit, and the passage advising recalibration when work moves between machines
points at `limit` instead — for a habitually split setup that advice is wrong,
per the reasoning above.

`store.py`'s `update_json_locked` docstring names `calibrate.py` as the writer
of the ceilings and needs the second one added. `pixi.toml` gains the task with
a comment in the file's existing style.

## Out of scope

- A baseline for spend on other machines. It would make the percentage track
  `/usage` across machines, but it is a second approximate figure to obtain and
  re-enter as it goes stale, which is the problem this design exists to escape.
- Reading limit state from Claude Code. It is not stored locally.
- Any change to how events are parsed, priced, or summed.
- Provenance for a ceiling, per section 5.

## Verification

To pass before this is considered done:

1. `pixi run test` — the existing suite plus the new files, all green.
2. `pixi run smoke` — unchanged, including the GTK render.
3. `pixi run limit -- --week 2000` on a `config.json` that already holds a
   `widget_position`, with the position still present afterwards and the week
   row showing a percentage.
4. `pixi run limit -- --week 0`, `-- --week nan` and `-- --week 2000 --clear`
   each refused, with `config.json` untouched.
5. `pixi run calibrate -- --clear-week` clearing a ceiling that `limit` wrote,
   printing the same line `limit --clear-week` would.
6. A declared 5h ceiling on a machine with no spend in the current block
   writing successfully — the case `calibrate` refuses.
