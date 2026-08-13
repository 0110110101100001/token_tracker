# Authoritative Limit Percentages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the calibrated limit percentages with the account-wide figures Claude Code already caches in `~/.claude.json`, so the two limit rows describe the account rather than this machine.

**Architecture:** A new `cost_meter/utilization.py` reads `cachedUsageUtilization` out of `~/.claude.json`, validates it (right account, young enough, a percentage it understands) and returns rows or `None`. `tally.py` passes those rows to `build_state`, which puts them in `state.json` and uses the server's reset time to bound the local 5-hour dollar window. The panel draws percentages on the limit rows, moves the dollars into a new tooltip, and colours from the server's `severity`. The whole calibration mechanism — `calibrate.py`, `limit.py`, `cost_meter/ceilings.py` — is deleted, because its only job was inventing the percentage this now reads.

**Tech Stack:** Python 3.14 standard library; PyGObject / GTK 3 in `widget.py` only; pixi tasks; `unittest` via `run_tests.py` (auto-discovers `tests/test_*.py`).

**Spec:** `docs/superpowers/specs/2026-08-13-authoritative-limits-design.md`

## Global Constraints

- **All file content in English** — code, comments, docstrings, commit messages, README.
- **Standard library only** outside `widget.py`. No new dependencies; nothing here reaches the network.
- **`tally.py` always exits 0.** A failure costs a number on screen, never the ability to work. `utilization.read()` must not raise into the hook path.
- **`pixi run smoke` after every task** — project convention and the PostToolUse hook both look for it. `pixi run test` for unit tests alone.
- **Windows and Linux run the same code.** No POSIX-only calls; paths via `pathlib`.
- **JSON writes go through `store.write_json_atomic`**; `config.json` read-modify-write goes through `store.update_json_locked`.
- **Ask Martin before every `git commit`.** The commit steps below are written out, but his standing rule is that commits are never made unasked. Commit messages end with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- **Two settled decisions this plan deliberately reverses**, both approved 2026-08-13: the `~` estimate marker leaves the limit rows, and the dollar figure leaves them for the tooltip. Do not restore either.
- **`weekly_scoped` gets no row.** It is parsed and reaches `state.json`; adding the row later must be a widget-only change.

## File Structure

| File | Responsibility |
|---|---|
| `cost_meter/paths.py` | gains `claude_config_path()` — the only place `~/.claude.json` is named |
| `cost_meter/utilization.py` *(new)* | the only reader of that file; validation and normalisation |
| `cost_meter/summary.py` | `anchor_block()`; `build_state` takes limits instead of a calibration |
| `tally.py` | calls `utilization.read()` and hands it to `build_state` |
| `widget.py` | `severity_class()`, `window_tooltip()`, rewritten `window_row()`, `draw_limits()` |
| `tests/support.py` | redirects `COST_METER_CLAUDE_CONFIG`; `write_claude_config()` helper |
| `tests/test_paths.py` *(new)* | the override and the sibling-not-child relationship |
| `tests/test_utilization.py` *(new)* | every way the cache can be untrustworthy |
| deleted | `calibrate.py`, `limit.py`, `cost_meter/ceilings.py`, `tests/test_calibrate.py`, `tests/test_ceilings.py`, `tests/test_limit.py` |

---

### Task 1: The path to `~/.claude.json`

**Files:**
- Modify: `cost_meter/paths.py` (append after `claude_settings_path`, ~line 92)
- Modify: `tests/support.py:20-52`
- Test: `tests/test_paths.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `paths.claude_config_path() -> pathlib.Path`, honouring `COST_METER_CLAUDE_CONFIG`. `tests/support.py`'s `TempHome` gains `self.claude_config` (a `str` path) and `write_claude_config(data: dict) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_paths.py`:

```python
# tests/test_paths.py
"""The one filesystem location that is not inside a directory we redirect."""

import os
import unittest
from pathlib import Path

from cost_meter import paths


class ClaudeConfigPathTest(unittest.TestCase):
    def setUp(self):
        previous = os.environ.pop("COST_METER_CLAUDE_CONFIG", None)
        self.addCleanup(self._restore, previous)

    @staticmethod
    def _restore(previous):
        if previous is None:
            os.environ.pop("COST_METER_CLAUDE_CONFIG", None)
        else:
            os.environ["COST_METER_CLAUDE_CONFIG"] = previous

    def test_default_is_the_file_beside_the_claude_directory(self):
        self.assertEqual(paths.claude_config_path(), Path.home() / ".claude.json")

    def test_it_is_a_sibling_of_claude_home_not_a_child_of_it(self):
        # The whole reason it gets its own override: deriving it from
        # claude_home() would put it inside a directory tests redirect, and the
        # real file would never be read -- or worse, would be in production.
        self.assertNotEqual(paths.claude_config_path().parent, paths.claude_home())

    def test_the_override_is_honoured(self):
        os.environ["COST_METER_CLAUDE_CONFIG"] = os.path.join("tmp", "fake.json")
        self.assertEqual(paths.claude_config_path(),
                         Path(os.path.join("tmp", "fake.json")))
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `cd ~/Desktop/token_calculator && python -m unittest tests.test_paths -v`
Expected: FAIL — `AttributeError: module 'cost_meter.paths' has no attribute 'claude_config_path'`

- [ ] **Step 3: Add the path helper**

Append to `cost_meter/paths.py`:

```python
def claude_config_path():
    """Claude Code's own config file — a sibling of claude_home(), not inside it.

    This is where Claude Code caches what the server says about the account's
    limits, which is the one thing the transcripts cannot tell us.

    Overridable on its own rather than derived from claude_home() for the reason
    that one already records: the two would then be impossible to redirect
    separately, and a test wanting one would silently get the other. Here the
    consequence would be worse than a confusing test — claude_home() is
    redirected in every test, so a derived path would never point at the real
    file, and the reader would look permanently empty.
    """
    override = os.environ.get("COST_METER_CLAUDE_CONFIG")
    return Path(override) if override else Path.home() / ".claude.json"
```

