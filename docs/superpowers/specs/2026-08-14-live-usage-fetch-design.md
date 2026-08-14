# Live usage figures — design

Date: 2026-08-14
Status: approved, ready for implementation

## Purpose

Make the two limit percentages move on their own, instead of standing still until
the user runs `/usage`.

They stand still because they are not ours. They belong to the account, they come
from the server, and the only local copy is the one Claude Code caches in
`~/.claude.json` — which it refreshes at session start and on `/usage`, and at no
other time. Measured on 2026-08-13: a four-hour gap across continuous work.

## What the probe established

Measured on 2026-08-14 against Claude Code 2.1.232, not assumed.

| Fact | Consequence |
|---|---|
| The bundle contains `fetchUtilization: GET /api/oauth/usage`, on `api.anthropic.com` | The figures have an endpoint of their own, which the 2026-08-13 probe never tried |
| `GET https://api.anthropic.com/api/oauth/usage` with `Authorization: Bearer <claudeAiOauth.accessToken>` answered **200** | The panel can ask for the figures itself, with the credential already on disk |
| The body is byte-for-byte the shape of `cachedUsageUtilization.utilization`: `limits[]` with `kind`/`percent`/`severity`/`resets_at`/`scope`, plus the legacy `five_hour` / `seven_day` objects | `utilization.py`'s parser is reused whole. No second shape to understand |
| `limit_dollars`, `used_dollars`, `remaining_dollars` are still `null` | The ceiling stays underivable. Percentages only, exactly as before |
| `~/.claude/.credentials.json` carries `accessToken`, `expiresAt`, `refreshToken`, `refreshTokenExpiresAt` | Expiry is checkable before spending a request. Refresh is possible and is deliberately not done — see Decisions |
| `limits[].percent` is an integer; the legacy `five_hour.utilization` is a float (`8.0`) | Whether that float ever carries a fraction is unknown and is out of scope |
| **Polling every 5 s was refused**: one `200`, then `429 rate_limit_error` with `Retry-After: 196` | The interval is the server's to set, not ours. 5 s is not available |
| **Polling every 60 s was answered `200` every time**, across seven consecutive minutes, and `session` moved 12 % → 13 % between two of them | A minute is sustainable and is already finer than the figure itself: a whole point of a five-hour window is minutes of heavy work |
| Claude Code throttles its own cache writes to 5 min (`IEb=300000`) | Consistent with a server-side rule of roughly one call per few minutes, seen from the other side |

This supersedes the 2026-08-13 decision "**We do not fetch our own figures**".
That verdict rested on two measurements — `/v1/messages` answering 429 with an
OAuth token, and `count_tokens` carrying no rate-limit headers — and both stand.
Neither covered this endpoint, which is the one Claude Code actually asks.

## Architecture

One new source in front of the existing one, and nothing downstream changes:

```
              /api/oauth/usage --> usage_api --> data/usage.json --\
                                                                    >-- utilization.read() --> widget / tally
Claude Code --> ~/.claude.json (cachedUsageUtilization) ----------/
```

`utilization.read()` stays the single door to the account's figures, and it now
returns **the newer of the two** sources. Everything that reads it —
`CostMeter.read_limits`, `summary.anchor_window`, the tooltip — gets live figures
without knowing that a network exists.

## Components

### `cost_meter/usage_api.py` (new)

The only thing in the project that opens a socket.

- Reads `claudeAiOauth.accessToken` from `~/.claude/.credentials.json` on **every**
  fetch rather than caching it, because Claude Code rewrites that file whenever it
  refreshes the token, and a held copy would go stale exactly once and then fail
  forever.
- Skips the request when `expiresAt` has passed. A request that is certain to 401
  is not worth making, and Claude Code will have written a new token by the time
  it next runs.
- `fetch()` returns `(block, retry_after)`. Every failure is `(None, ...)`: no
  credential, an expired one, a non-200, a timeout, unparseable JSON. A panel must
  not die of a network error.
- **A refusal's `Retry-After` travels back with the failure.** The endpoint states
  how long it wants to be left alone, so the panel obeys that instead of deciding
  for itself; it is capped at an hour, and a header carrying an HTTP date rather
  than seconds is ignored in favour of the plain backoff.
- `refresh()` writes the answer to `data/usage.json` in the *same shape* as
  `cachedUsageUtilization` — `{accountUuid, fetchedAtMs, utilization}` — so the
  reader has one code path for both sources. The `accountUuid` is copied from
  `~/.claude.json`, which is what the reader compares against.
- Never writes `.credentials.json`, and never logs a token.

### `cost_meter/utilization.py`

Split into a parser and a source, which is what lets the second source cost
nothing:

