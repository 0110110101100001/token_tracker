# Authoritative limit percentages — design

Date: 2026-08-13
Status: approved, ready for implementation planning

## Purpose

Replace the calibrated limit percentages with the account-wide figures Claude Code
already caches on disk, so the two limit rows stop describing this machine and
start describing the account the limits actually belong to.

The panel's dollar rows are unaffected. They are an API-equivalent measure of
scale, not a limit, and they stay local.

## The problem this closes

`window_5h.pct` and `window_7d.pct` divide **this machine's** recorded spend by a
ceiling that stands for the **whole account's** limit. Both halves of that ratio
come from different scopes, so the percentage is wrong by however much work
happens on other machines — and wrong in the flattering direction, since the
unseen usage is missing from the numerator only.

This was already known: `limit.py` exists precisely to sidestep the ratio when
work is split across machines habitually, and README documents the gap. What is
new is that the ratio no longer has to be sidestepped, because the account-wide
percentage can be read directly.

A second failure the same change removes: a ceiling silently expires. This
account currently carries a `+50% weekly limits promo through Aug 19` (visible in
`~/.claude.json` under `cachedGrowthBookFeatures.tengu_rate_limit_promo_notices`).
Any ceiling calibrated or declared before it — or after it lapses — is wrong with
nothing on screen to say so.

## What the probe established

Measured on 2026-08-13 against Claude Code 2.1.231, not assumed.

| Fact | Consequence |
|---|---|
| `~/.claude.json` carries `cachedUsageUtilization`, holding server-supplied `utilization` plus `fetchedAtMs` and `accountUuid` | Account-wide limit state is readable locally, with no network call and no credential handling |
| Its `limits` array carries `kind`, `percent`, `severity`, `resets_at`, `scope`, `is_active` — `session`, `weekly_all`, `weekly_scoped` | Three limits exist, each already carrying its own reset time and severity |
| `five_hour` / `seven_day` objects carry the same percentages in an older shape | A fallback exists if `limits` is absent in some version |
| `limit_dollars`, `used_dollars`, `remaining_dollars` are all `null` | Percentages only. The ceiling cannot be read, so a dollar-denominated ceiling stays underivable |
| Percentages are whole integers | The authoritative figure cannot express sub-percent movement |
| Cache writes are throttled to `5 min` (`IEb=300000`) and Claude Code discards its own cache past `1 h` (`xEb=3600000`) | The anchor is coarse in time; a per-turn percentage is not available from it |
| Observed age during continuous work: `30 min` | Refresh is not driven by turns. The panel must show the age rather than imply freshness |
| `severity` arrives from the server (`normal` observed) | Row colour need not be inferred from thresholds |
| Transcripts carry no limit state — the only structured hit is `error.rateLimits`, `null` in both occurrences | The transcript pipeline cannot supply this; a second source is genuinely required |
| `stats-cache.json` is computed from the same local transcripts, and its `costUSD` is `0` | Not a usable source |
| `count_tokens` with the OAuth token returns 200 but carries no rate-limit headers | The free endpoint cannot supply limit state |
| `/v1/messages` with the OAuth token returns `429 rate_limit_error` while the account is far from its limit | The subscription credential may not drive the API directly. Fetching our own figures is not available, and should not be retried |

## Architecture

The pipeline gains one read, and nothing else about its shape changes:

```
Stop hook --> tally.py --> events.jsonl ------> state.json --> widget.py
                          ~/.claude.json ---/
                          (cachedUsageUtilization)
```

`state.json` remains the only file the panel reads, so the panel gains no second
failure mode and no knowledge of where the percentages came from.

## Components

### `cost_meter/utilization.py` (new)

The only reader of `~/.claude.json`. Returns normalised limit rows, or `None`
when there is nothing trustworthy to return. Three checks, each for a failure
that would otherwise show a confident wrong number:

- **Shape.** Prefer the `limits` array; fall back to `five_hour` / `seven_day`.
- **Account.** Compare the cache's `accountUuid` against
  `oauthAccount.accountUuid`. After a re-login to a different account the stale
  cache would otherwise be presented as this account's usage. Claude Code makes
  the same comparison and discards the cache on a mismatch.
- **Age.** From `fetchedAtMs`. Past one hour the data is unusable — the same
  threshold Claude Code applies to its own cache, rather than a number invented
  here.

The parsed age travels onward even when usable, because the panel has to be able
to say how old the figure is.

### `cost_meter/paths.py`

Add a reader for `~/.claude.json` with its own environment override. It is a
*sibling* of `claude_home()`, not a file inside it, and the existing note on
`claude_home()` already records why these overrides stay independent: a test
that redirected one and silently got the other.

### `cost_meter/summary.py`

`state.json` gains a `limits` block: the authoritative rows (`pct`, `severity`,
`resets_at`, `scope`) keyed by the server's `kind`, plus `age_s`. There is no
separate stale flag — the reader returns nothing at all once the cache is too
old, so absence already means stale and a second flag could only contradict it.
`window_5h.pct` and `window_7d.pct` are removed with the ceilings that fed them,
along with `window_5h.resets_at`, which now has one owner in the `limits` block;
the `usd` figures stay.