- [ ] **Step 4: Run it to make sure it passes**

Run: `python -m unittest tests.test_paths -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Redirect it in the test base class**

In `tests/support.py`, extend the docstring's list of redirected variables and `setUp`. Replace the `setUp` body's tuple loop and add the helper:

```python
        self.tmp = tempfile.mkdtemp()
        self.transcripts = os.path.join(self.tmp, "transcripts")
        self.claude_home = os.path.join(self.tmp, "claude")
        self.claude_config = os.path.join(self.tmp, "claude.json")
        os.makedirs(self.transcripts)
        os.makedirs(self.claude_home)
        for name, value in (("COST_METER_HOME", self.tmp),
                            ("COST_METER_TRANSCRIPTS", self.transcripts),
                            ("COST_METER_CLAUDE_HOME", self.claude_home),
                            ("COST_METER_CLAUDE_CONFIG", self.claude_config)):
            self.addCleanup(self._restore, name, os.environ.get(name))
            os.environ[name] = value
```

and, beside `write_config`:

```python
    def write_claude_config(self, data):
        """Stand in for ~/.claude.json, which holds the account's limit figures.

        Redirected for the same reason as .credentials.json beside it: the real
        file describes however this machine happens to be logged in and how
        recently it was used, so a test that read it would pass or fail on the
        weather.
        """
        store.write_json_atomic(paths.claude_config_path(), data)
```

Add to the module docstring, after the `COST_METER_CLAUDE_HOME` paragraph:

```
COST_METER_CLAUDE_CONFIG points at a stand-in for ~/.claude.json. It is a
separate variable because that file is a sibling of ~/.claude/ rather than a file
inside it, so redirecting the directory does not move it.
```

- [ ] **Step 6: Run the whole suite and the smoke test**

Run: `pixi run test && pixi run smoke`
Expected: PASS — nothing reads the new variable yet, so this only proves the wiring broke nothing.

- [ ] **Step 7: Commit** (ask Martin first)

```bash
git add cost_meter/paths.py tests/support.py tests/test_paths.py
git commit -m "feat: locate ~/.claude.json, where the account's limits are cached"
```

---

### Task 2: Read the account's limit figures

**Files:**
- Create: `cost_meter/utilization.py`
- Test: `tests/test_utilization.py` (create)

**Interfaces:**
- Consumes: `paths.claude_config_path()`, `store.read_json` (Task 1).
- Produces:
  - `utilization.SESSION = "session"`, `utilization.WEEKLY = "weekly_all"`
  - `utilization.MAX_AGE_SECONDS = 7 * 86400.0` — a sanity cap, not a freshness
    rule. Refresh is triggered by session start and by the user running `/usage`,
    so hours-old figures are the normal case; a stale percentage is still a valid
    lower bound because usage within a window only grows. Past a week the weekly
    window has certainly reset and no bound survives.
  - `utilization.read(now: float | None = None) -> dict | None`, returning
    `{"age_s": float, "rows": {kind: {"pct": int, "severity": str | None, "resets_at": str | None, "scope": str | None}}}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_utilization.py`:

```python
# tests/test_utilization.py
"""Every way the cached account figures can be untrustworthy."""

from cost_meter import utilization
from tests.support import TempHome

NOW = 1_786_000_000.0
ACCOUNT = "acct-1"
RESET_5H = "2026-08-13T11:29:59.675479+00:00"
RESET_7D = "2026-08-15T00:59:59.675518+00:00"

LIMITS = {"limits": [
    {"kind": "session", "percent": 11, "severity": "normal",
     "resets_at": RESET_5H, "scope": None, "is_active": False},
    {"kind": "weekly_all", "percent": 15, "severity": "normal",
     "resets_at": RESET_7D, "scope": None, "is_active": True},
    {"kind": "weekly_scoped", "percent": 2, "severity": "normal",
     "resets_at": RESET_7D,
     "scope": {"model": {"id": None, "display_name": "Fable"}},
     "is_active": False},
]}

LEGACY = {
    "five_hour": {"utilization": 11, "resets_at": RESET_5H,
                  "limit_dollars": None, "used_dollars": None},
    "seven_day": {"utilization": 15, "resets_at": RESET_7D,
                  "limit_dollars": None, "used_dollars": None},
}


