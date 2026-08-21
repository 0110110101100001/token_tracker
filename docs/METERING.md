# What it counts, and how

Every figure the panel shows is one of two kinds of number, and the difference
between them is the first thing to know about this tool. The **dollar rows**
count Claude Code work done on this machine, priced at published API rates. The
**limit percentages** come from the server and cover the whole account. This
document says where each comes from, what is checked before it is trusted, and
where it is wrong.

← [README](../README.md) · [The panel](PANEL.md) · [Reference](REFERENCE.md)

## What it counts

Worth reading before you trust a figure on the panel.

- **The dollar rows: this machine, this account, and nothing else.** Their whole
  input is the Claude Code transcripts under `~/.claude/projects/` on the computer
  the tool runs on. Claude Code on a second machine, under another user account,
  on the other side of a dual boot, or on Windows beside WSL writes its
  transcripts somewhere this installation never looks. claude.ai, the desktop app
  and the phone write no local transcripts at all. All of that spends your
  subscription without moving a dollar figure here.
- **The dollar figures are an API-equivalent, not an invoice.** They price your
  token counts at published API rates, which is a consistent way to weight and
  compare usage. On a subscription it is not a bill you will receive.
- **The limit percentages are the account's, and they are floors.** They come
  from the server, so they cover every machine, claude.ai and the phone alike —
  the machine boundary above does not apply to them. What they carry instead is
  age: usually a minute of it, and hours whenever the panel cannot reach the
  network, since usage since the fetch only pushes them up.