- `_parse(cache, account, now)` — the existing account / age / shape checks,
  applied identically to whichever file the cache came from.
- `read()` — parses both files and returns whichever `fetchedAtMs` is newer.
  Newer, not "ours first": after a suspend, or with the poll disabled, Claude
  Code's cache can genuinely be the fresher of the two.

The account check applies to our own file as well. A `usage.json` left over from a
previous login describes somebody else's account just as surely as a stale
`cachedUsageUtilization` does, and the `accountUuid` stamped at write time is what
catches it. `MAX_AGE_SECONDS` stays a shared sanity cap.

### `cost_meter/paths.py`

`usage_path()` — `data/usage.json`, under `COST_METER_HOME`, so every test
redirects it for free.

### `widget.py`

The panel is what polls, because it is the only part of this tool that runs
continuously. The hook cannot: a turn landing is precisely the moment the
percentage does *not* need re-asking for.

- A second GLib timer, at `usage_poll_seconds` from `config.json`, default **60**
  — the endpoint's own pace, per the probe above, not a preference. `0` turns the
  fetch off entirely and leaves the panel reading Claude Code's cache, as it does
  today.
- The request runs on a worker thread and hands the result back through
  `GLib.idle_add`. A blocking read on the GTK main loop would freeze the panel for
  the length of the timeout.
- One request in flight at a time, and an exponential backoff on failure —
  doubling from the poll interval up to ten minutes, and never shorter than a
  `Retry-After` the server sent. A laptop that is merely offline must not fill the
  log, and an endpoint that has moved must not be asked once a minute forever.
- The first tick is one interval *after* startup, not at it. `--selftest` builds a
  real `CostMeter`, and smoke tests must not reach the network. The panel has
  Claude Code's session-start figure to show in the meantime.
- **Both timers are now dropped when the window is destroyed.** A GLib timeout
  belongs to the main context rather than to the widget, so a destroyed panel went
  on waking up and polling — invisible in production, where the panel outlives the
  process, and caught by the first test that built two panels in one process.

### The `≈` marker stays, unconditionally

Martin's call, 2026-08-14, asked for directly. The figure is now seconds old
rather than hours, so the case for dropping it exists — and it is still refused
for the reason set on 2026-08-13: a marker that comes and goes implies the
unmarked form is exact. It never is. `limits[].percent` is a whole number, the
window it describes is still growing while it is drawn, and the fallback to
Claude Code's cache is one failed request away.

The limit rows also stay out of `ROLL_KEYS`. An integer percentage that moves once
an hour has nothing to tween, whatever the polling rate.

## Decisions

- **We do not refresh the OAuth token.** `refreshToken` is right there and using it
  is out of the question: the refresh endpoint may rotate it, and rotating a token
  behind Claude Code's back could log the user out of the tool this panel exists to
  measure. An expired token means we skip the fetch and read the cache.
- **The reader picks by `fetchedAtMs`, not by source.** See above.
- **No dollar figures from this endpoint**, though it carries `extra_usage` and
  `spend` in EUR. That is credit balance, i.e. billing, and it belongs to the
  billing row's question rather than to the limit rows'.
- **The endpoint is undocumented and may change.** Which is why every failure path
  ends in the existing behaviour rather than in an error on the panel: if the
  endpoint moves, the rows go back to being hours old and nothing else happens.

## Testing

`tests/test_usage_api.py`, with a fake opener injected — no test touches the
network or the real credentials:

- a 200 body yields the `utilization` block
- a missing credentials file, and one with no `claudeAiOauth`, yield `None`
- an expired `expiresAt` yields `None` **without** calling the opener
- a 401, a timeout and unparseable JSON each yield no block
- a 429's `Retry-After` is carried back, a missing or unparseable one is not, and
  an absurd one is capped
- `refresh()` writes `usage.json` in the cache shape, and `utilization.read()`
  then returns those rows
- `backoff_seconds` doubles and clamps

`tests/test_utilization.py` gains the two-source cases: ours newer wins, Claude
Code's newer wins, ours alone answers, ours from another account or past the
sanity cap is refused, and an unreadable one leaves the cache answering.

`tests/test_widget.py` gains the interval validation, and the wiring on a real
main loop with `usage_api.refresh` patched — a figure the panel fetched itself
reaching the row, no fetch at all when the interval is `0`, a backoff that grows
on repeated failures and clears on an answer, and a `Retry-After` that beats it.

`pixi run smoke` after every change, per project convention.

## Out of scope

- Sub-integer percentages from the legacy float field. Unproven; a later probe.
- A row for `weekly_scoped`, still parsed and still undrawn.
- Aggregating dollars across machines, and fast-mode pricing. Both stand as they
  are.