class UtilizationTest(TempHome):
    def write(self, utilization_block, account=ACCOUNT,
              cache_account=ACCOUNT, fetched_s=NOW - 60.0):
        self.write_claude_config({
            "oauthAccount": {"accountUuid": account},
            "cachedUsageUtilization": {
                "fetchedAtMs": fetched_s * 1000.0,
                "accountUuid": cache_account,
                "utilization": utilization_block,
            },
        })

    def test_the_limits_array_gives_both_rows_with_severity(self):
        self.write(LIMITS)
        result = utilization.read(now=NOW)
        self.assertEqual(result["rows"][utilization.SESSION],
                         {"pct": 11, "severity": "normal",
                          "resets_at": RESET_5H, "scope": None})
        self.assertEqual(result["rows"][utilization.WEEKLY]["pct"], 15)

    def test_the_scoped_weekly_limit_is_carried_with_its_model_named(self):
        # No row draws it, but parsing it here means adding that row later is a
        # widget change and nothing more.
        self.write(LIMITS)
        scoped = utilization.read(now=NOW)["rows"]["weekly_scoped"]
        self.assertEqual((scoped["pct"], scoped["scope"]), (2, "Fable"))

    def test_the_age_of_the_cache_is_reported(self):
        self.write(LIMITS, fetched_s=NOW - 1800.0)
        self.assertAlmostEqual(utilization.read(now=NOW)["age_s"], 1800.0, places=1)

    def test_the_older_shape_is_read_when_there_is_no_limits_array(self):
        self.write(LEGACY)
        rows = utilization.read(now=NOW)["rows"]
        self.assertEqual(rows[utilization.SESSION]["pct"], 11)
        self.assertEqual(rows[utilization.WEEKLY]["resets_at"], RESET_7D)
        # No severity in that shape; the panel colours from the percentage.
        self.assertIsNone(rows[utilization.SESSION]["severity"])

    def test_a_missing_file_is_no_answer(self):
        self.assertIsNone(utilization.read(now=NOW))

    def test_an_unreadable_file_is_no_answer(self):
        paths_file = self.claude_config
        with open(paths_file, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(utilization.read(now=NOW))

    def test_another_accounts_cache_is_refused(self):
        # What a re-login leaves behind. Presenting it would report somebody
        # else's usage as this account's.
        self.write(LIMITS, account="acct-2", cache_account="acct-1")
        self.assertIsNone(utilization.read(now=NOW))

    def test_a_cache_past_the_sanity_cap_is_refused(self):
        self.write(LIMITS, fetched_s=NOW - utilization.MAX_AGE_SECONDS - 1.0)
        self.assertIsNone(utilization.read(now=NOW))

    def test_an_hours_old_cache_is_still_an_answer(self):
        # The normal case, not an error: refresh happens on session start and on
        # /usage, so hours pass between them. The figure is a floor, and the
        # panel says so with the >= marker rather than throwing it away.
        self.write(LIMITS, fetched_s=NOW - 4 * 3600.0)
        self.assertEqual(utilization.read(now=NOW)["rows"][utilization.SESSION]["pct"],
                         11)

    def test_a_cache_written_by_a_clock_ahead_of_ours_is_not_fresh_forever(self):
        self.write(LIMITS, fetched_s=NOW + 600.0)
        self.assertEqual(utilization.read(now=NOW)["age_s"], 0.0)

    def test_a_shape_carrying_no_percentage_is_no_answer(self):
        self.write({"limits": [{"kind": "session", "percent": None}]})
        self.assertIsNone(utilization.read(now=NOW))

    def test_a_cache_with_no_utilization_block_is_no_answer(self):
        self.write(None)
        self.assertIsNone(utilization.read(now=NOW))
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m unittest tests.test_utilization -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cost_meter.utilization'`

- [ ] **Step 3: Write the module**

Create `cost_meter/utilization.py`:

```python
# cost_meter/utilization.py
"""The account's limit percentages, as Claude Code last cached them.

Claude Code asks the server how much of each limit the *account* has used, and
caches the answer in ~/.claude.json under `cachedUsageUtilization`. Reading that
file is the whole of this module, and it is the only way the panel can know these
figures: a limit belongs to the account, while a transcript only ever records
what happened on this machine. No calibration can bridge that — it would divide
one machine's dollars by the whole account's ceiling.

Nothing here reaches the network. The cache is a side effect of Claude Code
running, so on a machine in use it is present and recent, and on one that is not
it is absent or old. Both of those are answered with None rather than with a
figure presented as current.
"""

import time

from . import paths, store

# A sanity cap, not a freshness rule. Refresh is triggered by session start and
# by the user running /usage, so an hours-old figure is the normal case -- and it
# is still worth showing, because usage within a window only grows and a stale
# percentage is therefore a floor rather than a guess. Past a week the weekly
# window has certainly reset and no bound survives it.
#
# Claude Code's own one-hour threshold is deliberately not used: it discards a
# figure it can re-fetch on demand, which this panel cannot.
MAX_AGE_SECONDS = 7 * 86400.0

# The two limits the panel draws, named as the server names them.
SESSION = "session"
WEEKLY = "weekly_all"

# The kinds worth carrying into state.json. `weekly_scoped` has no row, and is
# parsed anyway so that adding one is a widget change and nothing more.
KINDS = (SESSION, WEEKLY, "weekly_scoped")

# The older shape, mapped onto the same kind names.
LEGACY_KEYS = {"five_hour": SESSION, "seven_day": WEEKLY}


def _scope_name(scope):
    """`Fable` out of the server's nested scope object, or None."""
    model = (scope or {}).get("model") or {}
    return model.get("display_name") or None


def _row(pct, severity, resets_at, scope=None):
    return {"pct": pct, "severity": severity, "resets_at": resets_at,
            "scope": scope}


def _from_limits(limits):
    """Rows out of the `limits` array — the shape that carries severity.

    A percentage has to be a whole number to count. The server sends integers;
    anything else is a shape this module does not understand, and guessing at it
    would put a confident figure on the row.
    """
    rows = {}
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        pct = entry.get("percent")
        if kind not in KINDS or not isinstance(pct, int) or isinstance(pct, bool):
            continue
        rows[kind] = _row(pct, entry.get("severity"), entry.get("resets_at"),
                          _scope_name(entry.get("scope")))
    return rows


def _from_legacy(utilization):
    """Rows out of the older `five_hour` / `seven_day` objects.

    They carry the same percentages and reset times but no severity, so a row
    built here leaves the panel to colour from the percentage. Kept as a fallback
    because `limits` is the newer of the two shapes: if a Claude Code version
    stops sending it, the panel should lose the colour source, not the figures.
    """
    rows = {}
    for key, kind in LEGACY_KEYS.items():
        entry = utilization.get(key)
        if not isinstance(entry, dict):
            continue
        pct = entry.get("utilization")
        if not isinstance(pct, int) or isinstance(pct, bool):
            continue
        rows[kind] = _row(pct, None, entry.get("resets_at"))
    return rows


def read(now=None):
    """The account's limit rows and the cache's age, or None.

    None covers every way the answer can be untrustworthy: no file, an unreadable
    one, a cache belonging to a different account, one older than Claude Code
    itself would use, or one carrying no percentage this module understands. The
    caller shows dollars in all of those cases, which is what these rows showed
    before any of this existed.

    Returns `{"age_s": float, "rows": {kind: row}}`. The age travels with the
    rows because the panel has to be able to say how old a figure is: it is
    refreshed at most every five minutes and often much less
    often, so freshness cannot be implied.
    """
    now = time.time() if now is None else now
    data = store.read_json(paths.claude_config_path(), default=None)
    if not isinstance(data, dict):
        return None

    cache = data.get("cachedUsageUtilization")
    if not isinstance(cache, dict):
        return None

    # A cache left behind by a previous login describes somebody else's account.
    # Claude Code makes this same comparison and drops the cache on a mismatch.
    account = (data.get("oauthAccount") or {}).get("accountUuid")
    if not account or cache.get("accountUuid") != account:
        return None

    fetched_ms = cache.get("fetchedAtMs")
    if not isinstance(fetched_ms, (int, float)) or isinstance(fetched_ms, bool):
        return None
    age = now - fetched_ms / 1000.0
    # A writer whose clock runs ahead of ours produces a negative age, which
    # would otherwise read as fresh forever. summary.staleness clamps the same
    # case for the same reason.
    age = max(age, 0.0)
    if age > MAX_AGE_SECONDS:
        return None

    utilization = cache.get("utilization")
    if not isinstance(utilization, dict):
        return None
    limits = utilization.get("limits")
    rows = _from_limits(limits) if isinstance(limits, list) else {}
    if not rows:
        rows = _from_legacy(utilization)
    if SESSION not in rows and WEEKLY not in rows:
        return None

    return {"age_s": round(age, 1), "rows": rows}
```

- [ ] **Step 4: Run them to make sure they pass**

Run: `python -m unittest tests.test_utilization -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Run the whole suite and the smoke test**

Run: `pixi run test && pixi run smoke`
Expected: PASS

- [ ] **Step 6: Commit** (ask Martin first)

```bash
git add cost_meter/utilization.py tests/test_utilization.py
git commit -m "feat: read the account's limit percentages from Claude Code's cache"
```

---

### Task 3: Put the account figures in `state.json`

**Files:**
- Modify: `cost_meter/summary.py:26-29` (delete `_pct`), `:170-215` (`build_state`)
- Modify: `tally.py:61-72`
- Test: `tests/test_summary.py:1-150`

**Interfaces:**
- Consumes: `utilization.read()`, `utilization.SESSION` (Task 2).
- Produces:
  - `summary.anchor_block(limits, now_epoch) -> tuple[float, float] | None`
  - `summary.build_state(events, pricing, session_id, new_ids, now_epoch, limits)` — the sixth argument is now the `utilization.read()` result (or `None`), not a config dict
  - `state.json`: `window_5h` and `window_7d` carry `usd` only; a new top-level `limits` key carries the `utilization.read()` result or `None`

- [ ] **Step 1: Write the failing tests**

In `tests/test_summary.py`, replace the `NO_CAL` constant with limit fixtures and add a new test class. Change line 8 from `NO_CAL = {...}` to:

```python
# No account figures available: what every row showed before this existed.
NO_LIMITS = None


def limits(pct_5h=11, pct_week=15, resets_at=None, age_s=60.0):
    """A utilization.read() result, as build_state now receives it."""
    rows = {"session": {"pct": pct_5h, "severity": "normal",
                        "resets_at": resets_at, "scope": None},
            "weekly_all": {"pct": pct_week, "severity": "normal",
                           "resets_at": None, "scope": None}}
    return {"age_s": age_s, "rows": rows}
```

Then replace every `NO_CAL` argument with `NO_LIMITS`, delete `test_pct_is_none_without_calibration` and the test at line 118 that passes a ceiling, and add:

```python
class LimitsTest(unittest.TestCase):
    """The account figures pass through, and they bound the local 5h window."""

    def setUp(self):
        self.now = 1_786_000_000.0

    def test_the_account_figures_reach_state_untouched(self):
        given = limits()
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, given)
        self.assertEqual(state["limits"], given)

    def test_no_account_figures_is_recorded_as_such(self):
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, NO_LIMITS)
        self.assertIsNone(state["limits"])

    def test_the_window_rows_no_longer_carry_a_percentage_or_a_reset(self):
        # Both moved to `limits`, which has one owner. Two sources for the same
        # fact is what the ceilings did.
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, limits())
        self.assertEqual(set(state["window_5h"]), {"usd"})
        self.assertEqual(set(state["window_7d"]), {"usd"})

    def test_the_servers_reset_time_bounds_the_local_dollar_window(self):
        # A block opened on another machine started before anything in these
        # events, so the local guess would put its start too late and count too
        # little. The reset time the server reports is what the block really is.
        reset = datetime.fromtimestamp(self.now + 600.0, timezone.utc).isoformat()
        early = self.now - 4 * 3600.0   # inside the server's block
        late = self.now - 60.0
        state = build_state([event(early, "a"), event(late, "b")], PRICING,
                            "s1", set(), self.now, limits(resets_at=reset))
        # Both events fall in [reset - 5h, reset), so both are counted.
        self.assertAlmostEqual(state["window_5h"]["usd"],
                               state["today_usd"], places=4)

    def test_a_reset_time_already_past_falls_back_to_the_local_guess(self):
        stale_reset = datetime.fromtimestamp(self.now - 60.0,
                                            timezone.utc).isoformat()
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, limits(resets_at=stale_reset))
        # The local guess opens a block on the one event there is.
        self.assertGreater(state["window_5h"]["usd"], 0.0)