The full list, including the ones that are only worth knowing once something
looks wrong, is under [Known limitations](#known-limitations).

## Limit percentages

There is nothing to set up. The **5h window** and **week** rows show the
account's own figures — the same ones `/usage` prints — and the panel asks the
server for them itself, **once a minute**. No calibration, and nothing to redo
when your plan changes or a promotion moves the ceiling.

A minute is the endpoint's pace rather than a setting worth tuning: five-second
polling is refused outright (`429`, with a three-minute cool-off attached), and a
whole percentage point of a five-hour window is minutes of heavy work anyway. To
change it, or to stop the panel talking to the network at all, put
`usage_poll_seconds` in [`data/config.json`](REFERENCE.md#configuration) — seconds,
or `0` for off. With it off the rows fall back to whatever Claude Code last
cached, which is what they used to show.

**`≈` marks a floor, not a guess.**, so `≈17 %` means at least 17 %, never less.


Hover either row for what this machine spent in that window, how old the account
figure is, and the reset time in full.

A row falls back to a dollar figure alone when there is no account figure to show
— Claude Code has never run on this machine, the cache belongs to a different
login, or the window it described has since reset. What is checked before a figure
is trusted, and what replaced the old calibration, is under
[Where the limit figures come from](#where-the-limit-figures-come-from).

## Where the limit figures come from

How much of each limit the **account** has used is a fact only the server has, and
two things ask it for the panel:

- **the panel itself**, once a minute, with `GET /api/oauth/usage` — the request
  Claude Code makes internally (its bundle calls it `fetchUtilization`), using the
  subscription token Claude Code already keeps in `~/.claude/.credentials.json`.
  The answer lands in `data/usage.json`.
- **Claude Code**, which asks on a session start and when you run `/usage`, and
  caches its answer in `~/.claude.json` under `cachedUsageUtilization`.

Both files carry the same shape, and the panel reads whichever was fetched more
recently. So the second is a fallback rather than a redundancy: with the poll
turned off, after a suspend, or while the network is unreachable, Claude Code's
cache is the fresher of the two and the rows quietly come from there.

**This is the one part of the tool that opens a socket**, and it is a read: one
GET, no query, nothing about your machine in it. The token is read on every fetch
because Claude Code rewrites that file whenever it refreshes it — and it is only
ever read. Nothing here writes credentials, nothing logs a token, and the
`refreshToken` sitting beside it is deliberately never used: refreshing may rotate
it, and rotating it behind Claude Code's back could log you out of the tool this
panel exists to measure. An expired token simply means the fetch is skipped.

**The endpoint is undocumented and may change without notice.** That is why every
failure ends in the behaviour the panel had before it: nothing written, no error on
screen, and rows reading Claude Code's cache. It is also rate-limited — five-second
polling earned `429 Retry-After: 196` — so a refusal is obeyed for exactly as long
as it asks, and other failures back off by doubling, up to ten minutes. `data/cost-meter.log`
records each failed fetch, which is where an endpoint that has moved for good shows up.

Three things are checked before a figure is shown, each for a way it could be
confidently wrong:

- **The account.** Each file records which account it describes — Claude Code
  stamps its cache, and the panel stamps what it fetches. After logging in as
  somebody else the old figures would otherwise be presented as the new account's;
  on a mismatch they are dropped. Claude Code makes the same check.
- **The window.** Each row carries its own reset time, and once that has passed
  the figure describes a window that no longer exists. Age cannot detect this — a
  four-hour-old weekly figure is fine, a twenty-minute-old 5-hour figure is
  worthless if the block turned over in between — so the reset time is what
  decides.
- **Sanity.** Past a week the figure is refused outright: the weekly window has
  certainly reset by then, so no floor survives it. This is deliberately *not*
  Claude Code's own one-hour threshold — it discards a figure it can re-fetch at
  will, and the panel's own fetch may be the thing that is failing.

**A 5-hour block turning over mid-session used to leave the row on dollars until
the next `/usage`.** The old percentage is withdrawn the moment its window ends,
correctly — it describes a window that is gone — and the new window's percentage
was a fact only the server had, which nothing asked it for until a session started
or you typed `/usage`. So a block that turned over at lunchtime could leave the 5h
row showing dollars all afternoon. The panel's own poll is what closes that: the
new window's figure arrives within the minute, unasked.

**The panel reads both files itself**, rather than the copy the hook writes into
`state.json` — which only moves when a turn lands, and the things that move a
percentage have nothing to do with turns. The copy is still written, because
`tally` needs the reset times to anchor the dollar windows; the panel deliberately
ignores it.

Three things bring a new figure to the row, and they cover different failures.
The **once-a-minute fetch** is the ordinary one. A **file monitor** on Claude
Code's cache repaints the moment that file is written, which matters after a
`/usage`: the panel is usually right beside the output you are comparing it
against. The **60-second staleness poll** catches what no file change announces —
a window reaching its `resets_at`, where the row has to be withdrawn with nothing
having been written anywhere.

The previous design derived these percentages instead, by dividing locally
recorded spend by a ceiling you calibrated against a `/usage` reading. That is
gone, and so are `pixi run calibrate` and `pixi run limit`. It had two failures
this does not: the ratio paired one machine's dollars with the whole account's
percentage, so it was wrong by however much work happened elsewhere; and a
ceiling silently expired whenever a plan changed or a promotion moved the limit,
with nothing on screen to say so.

The dollar rows are still measured in USD-equivalent rather than raw tokens,
because the models draw on the limit unevenly — an Opus token costs the limit more
than a Sonnet token — and pricing already carries those per-model weights.

## The billing row

Every figure above it on the panel is a dollar amount whose meaning depends on
it. On a
seat they are notional — a measure of what you used, not of what you owe. On API
billing they are a bill. The row is there so the two are never confused.

It is decided in this order, first match winning:

1. `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in the session's environment,
   `apiKeyHelper` in `~/.claude/settings.json`, or `CLAUDE_CODE_USE_BEDROCK` /
   `CLAUDE_CODE_USE_VERTEX` → **API**. An exported key is what Claude Code
   prefers over the login it has on disk, so a machine that is signed in
   perfectly well can still be spending per token.
2. A `claudeAiOauth` block in `~/.claude/.credentials.json` → the seat, labelled
   with its `subscriptionType` and `rateLimitTier` (`default_claude_max_5x`
   becomes `max 5x`).
3. Neither → `—`. A guessed billing mode would misrepresent every row above it,
   so the panel says nothing instead.

Only the presence of a credential is ever read, never its value.

**This is not in the transcripts.** `usage.service_tier` is the field that looks
like it should carry it and does not — it reads `standard` whichever way the
account is billed, because it names the API's latency tier. So the mode is read
from the environment by the `Stop` hook, which is the only thing that runs
*inside* the session it is reporting on: a key exported for one session is
visible there and in no other process. Two consequences follow. Turns priced
before this row existed carry no mode and cannot be given one after the fact, so
an old `state.json` reads `—` until the next turn. And with several sessions
running under different credentials, the row describes whichever wrote last —
the same rule the **session** row already follows.

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

- **The dollar rows count one machine, not a subscription.**
  `~/.claude/projects/` on the computer the tool runs on is their entire input, so
  they are a per-installation view of usage that is really billed per
  subscription. Nothing warns you about the gap — invisible usage is
  indistinguishable from no usage. Running the tool on each machine you use gives
  each one its own honest local ledger, but does not add them up; there is
  deliberately no shared store, because merging ledgers across machines means
  reconciling clocks and de-duplicating sessions, which is a much larger tool than
  this one. The limit rows are unaffected: they come from the account.
- **The limit percentages are a minute old, and depend on an undocumented
  endpoint.** The panel fetches them itself once a minute; when that fails —
  offline, an expired token, a rate limit, or the endpoint changing — it falls
  silently back to whatever Claude Code last cached, which can be hours old with
  nothing on the row to distinguish the two cases.
- **A whole percentage point is the finest the server reports.** The figures arrive
  as integers, so the rows step rather than glide however often they are fetched,
  and the two limit rows are deliberately left out of the panel's rolling
  animation for that reason.
- **USD here is an API-equivalent, not an invoice** — on a seat. The dollar
  figures are then a consistent way to compare and weight usage, not a bill you
  will actually receive. The **billing** row says which case you are in, but it
  reports only the mode: on API billing the figures are the right *kind* of
  number, and still not an invoice, since they come from published rates rather
  than from Anthropic's meter.
- **The billing row cannot be backfilled and describes the last writer.** The
  mode is not in the transcripts — `usage.service_tier` names the API's latency
  tier and reads `standard` either way — so it is observed by the `Stop` hook in
  its own session's environment. Turns priced before the row existed carry no
  mode and never will, and with several sessions running under different
  credentials the row describes whichever wrote most recently.
- **Fast mode is priced as though it were standard.** The transcripts do record
  which speed served each message, in `usage.speed`, but nothing here reads that
  field and `pricing.json` carries no fast-mode rates. Since Opus 5 fast mode
  costs $10.00 / $50.00 against the standard $5.00 / $25.00, a fast turn would
  be understated by half.
- **Both *dollar* windows are only as well-placed as the reset the server last
  reported.** With one, they are the five hours and the seven days ending at it,
  which is why the **this machine** row empties exactly when the percentage above
  it does. Without one — no cached figures at all — they fall back to a trailing
  seven days and a block anchored on this machine's own first message, and then
  the weekly figure keeps a tail the real limit has already reset past. The window
  *length* is assumed either way: the server sends only the end.
- **The 5h *dollar* figure falls back to a guess when no account figure is
  available.** Normally the server's reset time bounds it. Without one, the tool
  anchors the block on the first Claude Code message it can see, which is later
  than the real start whenever the block was opened elsewhere — another machine,
  or Claude on claude.ai, which shares the same pool and writes nothing here. The
  figure then covers too little.
- **Server-side tool use is not counted.** Web search is billed per thousand
  searches rather than per token, and those counts (`usage.server_tool_use`)
  are ignored. They are zero across every transcript on the machine this was
  built for.
- **`/usage` and the panel now agree by construction**, because they read the
  same file, and the panel watches it — so a `/usage` reaches the rows as it is
  written, without waiting for a turn.