`current_block()` stays, because the local dollar figure still needs to know
which events fall inside the window. But when the anchor is present, the window
is bounded by `[resets_at - 5h, resets_at)` from the server rather than by a
start guessed from local timestamps. Without this the 5h row would state dollars
from one window beside a reset time from another — and the locally-guessed start
is exactly what other machines make wrong. The guess remains the fallback when
no anchor is available.

### `widget.py`

- Limit rows read the authoritative percentage. They now always have one, so the
  5h reset time now always shows. That is consistent with the rule set on
  2026-08-12: the time is conditioned on the row carrying a percentage, and it
  qualifies which block the figure describes.
- **The dollar figure leaves both limit rows and moves into the tooltip**
  (Martin's call, 2026-08-13). Keeping `$71.46 20 % · 19:04` would put this
  machine's dollars beside the account's percentage with nothing to distinguish
  them, and a reader equates two numbers sitting on one row — an equation that is
  false as soon as another machine contributes. The tooltip has the room to name
  each scope: `$71.46 on this machine · account at 20 %, resets 19:04`.

  Consequence: the 5h and week rows leave the rolling set, since an integer
  percentage that moves every 5–30 minutes has nothing to animate. `last turn`,
  `session` and `today` keep rolling, so `roll.py` is unaffected — it is handed
  fewer keys, not changed.
- **The week row gains a reset time**, which the server supplies for
  `weekly_all`. README's note that the weekly cap has no boundary this tool can
  locate is now false and goes with it.
- **The `~` marker is dropped from these rows.** Its stated reason was that a
  declared ceiling understates the account; a server-supplied percentage is not
  an estimate, so the reason does not survive.
- **Colour comes from `severity`**, not from thresholds applied to the
  percentage.
- When the anchor is stale or absent, the percentage and the reset time are
  withdrawn and the row shows its dollar figure alone — the project's existing
  preference for a dollar over an invented number, and the same state the row
  had before any calibration existed.
- **The anchor's age is not on the row.** A figure up to an hour old is normal
  here, so an age beside every percentage would be permanent noise; past an hour
  the percentage is withdrawn entirely, which says the same thing more plainly.
  `age_s` reaches the tooltip instead.
- **The tooltip is new.** The panel has none today — the 2026-08-10 design
  mentions one, but it was never built. So this change adds it, on the two limit
  rows only: `$71.46 on this machine · account at 20 %, resets 19:04`, plus the
  anchor's age. The text is a pure function beside `window_row`, matching how
  every other string in the panel is produced and tested; the GTK side is one
  `set_tooltip_text` per row.

### Deletions

`calibrate.py`, `limit.py`, `cost_meter/ceilings.py`, their tests, the
`calibrate` and `limit` pixi tasks, the `ceiling_5h_usd` / `ceiling_7d_usd`
config keys, and the README sections covering calibration, un-calibration and
declaring a known limit — including the jump-links to them, the panel-row
descriptions, and the known-limitation stating that the percentages are
estimates derived from calibration.

Every one of them exists only to turn dollars into a percentage. With the
percentage arriving from the server they have no remaining purpose, and leaving
them in place would leave two ways to produce the same row with no rule for which
wins.

Ceilings already written to `data/config.json` are ignored rather than migrated;
the panel no longer has anything to divide with them.

## Decisions

- **No interpolation between anchors.** Converting local dollars into a
  percentage between refreshes is possible and is deliberately not done: the
  result is an invented number, which is the thing this project has refused
  since the first design ("before calibration the widget shows dollars, not
  percentages"). Per-turn responsiveness stays where it is real — the dollar
  rows, which keep their rolling animation.
- **`weekly_scoped` gets no row.** Martin's call, 2026-08-13. The data is parsed
  and reaches `state.json`, so adding the row later is a widget change only.
- **We do not fetch our own figures.** Route rejected on the measured 429 above,
  plus the egress and credential handling it would need, for a few minutes of
  freshness.

## Testing

Unit tests against fixture `.claude.json` files, covering: the `limits` shape,
the legacy `five_hour`/`seven_day` fallback, a missing file, malformed JSON, an
`accountUuid` mismatch, an age past one hour, and an anchor-bounded 5h window
against a locally-guessed one.

The two new pure text functions — the limit row and its tooltip — are tested the
way `window_row` already is: percentage present, percentage withdrawn, each
severity, and a scope named in the tooltip. The GTK render stays covered by
`widget.py --selftest`, which does not exercise tooltips; that is why the text
lives in a pure function rather than inline at the call site.

`pixi run smoke` after every change, per project convention and the PostToolUse
hook.

## Out of scope

- A row for `weekly_scoped`, per the decision above.
- `extra_usage` / `spend` (credit balance in EUR), which is billing rather than
  limits.
- Fast-mode pricing, which stays knowingly broken by an earlier decision.
- Aggregating dollars across machines. The dollar rows stay local and are
  labelled as such.