```

Add the imports `tests/test_summary.py` now needs at the top: `import unittest` (if absent) and `from datetime import datetime, timezone`.

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m unittest tests.test_summary -v`
Expected: FAIL — `AttributeError: module 'cost_meter.summary' has no attribute 'anchor_block'` and `KeyError: 'limits'`

- [ ] **Step 3: Rewrite the summary**

In `cost_meter/summary.py`: add `from . import utilization` to the imports, **delete** `_pct` (lines 26-29), and add above `build_state`:

```python
def anchor_block(limits, now_epoch):
    """The 5-hour block bounded by the server's reset time, or None.

    The server knows when the block it is reporting on ends. `current_block`
    only knows when this machine last sent something, and on an account used
    from more than one machine those disagree — a block another machine opened
    started earlier than anything in these events, so the local guess puts the
    start too late and the dollar figure too low.

    None when there are no account figures, when the row carries no usable reset
    time, or when that time has already passed: a block that has reset is not
    the open one, and the caller falls back to the local guess.
    """
    row = ((limits or {}).get("rows") or {}).get(utilization.SESSION)
    end = parse_updated_at((row or {}).get("resets_at"))
    if end is None or now_epoch >= end:
        return None
    return end - BLOCK_5H_SECONDS, end
```

Then in `build_state`, change the signature's last parameter from `calibration` to `limits`, replace the block line:

```python
    block = anchor_block(limits, now_epoch) or current_block(
        [event[0] for event in events], now_epoch)
```

and replace the two window entries and add the new key in the returned dict:

```python
        # Dollars only. The percentage and the reset time live under `limits`,
        # which has one owner: these figures are this machine's and those are the
        # account's, and a row that carried both would invite dividing one by
        # the other.
        "window_5h": {"usd": round(usd_5h, 4)},
        "window_7d": {"usd": round(usd_7d, 4)},
        # The account's own figures, straight through. None when Claude Code has
        # cached nothing usable — see cost_meter/utilization.py.
        "limits": limits,
```

- [ ] **Step 4: Update the caller**

In `tally.py`, add `utilization` to the `from cost_meter import ...` line, and replace lines 61-72:

```python
    pricing = load_pricing(paths.pricing_path())
    events = store.read_events(events_path)

    # last_turn is scoped to this session's own bookmark rather than to what this
    # run happened to append: with parallel sessions the first hook to fire picks
    # up everybody's new messages, so "appended by me" reads 0.00 for the rest.
    marks_path = paths.session_marks_path()
    marks = store.read_json(marks_path, default={}) or {}
    turn_ids, mark = new_turn_ids(events, session_id, marks.get(session_id))

    # Read here rather than inside build_state, which turns events into figures
    # and has no business reading files outside the ledger — the same split
    # billing.detect() gets below.
    state = build_state(events, pricing, session_id, turn_ids, now,
                        utilization.read(now))
```

Note the `config.json` read is gone: nothing in `build_state` needs it any more.

- [ ] **Step 5: Run the tests to make sure they pass**

Run: `python -m unittest tests.test_summary tests.test_tally tests.test_turns -v`
Expected: PASS. If `tests/test_tally.py` or `tests/test_turns.py` assert on `window_5h["pct"]` or `["resets_at"]`, update those assertions to read `state["limits"]["rows"]` instead — the figures moved, they did not disappear.

- [ ] **Step 6: Run the whole suite and the smoke test**

Run: `pixi run test && pixi run smoke`
Expected: PASS — `widget.py` still reads the old keys, so the panel will show dollars on the limit rows until Task 4. That is the pre-calibration behaviour, not a break.

- [ ] **Step 7: Commit** (ask Martin first)

```bash
git add cost_meter/summary.py tally.py tests/test_summary.py
git commit -m "feat: carry the account's limit figures through to state.json"
```

---

### Task 4: Draw the account figures, and build the tooltip

**Files:**
- Modify: `widget.py:24` (import), `:60-62` (roll keys), `:199-224` (`window_row`), `:344-364` (row registry), `:560-624` (`refresh`), `:652-676` (`set_stale`), `:678-706` (`draw_row`)
- Test: `tests/test_widget.py:160-215`

**Interfaces:**
- Consumes: `state["limits"]` (Task 3), `utilization.SESSION` / `utilization.WEEKLY` (Task 2).
- Produces: `widget.severity_class(severity, pct) -> str`, `widget.window_expired(limit, now) -> bool`, `widget.window_row(window, limit, now=None) -> tuple[str, str]`, `widget.window_tooltip(window, limit, age_s, now=None) -> str`, `CostMeter.draw_limits()`.
- `now` is a parameter rather than read inside, so the expiry rule is testable without waiting for a real window to reset. It defaults to `time.time()` for the panel's call sites.

- [ ] **Step 1: Write the failing tests**

In `tests/test_widget.py`, replace the `window_row` tests (lines ~168-215) with:

```python
    def test_a_row_with_no_account_figure_shows_dollars_muted(self):
        # No cache, one too old, or another account's: the row reads exactly as
        # it did before any of this existed.
        self.assertEqual(widget.window_row({"usd": 6.4}, None),
                         ("$6.40", "muted"))

    def test_a_row_with_an_account_figure_shows_the_percentage_alone(self):
        # No dollars beside it: they describe this machine and the percentage
        # describes the account, and one row reads as one claim.
        self.assertEqual(
            widget.window_row({"usd": 6.4}, {"pct": 31, "severity": "normal"}),
            ("≥31 %", "green"))

    def test_the_percentage_is_always_marked_as_a_floor(self):
        # Usage within a window only grows, so the figure was true when it was
        # fetched and can only have risen. Unconditional: a marker that came and
        # went would imply the unmarked form is exact, and it never is.
        fresh = widget.window_row({"usd": 1.0}, {"pct": 5, "severity": "normal"})
        self.assertTrue(fresh[0].startswith("≥"), fresh[0])

    def test_the_reset_time_rides_with_the_percentage(self):
        text = widget.window_row(
            {"usd": 51.04},
            {"pct": 6, "severity": "normal", "resets_at": self.iso})[0]
        self.assertTrue(text.startswith("≥6 % · "), text)

    def test_a_window_that_has_already_reset_is_withdrawn(self):
        # The figure describes a window that no longer exists, so no bound
        # survives it -- and age alone cannot detect that.
        past = datetime.fromtimestamp(1000.0, timezone.utc).isoformat()
        self.assertEqual(
            widget.window_row({"usd": 6.4},
                              {"pct": 31, "severity": "normal",
                               "resets_at": past}, now=2000.0),
            ("$6.40", "muted"))

    def test_a_row_with_no_reset_time_never_expires(self):
        self.assertEqual(
            widget.window_row({"usd": 6.4}, {"pct": 31, "severity": "normal",
                                             "resets_at": None},
                              now=2000.0)[0], "≥31 %")

    def test_the_servers_severity_decides_the_colour(self):
        # It knows where the thresholds are, and they move with promotions and
        # plan changes in a way a hardcoded number cannot.
        for severity, expected in (("normal", "green"), ("warning", "amber"),
                                   ("critical", "red")):
            self.assertEqual(widget.severity_class(severity, 5), expected)

    def test_an_unknown_severity_falls_back_to_the_percentage(self):
        # A word this panel has never seen must not be what paints a row at
        # 95 % as safe.
        self.assertEqual(widget.severity_class("brand-new-word", 95), "red")
        self.assertEqual(widget.severity_class(None, 60), "amber")
        self.assertEqual(widget.severity_class(None, 59), "green")

    def test_a_percentage_that_is_not_a_number_is_treated_as_absent(self):
        self.assertEqual(widget.window_row({"usd": 6.4}, {"pct": None}),
                         ("$6.40", "muted"))

    def test_the_tooltip_names_the_scope_of_each_figure(self):
        text = widget.window_tooltip(
            {"usd": 71.46},
            {"pct": 20, "severity": "normal", "resets_at": self.iso}, 1800.0)
        self.assertIn("$71.46 on this machine", text)
        self.assertIn("at least 20 %", text)
        self.assertIn("30 min", text)

    def test_the_tooltip_says_what_refreshes_the_figure(self):
        # The one thing a reader can act on: nothing else moves it.
        text = widget.window_tooltip(
            {"usd": 71.46}, {"pct": 20, "severity": "normal"}, 1800.0)
        self.assertIn("/usage", text)

    def test_the_tooltip_says_so_when_there_is_no_account_figure(self):
        text = widget.window_tooltip({"usd": 71.46}, None, None)
        self.assertIn("$71.46 on this machine", text)
        self.assertIn("no account figure", text)
```

`self.iso` already exists in that test class (used by the current reset-time test). If it does not, add `self.iso = datetime.fromtimestamp(time.time() + 600).astimezone().isoformat()` to its `setUp`.

- [ ] **Step 2: Run them to make sure they fail**

Run: `python -m unittest tests.test_widget -v`
Expected: FAIL — `TypeError: window_row() takes 1 positional argument but 2 were given`

- [ ] **Step 3: Rewrite the row text and add the tooltip**

In `widget.py`, change the import on line 24 to:

```python
from cost_meter import autolaunch, paths, roll, store, summary, utilization  # noqa: E402
```

Replace lines 60-62 with:

```python
# The rolling rows. The two limit rows are not among them any more: they carry an
# integer percentage that moves once every few minutes, which has nothing to
# tween, and the dollars that used to animate there have moved to the tooltip.
ROLL_KEYS = ("last_turn", "session", "today")
TURN_KEY = "last_turn"
WINDOW_KEYS = ("window_5h", "window_7d")
# Which account limit each row draws, named as the server names them.
WINDOW_KINDS = {"window_5h": utilization.SESSION,
                "window_7d": utilization.WEEKLY}
# The server's severity, mapped onto the panel's colour classes.
SEVERITY_CLASSES = {"normal": "green", "warning": "amber", "critical": "red"}
```

Replace `window_row` (lines 199-224) with:

```python
def severity_class(severity, pct):
    """The colour class for a limit row.

    The server's own severity is preferred over a threshold of ours: it knows
    where the thresholds are, and they move — this account is currently carrying
    a +50 % weekly promotion, which no number compiled in here would follow.

    An unrecognised value falls back to the percentage rather than to green. A
    word this panel has never seen must not be what paints a row at 95 % as
    safe.
    """
    return SEVERITY_CLASSES.get(severity) or (
        "red" if pct >= RED_AT else "amber" if pct >= AMBER_AT else "green")


def _pct_of(limit):
    """The row's whole-number percentage, or None if it hasn't got one."""
    pct = (limit or {}).get("pct")
    if isinstance(pct, bool) or not isinstance(pct, int):
        return None
    return pct


def window_expired(limit, now):
    """Whether this figure describes a window that has already reset.

    Age cannot answer this. A figure fetched four hours ago still bounds a weekly
    window, and one fetched twenty minutes ago bounds nothing if the 5-hour block
    turned over in between. The reset time the server sent is what decides.

    A row without a reset time never expires: there is nothing to compare, and
    the older cache shape does not always carry one.
    """
    end = summary.parse_updated_at((limit or {}).get("resets_at"))
    return end is not None and now >= end


def window_row(window, limit, now=None):
    """The text and style class for one limit row, as (text, class).

    With an account figure the row is that figure and nothing else. The
    percentage describes the whole account and the dollars describe this
    machine; two scopes on one row read as one claim, and the reader divides
    them — which is exactly the arithmetic the old calibrated percentage was
    doing wrongly. The dollars move to the tooltip, which has room to say which
    is which.

    The percentage is always marked `≥`. Refresh happens on session start and
    when the user runs `/usage`, so the figure is usually hours old — and usage
    within a window only grows, which makes it a floor rather than a reading.
    Unconditional, because a marker that came and went would imply the unmarked
    form is exact, and it never is: even a twelve-second-old figure has had
    twelve seconds to rise. This is the `~` marker's replacement, and it says
    something stronger — `~` admitted the number could be wrong either way.

    The reset time rides with the percentage, as it always has: what it is for
    is saying which window the figure describes. Once that time has passed the
    row is withdrawn, because the window it described is gone.

    Without an account figure the row falls back to the dollars alone, muted,
    exactly as it read before any of this existed. Interpolating a percentage
    from local dollars would be an invented number.
    """
    now = time.time() if now is None else now
    pct = _pct_of(limit)
    if pct is None or window_expired(limit, now):
        return _fmt_usd(window.get("usd")), "muted"
    resets = _fmt_reset(limit.get("resets_at"))
    tail = "" if resets is None else f" · {resets}"
    return f"≥{pct} %{tail}", severity_class(limit.get("severity"), pct)


def window_tooltip(window, limit, age_s, now=None):
    """The tooltip for one limit row: which figure belongs to which scope.

    This is where the dollar figure went when it left the row, and the only
    place the two scopes are stated rather than implied.

    The age is here rather than on the row because hours-old figures are the
    normal case, so an age beside every percentage would be permanent noise —
    while somebody wondering why a percentage has not moved all afternoon wants
    exactly this, together with the one thing that would move it.
    """
    now = time.time() if now is None else now
    lines = [f"{_fmt_usd(window.get('usd'))} on this machine"]
    pct = _pct_of(limit)
    if pct is None:
        lines.append("no account figure available")
        return "\n".join(lines)
    if window_expired(limit, now):
        lines.append("the account figure describes a window that has reset")
        return "\n".join(lines)
    resets = _fmt_reset(limit.get("resets_at"))
    account = f"account at least {pct} %"
    if resets is not None:
        account += f", resets {resets}"
    lines.append(account)
    if age_s is not None:
        lines.append(f"figure {summary.format_age(age_s)} old; /usage refreshes it")
    else:
        lines.append("/usage refreshes it")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the text tests to make sure they pass**

Run: `python -m unittest tests.test_widget -v`
Expected: PASS for the text tests. The panel class is still calling the old shapes; Step 5 fixes that.

- [ ] **Step 5: Wire the panel**

In `__init__`, after `self.windows = {key: {} for key in WINDOW_KEYS}` (line 364), add:

```python
        # The account's own limit figures from the last state.json, and how old
        # they were when it was written. Held because the limit rows are redrawn
        # from them outside of refresh() — set_stale() re-mutes them.
        self.limits = {}
```

Update the comment at lines 344-347 — `draw_row`'s second owner of `muted` is now `draw_limits`, and the case is "no account figure", not "no calibration":

```python
        # Split because `muted` has two owners: staleness for all five rows, and
        # draw_limits for the two window rows when there is no account figure.
        # The billing row rides with the plain ones — it is drawn once from
        # state.json and only staleness ever mutes it.
```

In `refresh`, replace the roll targets (lines 580-586) with:

```python
        for key in WINDOW_KEYS:
            self.windows[key] = state.get(key) or {}
        self.limits = state.get("limits") or {}
        rolling = self.roll.retarget({
            "session": (state.get("session") or {}).get("usd"),
            "today": state.get("today_usd"),
        }) or turn_rolling
```

and after the `for key in ROLL_KEYS: self.draw_row(key)` loop (line 600-601) add:

```python
        self.draw_limits()
```

In `set_stale`, replace the trailing comment (lines 674-676):

```python
        # When fresh, the window rows are left alone: draw_limits has already
        # set or cleared their `muted` for the no-account-figure case, and
        # clearing it here would present a bare dollar figure as an account one.
```

Delete the `if key in WINDOW_KEYS:` branch from `draw_row` (lines 699-701) so it reads:

```python
        value = self.roll.shown(key)
        if key == TURN_KEY:
            text = turn_text(value, self.roll.moving(key))
        else:
            text = _fmt_usd(value)
```

and add the new method beside it:

```python
    def draw_limits(self):
        """Paint both limit rows from the account figures in state.json.

        Separate from draw_row because these rows do not animate: there is no
        tween between 11 % and 12 %, and the figure behind them is refreshed on
        a five-minute floor rather than per turn. They are painted when
        state.json changes and left alone in between.
        """
        rows = self.limits.get("rows") or {}
        age = self.limits.get("age_s")
        for key in WINDOW_KEYS:
            window = self.windows[key]
            limit = rows.get(WINDOW_KINDS[key])
            label = self.rows[key]
            context = label.get_style_context()
            for name in LIMIT_CLASSES + ("muted",):
                context.remove_class(name)
            text, style = window_row(window, limit)
            context.add_class(style)
            label.set_text(text)
            label.set_tooltip_text(window_tooltip(window, limit, age))
```

Also update `draw_row`'s docstring: drop the paragraph about limit rows rebuilding composite text, since they no longer go through it.

- [ ] **Step 6: Run everything, including a real render**

Run: `pixi run test && pixi run smoke`
Expected: PASS. `widget.py --selftest` renders a frame; it does not exercise tooltips, which is why their text is a pure function.

- [ ] **Step 7: See it on screen**

Run: `pixi run show`
Expected: the 5h row reads like `≥12 % · 18:30` in green, the week row like `≥17 % · Sat 02:59`, and hovering either shows the dollars with each scope named plus the figure's age. Run `/usage` in Claude Code and then `pixi run tally` — the panel's percentages should agree with `/usage` exactly, because it is the same source.

- [ ] **Step 8: Commit** (ask Martin first)

```bash
git add widget.py tests/test_widget.py
git commit -m "feat: draw account limit percentages, with the dollars in a tooltip"
```

---

### Task 5: Delete the calibration machinery

**Files:**
- Delete: `calibrate.py`, `limit.py`, `cost_meter/ceilings.py`, `tests/test_calibrate.py`, `tests/test_ceilings.py`, `tests/test_limit.py`
- Modify: `pixi.toml:1-8` (header comment), `:66-72` (the two tasks)
- Modify: `cost_meter/store.py:109-116` (`update_json_locked` docstring)

**Interfaces:**
- Consumes: nothing. Everything these modules produced (`ceiling_5h_usd`, `ceiling_7d_usd`) is now unread.
- Produces: nothing.

- [ ] **Step 1: Confirm nothing still imports them**

Run: `grep -rn -E 'ceilings|calibrate|limit\.py|ceiling_' --include='*.py' --include='*.toml' --include='*.sh' --include='*.cmd' .`
Expected: hits only in the six files being deleted, in `pixi.toml`, and in the two comments listed above. Any other hit is a caller this task must fix before deleting.

- [ ] **Step 2: Delete the modules and their tests**

```bash
git rm calibrate.py limit.py cost_meter/ceilings.py \
       tests/test_calibrate.py tests/test_ceilings.py tests/test_limit.py
```

- [ ] **Step 3: Drop the two pixi tasks**

In `pixi.toml`, delete the `calibrate` and `limit` task entries with their comments (lines 66-72), and fix the header comment on lines 4-6, which names `calibrate.py`:

```toml
# Only widget.py needs anything from here: it imports gi (PyGObject) and
# requires GTK 3. Everything else -- tally.py, cost_meter/ and the tests -- is
# standard library only. The environment still owns every entry point anyway, so
# there is exactly one interpreter to reason about and the documented commands
# are identical on every machine.
```

- [ ] **Step 4: Fix the one stale comment in store.py**

`update_json_locked`'s docstring names `ceilings.py` as a writer of `config.json`. It no longer exists, and the lock is still needed — the panel writes its position and scale there:

```python
    """Read-modify-write a JSON file with the lock given across both halves.

    config.json has more than one writer — the panel stores its position and its
    scale there, from a process that is not the hook — and an unlocked
    read-modify-write silently drops whichever value the other side wrote in
    between.
    """
```

- [ ] **Step 5: Run everything**

Run: `pixi run test && pixi run smoke`
Expected: PASS, with three fewer test files. `pixi run tally && pixi run show` should still bring up a working panel.

- [ ] **Step 6: Commit** (ask Martin first)

```bash
git add -A
git commit -m "refactor: drop the calibration machinery the account figures replace"
```

---

### Task 6: Tell the truth in the README

**Files:**
- Modify: `README.md` — jump links (line ~11), `## Calibrate` and `## Un-calibrate` sections (~66-105), the `Declaring a known limit` section, panel rows (~200-215), known limitations (~173-197), and the 5-hour block explanation (~217-227)

**Interfaces:**
- Consumes: the behaviour built in Tasks 1-4.
- Produces: nothing code depends on.

- [ ] **Step 1: Rewrite the sections**

Six edits, each removing a claim that is now false:

1. **Jump links** — drop `Calibrate`, `Un-calibrate` and `Declaring a known limit`.
2. **Delete `## Calibrate`, `## Un-calibrate` and `## Declaring a known limit`** outright. There is nothing to calibrate; replace them with a short `## Limit percentages` section:

```markdown
## Limit percentages

The two limit rows are the account's own figures, not this machine's. Claude Code
asks the server how much of each limit the account has used and caches the answer
in `~/.claude.json`; the panel reads it from there. Nothing to calibrate, and
nothing to re-calibrate when your plan or a promotion moves the ceiling.

**`≥` is not hedging.** Claude Code re-asks the server when a session starts and
when you run `/usage`, and at no other time — so the figure on the row is usually
hours old. Usage within a window only ever grows, which makes an old percentage a
floor rather than a guess: `≥17 %` means at least 17 %, never less. Run `/usage`
to pull a fresh one; hover the row to see how old the current one is and what
this machine has spent in that window.

A row goes back to showing dollars alone when there is no account figure to show
— you have never run Claude Code on this machine, the cache belongs to a
different login, or the window it described has since reset.
```

3. **Panel rows** — the two limit rows no longer carry dollars:

```markdown
- **5h window** — how much of the account's 5-hour limit is used, as a floor:
  `≥12 % · 18:30`, where `18:30` is the clock time that block resets. Hover for
  what this machine spent in the block and how old the figure is. `$6.40` on its
  own means no account figure was available.
- **week** — the same for the weekly limit, with its own reset time
```

4. **Known limitations** — delete the bullet calling the percentages estimates derived from calibration, and the three paragraphs about recalibrating and declaring a ceiling. The remaining machine-scope limitation applies to the **dollar** rows only, so say that:

```markdown
- **The dollar figures cover this machine only.** They are computed from the
  Claude Code transcripts under `~/.claude/projects/` on the computer the tool
  runs on. Work done on another machine is absent from them. The limit
  percentages are not affected — those come from the account.
```

5. **The 5-hour block explanation** — it describes deriving the block from local messages. The server now names the reset, so the paragraph about checking the panel's time against `/usage` before trusting the percentage goes; keep the explanation of *why* the limit is a fixed block rather than a trailing five hours, since that is still what the dollar figure measures.

6. **Anywhere `~` is explained** as the estimate marker — it is gone from the limit rows.

- [ ] **Step 2: Check no dead links or commands remain**

Run: `grep -n -iE 'calibrat|ceiling|~[0-9]|pixi run (limit|calibrate)' README.md`
Expected: no output.

- [ ] **Step 3: Run the smoke test**

Run: `pixi run smoke`
Expected: PASS. (Nothing in the README is executable, but the hook runs it anyway and a green run is the convention.)

- [ ] **Step 4: Commit** (ask Martin first)

```bash
git add README.md
git commit -m "docs: describe the account's limit figures, not calibration"
```

---

## Self-review

**Spec coverage.** `utilization.py` → Task 2. `paths.py` → Task 1. `summary.py` (`limits` block, anchor-bounded window, `pct` removal) → Task 3. Widget (percentage rows, `~` dropped, severity colours, withdrawal when stale, tooltip, week reset time) → Task 4. Deletions → Task 5. README → Task 6. Testing section → the test steps in Tasks 1-4. `weekly_scoped` parsed but undrawn → Task 2 Step 1/3 and the `WINDOW_KINDS` map in Task 4.

**One spec statement this plan corrects:** the spec's `state.json` sketch says the `limits` block carries `age_s` *and* `stale`. It carries `age_s` only — `utilization.read()` returns `None` when the cache is too old, so absence already means stale and a second flag could contradict it. Fix the spec line when this plan is approved.

**Type consistency.** `window_row(window, limit)` and `window_tooltip(window, limit, age_s)` take the same `limit` dict shape `utilization._row()` produces, keyed by the same `utilization.SESSION` / `utilization.WEEKLY` constants that `WINDOW_KINDS` and `anchor_block` use. `build_state`'s sixth parameter is the whole `utilization.read()` result (`{"age_s", "rows"}`) in Tasks 3 and 4 alike, and `state["limits"]` is that same object.
