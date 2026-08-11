# Declared limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `pixi run limit` task that writes a known ceiling straight into `config.json`, so the 5h and week rows can show a percentage on an installation where calibration cannot work.

**Architecture:** A new `cost_meter/ceilings.py` becomes the sole owner of the `ceiling_*_usd` keys and of the shared clear-and-report behaviour. `calibrate.py` (derive from a ratio) and a new `limit.py` (declare outright) become thin front ends over it. The read path — `summary.py`, `widget.py`, the shape of `config.json` — is untouched.

**Tech Stack:** Python 3.14, standard library only. `unittest`, not pytest. pixi for every entry point.

## Global Constraints

- **Standard library only.** Only `widget.py` may import anything else (`gi`/GTK). Nothing in this plan touches it.
- **Both platforms.** Linux and Windows run the identical command; no bash-only constructs, no POSIX-only calls.
- **`store.exclusive_lock` is not reentrant.** A config write and a refresh must be sequential lock holds, never nested. See `calibrate.py:136-139`.
- **`config.json` has three writers** — the two ceilings and the panel's `widget_position`. Every mutation goes through `store.update_json_locked`; a wholesale `write_json_atomic` would drop whichever value the writing side does not know about.
- **Print only after the write lands.** A run must never report a ceiling it failed to persist.
- **Tests are `unittest.TestCase`.** Full suite: `pixi run test`. Single module: `pixi run --frozen python -m unittest discover -s tests -p <file>.py -v` (verified: modules import as top-level names, so `from support import TempHome` resolves).
- **Every value written to a ceiling key is a divisor.** It must be finite and greater than zero.

## Deviations from the spec

Three, all found while writing this plan. Each is a deliberate change, not a reinterpretation.

1. **`TempHome` moves to `tests/support.py` and redirects `COST_METER_TRANSCRIPTS` too.** The spec's §6 only moved `ClearCeilingsTest`. But three modules now need the base class, and — more importantly — any test that reaches `main()` reaches `tally.refresh`, which scans `paths.transcripts_root()`. Left at its default that is the user's real `~/.claude/projects`: the test would read live data and take as long as the real ledger is large. `test_tally.py:40` already sets both variables for this reason.
2. **Two output strings change, not one.** The spec counted only `calibration removed, back to dollars`. `calibrate.py:70` also prints `was not calibrated, nothing to remove`, which names the derivation just as wrongly for a declared ceiling. Both move into `ceilings.py` as constants.
3. **`ceilings.clear` takes `refresh` as a parameter.** The spec kept refresh out of `cost_meter/` to avoid a package importing a root-level module, and accepted ~6 duplicated lines as the price. That price is wrong here: §2 of the spec insists the two tools' clear behaviour must be *identical*, and duplication is the wrong tool for code that must not diverge. Injecting `refresh` keeps `cost_meter/` free of upward imports **and** single-sources the behaviour — and it lets the tests drive `clear` without any transcript scan at all. `set_ceilings` needs no injection; only `clear` has two callers.

## Out of scope, found on the way

`tests/test_install.py:63-69` defines its own `TempHome` whose teardown *pops* `COST_METER_HOME` instead of restoring it — the exact fault `test_calibrate.py:14-17` documents. Since `discover` runs `test_install` second alphabetically, every module after it runs with the variable unset. Not touched by this plan; reported separately.

---

### Task 1: Move the ceiling keys, the clear behaviour and the shared test base

**Files:**
- Create: `cost_meter/ceilings.py`
- Create: `tests/support.py`
- Create: `tests/test_ceilings.py`
- Modify: `calibrate.py:21-27` (imports, `CEILINGS`), `calibrate.py:46-83` (delete `clear_ceilings` and `clear`), `calibrate.py:92-93,109` (call sites)
- Modify: `tests/test_calibrate.py:1-72` (delete `TempHome` and `ClearCeilingsTest`, import from `support`)

**Interfaces:**
- Consumes: `store.update_json_locked`, `paths.config_path`, `paths.lock_path`, `store.LockTimeout`, `tally.refresh(session_id, now=None)`.
- Produces: `ceilings.CEILINGS: dict[str, str]`, `ceilings.REMOVED: str`, `ceilings.NOT_SET: str`, `ceilings.clear_ceilings(keys: list[str]) -> list[str]`, `ceilings.clear(labelled_keys: list[tuple[str, str]], refresh) -> int`, and `support.TempHome` with `.config()` and `.write_config(dict)`.

- [ ] **Step 1: Write the shared test base**

Create `tests/support.py`:

```python
# tests/support.py
"""Shared base for tests that write into COST_METER_HOME.

The variable is *restored* rather than unset on teardown: run_tests.py points it
at one throwaway directory for the whole run, so a test that removed it would
send every test after it at the real data/ directory.

COST_METER_TRANSCRIPTS is redirected as well. A test that reaches a front end's
main() reaches tally.refresh, which scans the transcript root; left at its
default that is the user's real ~/.claude/projects, and the test would read live
data and take as long as the real ledger is large.
"""

import os
import tempfile
import unittest

from cost_meter import paths, store


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.transcripts = os.path.join(self.tmp, "transcripts")
        os.makedirs(self.transcripts)
        for name, value in (("COST_METER_HOME", self.tmp),
                            ("COST_METER_TRANSCRIPTS", self.transcripts)):
            self.addCleanup(self._restore, name, os.environ.get(name))
            os.environ[name] = value

    @staticmethod
    def _restore(name, previous):
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    def config(self):
        return store.read_json(paths.config_path(), default={}) or {}

    def write_config(self, config):
        store.write_json_atomic(paths.config_path(), config)
```

- [ ] **Step 2: Write the failing tests for the new module**

Create `tests/test_ceilings.py`. The five `ClearCeilingsTest` cases are moved verbatim from `tests/test_calibrate.py:39-72`; the last two classes are new and pin what Task 1 adds.

```python
# tests/test_ceilings.py
"""The two ceiling keys, and the one clear path both front ends share."""
import unittest

from support import TempHome

from cost_meter import ceilings


class ClearCeilingsTest(TempHome):
    def test_a_calibrated_ceiling_is_removed_and_reported(self):
        self.write_config({"ceiling_5h_usd": 40.0, "ceiling_7d_usd": 120.0})
        self.assertEqual(ceilings.clear_ceilings(["ceiling_5h_usd"]),
                         ["ceiling_5h_usd"])
        # Only the window asked for: clearing the 5h row must leave the week
        # calibrated, or one flag would quietly undo both calibrations.
        self.assertEqual(self.config(), {"ceiling_7d_usd": 120.0})

    def test_both_can_go_at_once(self):
        self.write_config({"ceiling_5h_usd": 40.0, "ceiling_7d_usd": 120.0})
        self.assertEqual(
            ceilings.clear_ceilings(["ceiling_5h_usd", "ceiling_7d_usd"]),
            ["ceiling_5h_usd", "ceiling_7d_usd"])
        self.assertEqual(self.config(), {})

    def test_clearing_what_was_never_calibrated_is_not_an_error(self):
        self.write_config({"ceiling_7d_usd": 120.0})
        self.assertEqual(ceilings.clear_ceilings(["ceiling_5h_usd"]), [])
        self.assertEqual(self.config(), {"ceiling_7d_usd": 120.0})

    def test_clearing_against_no_config_at_all_is_not_an_error(self):
        self.assertEqual(ceilings.clear_ceilings(["ceiling_5h_usd"]), [])

    def test_the_saved_window_position_survives(self):
        # config.json has three owners: the two ceilings and the panel's window
        # position. A wholesale rewrite rather than a read-modify-write under the
        # lock would drop whichever value this side does not know about.
        self.write_config({"ceiling_5h_usd": 40.0, "widget_position": [100, 200]})
        ceilings.clear_ceilings(["ceiling_5h_usd"])
        self.assertEqual(self.config(), {"widget_position": [100, 200]})


class WordingTest(unittest.TestCase):
    """Neither message may name calibration: a declared ceiling never was."""

    def test_neither_message_mentions_calibration(self):
        self.assertNotIn("calibrat", ceilings.REMOVED)
        self.assertNotIn("calibrat", ceilings.NOT_SET)

    def test_both_messages_name_the_ceiling(self):
        self.assertIn("ceiling", ceilings.REMOVED)
        self.assertIn("ceiling", ceilings.NOT_SET)


class ClearTest(TempHome):
    """`clear` reports per window and refreshes once, whatever it removed."""

    def setUp(self):
        super().setUp()
        self.refreshed = []

    def refresh(self, session_id):
        self.refreshed.append(session_id)

    def test_a_removed_ceiling_is_reported_as_removed(self):
        self.write_config({"ceiling_7d_usd": 2000.0})
        lines = []
        code = ceilings.clear([("week", "ceiling_7d_usd")], self.refresh,
                              report=lines.append)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [f"week: {ceilings.REMOVED}"])
        self.assertEqual(self.config(), {})

    def test_an_absent_ceiling_is_reported_as_absent(self):
        lines = []
        ceilings.clear([("week", "ceiling_7d_usd")], self.refresh,
                       report=lines.append)
        self.assertEqual(lines, [f"week: {ceilings.NOT_SET}"])

    def test_the_panel_is_refreshed_exactly_once(self):
        # state.json still carries the percentage this call invalidated, and the
        # panel redraws from the file monitor: without the refresh the row would
        # keep showing it until the next assistant turn.
        self.write_config({"ceiling_5h_usd": 40.0, "ceiling_7d_usd": 2000.0})
        ceilings.clear([("5h window", "ceiling_5h_usd"),
                        ("week", "ceiling_7d_usd")], self.refresh,
                       report=lambda line: None)
        self.assertEqual(self.refreshed, [""])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pixi run --frozen python -m unittest discover -s tests -p test_ceilings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cost_meter.ceilings'`

- [ ] **Step 4: Write the module**

Create `cost_meter/ceilings.py`:

```python
# cost_meter/ceilings.py
"""The two ceiling values in config.json, and the only writers of them.

A ceiling is the divisor summary.py turns spend into a percentage with. It
reaches config.json two ways -- derived from a reported percentage by
calibrate.py, declared outright by limit.py -- and this module exists so those
two cannot grow separate notions of the same key. There is one ceiling per
window: whichever tool set it, either tool clears it, through the one `clear`
below.

`refresh` is a parameter rather than an import. tally is a root-level module and
a package reaching upwards for it would invert the layering every other module
in cost_meter/ observes; injecting it also lets the tests drive `clear` without
a transcript scan.
"""

from . import paths, store

CEILINGS = {"5h window": "ceiling_5h_usd", "week": "ceiling_7d_usd"}

# Printed by both front ends, and neither names calibration: a ceiling that
# limit.py declared was never calibrated, so wording that named the derivation
# would be wrong for half the ways one can arrive.
REMOVED = "ceiling removed, back to dollars"
NOT_SET = "no ceiling was set, nothing to remove"


def clear_ceilings(keys):
    """Remove `keys` from config.json. Returns the ones that were really set.

    Read-modify-write under the lock, like every other writer of this file: the
    panel keeps its window position here too, and a wholesale rewrite would drop
    whichever value this side does not know about.

    Clearing something already clear is not an error. The flag names the state
    you want, not a transition you have to be mid-way through, which is what
    makes it safe to run twice.
    """
    with store.update_json_locked(paths.config_path(), paths.lock_path()) as config:
        return [key for key in keys if config.pop(key, None) is not None]


def set_ceilings(mapping):
    """Write `{config key: usd}` in one lock hold, preserving every other key."""
    with store.update_json_locked(paths.config_path(), paths.lock_path()) as config:
        config.update(mapping)


def clear(labelled_keys, refresh, report=print, warn=None):
    """Drop the named ceilings and refresh, so the panel redraws at once.

    Shared by both front ends deliberately. There is one ceiling per window, so
    `calibrate --clear-week` and `limit --clear-week` are the same operation and
    must not be able to drift into printing different things.

    Returns a process exit code.
    """
    warn = report if warn is None else warn
    try:
        removed = clear_ceilings([key for _, key in labelled_keys])
    except store.LockTimeout as exc:
        warn(f"could not clear the ceiling: {exc}")
        return 1
    for label, key in labelled_keys:
        report(f"{label}: {REMOVED if key in removed else NOT_SET}")

    # state.json still carries the percentages this run just invalidated, and the
    # panel redraws from the file monitor, so without this the rows would keep
    # showing them until the next assistant turn.
    try:
        with store.exclusive_lock(paths.lock_path()):
            refresh(session_id="")
    except store.LockTimeout as exc:
        warn(f"ceiling cleared, but the refresh could not run: {exc}")
        return 1
    return 0
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pixi run --frozen python -m unittest discover -s tests -p test_ceilings.py -v`
Expected: PASS — 10 tests OK

- [ ] **Step 6: Rewire `calibrate.py` onto the shared module**

Replace `calibrate.py:21-27` (the imports and the local `CEILINGS`) with:

```python
import argparse
import sys

from cost_meter import ceilings, paths, store
from cost_meter.ceilings import CEILINGS
from tally import refresh
```

Delete `calibrate.py:46-83` entirely — both `clear_ceilings` and `clear` now live in `ceilings.py`. In `main`, replace the `clearing` branch at `calibrate.py:108-109`:

```python
    if clearing:
        return ceilings.clear(clearing, refresh,
                              warn=lambda line: print(line, file=sys.stderr))
```

Leave everything else in `calibrate.py` untouched: the parser, the validation, the zero-spend guard at `130-134`, and the derive-and-write block at `143-155` (which keeps using `store.update_json_locked` directly, because it computes each value from state while holding the lock).

- [ ] **Step 7: Strip the moved code out of `tests/test_calibrate.py`**

Replace `tests/test_calibrate.py:1-72` — the module docstring, the imports, `TempHome` and all of `ClearCeilingsTest` — with:

```python
# tests/test_calibrate.py
"""Calibrate's argument contract. The ceiling keys themselves live in
tests/test_ceilings.py, alongside the module that now owns them."""
import unittest

import calibrate
```

`ClearArgumentTest` at `tests/test_calibrate.py:75-94` stays exactly as it is, as does the `unittest.main()` footer.

- [ ] **Step 8: Run the full suite**

Run: `pixi run test`
Expected: PASS — every test, with the count unchanged apart from the four new ones in `test_ceilings.py` (`WordingTest` ×2, `ClearTest` ×3 minus none: 5 moved + 5 new = 10 in the new file).

- [ ] **Step 9: Run the smoke test**

Run: `pixi run smoke`
Expected: OK, GTK render included. Nothing in this task touches the panel; this proves it.

- [ ] **Step 10: Commit**

```bash
git add cost_meter/ceilings.py tests/support.py tests/test_ceilings.py calibrate.py tests/test_calibrate.py
git commit -m "refactor: give the ceiling keys one owner

clear_ceilings and the clear-and-report path move out of calibrate.py into
cost_meter/ceilings.py, ahead of a second front end that writes the same two
keys. refresh is injected rather than imported, so the package does not reach
upwards for a root-level module and the tests can drive clear without scanning
a transcript tree.

Both printed messages stop naming calibration: a declared ceiling never was
calibrated, and the same lines serve both tools."
```

---

### Task 2: Declare a ceiling — the CLI contract

**Files:**
- Create: `limit.py`
- Create: `tests/test_limit.py`

This task deliberately registers no pixi task. `main` and the `__main__` footer
arrive in Task 3, and until they do `python limit.py --help` would define the
parser and exit silently without printing anything — a task that looked wired up
and answered nothing. The deliverable here is the argument contract, and
`unittest` is what exercises it.

**Interfaces:**
- Consumes: `ceilings.CEILINGS`.
- Produces: `limit.build_parser() -> argparse.ArgumentParser`; `limit.plan(parser, args) -> tuple[list[tuple[str, str, float]], list[tuple[str, str]]]` returning `(to_set, to_clear)`, where a `to_set` entry is `(label, config_key, usd)` and a `to_clear` entry is `(label, config_key)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_limit.py`:

```python
# tests/test_limit.py
"""Limit's argument contract: what it accepts, and what it refuses to."""
import unittest

import limit


class ParseTest(unittest.TestCase):
    def test_both_windows_parse(self):
        args = limit.build_parser().parse_args(["--5h", "130", "--week", "2000"])
        self.assertEqual(args.five_hour, 130.0)
        self.assertEqual(args.week, 2000.0)

    def test_a_bare_run_is_rejected(self):
        parser = limit.build_parser()
        with self.assertRaises(SystemExit):
            limit.plan(parser, parser.parse_args([]))


class PlanTest(unittest.TestCase):
    def build(self, argv):
        parser = limit.build_parser()
        return limit.plan(parser, parser.parse_args(argv))

    def test_a_declared_week_becomes_one_write(self):
        self.assertEqual(self.build(["--week", "2000"]),
                         ([("week", "ceiling_7d_usd", 2000.0)], []))

    def test_both_windows_become_two_writes(self):
        to_set, to_clear = self.build(["--5h", "130", "--week", "2000"])
        self.assertEqual(to_set, [("5h window", "ceiling_5h_usd", 130.0),
                                  ("week", "ceiling_7d_usd", 2000.0)])
        self.assertEqual(to_clear, [])

    def test_clear_expands_to_both_windows(self):
        to_set, to_clear = self.build(["--clear"])
        self.assertEqual(to_set, [])
        self.assertEqual(to_clear, [("5h window", "ceiling_5h_usd"),
                                    ("week", "ceiling_7d_usd")])

    def test_clear_week_expands_to_one(self):
        self.assertEqual(self.build(["--clear-week"]),
                         ([], [("week", "ceiling_7d_usd")]))


class RejectionTest(unittest.TestCase):
    def reject(self, argv):
        parser = limit.build_parser()
        with self.assertRaises(SystemExit):
            limit.plan(parser, parser.parse_args(argv))

    def test_zero_is_rejected(self):
        # It becomes a divisor.
        self.reject(["--week", "0"])

    def test_a_negative_ceiling_is_rejected(self):
        self.reject(["--week", "-5"])

    def test_nan_is_rejected(self):
        # argparse's float() accepts the spelling; _pct would inherit the poison.
        self.reject(["--week", "nan"])

    def test_inf_is_rejected(self):
        self.reject(["--week", "inf"])

    def test_setting_and_clearing_in_one_run_is_rejected(self):
        # Contradictory, and worse than useless if half-applied.
        self.reject(["--week", "2000", "--clear"])

    def test_clearing_one_window_while_declaring_the_other_is_rejected(self):
        self.reject(["--clear-5h", "--week", "2000"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run --frozen python -m unittest discover -s tests -p test_limit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'limit'`

- [ ] **Step 3: Write the parser and the plan function**

Create `limit.py`:

```python
#!/usr/bin/env python3
"""Declare a known subscription ceiling instead of deriving one from /usage.

calibrate.py turns a percentage /usage reported into a ceiling by dividing this
installation's recorded spend by it. That pairs two numbers from different
scopes -- the spend is this machine's, the percentage is the whole account's --
which holds up only while this machine's share of the account stays roughly
constant. Where the work is split across machines habitually rather than moved
occasionally, no reading of /usage yields a correct ceiling at all: the number
is not approximate, it is unobtainable.

A limit you already know sidesteps the ratio. The percentage then means this
installation's measured share of a known bound rather than an estimate of
account-wide consumption -- a smaller claim, and a true one. It stays marked
`~` on the panel, because it understates the account by however much the other
machines contribute.

Usage (the bare -- keeps pixi from reading --5h as one of its own flags):
    pixi run limit -- --week 2000    # the weekly cap, in USD-equivalent
    pixi run limit -- --5h 130       # the current 5-hour block's cap
    pixi run limit -- --clear        # forget both, back to dollar figures only
"""

import argparse
import math
import sys

from cost_meter import ceilings
from cost_meter.ceilings import CEILINGS


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--5h", dest="five_hour", type=float,
                        help="ceiling for the 5-hour block, in USD-equivalent")
    parser.add_argument("--week", dest="week", type=float,
                        help="ceiling for the week, in USD-equivalent")
    parser.add_argument("--clear", action="store_true",
                        help="forget both ceilings; the rows go back to dollar "
                             "figures with no percentage")
    parser.add_argument("--clear-5h", dest="clear_5h", action="store_true",
                        help="forget the 5-hour ceiling only")
    parser.add_argument("--clear-week", dest="clear_week", action="store_true",
                        help="forget the weekly ceiling only")
    return parser


def plan(parser, args):
    """Turn parsed arguments into `(to_set, to_clear)`, or exit via parser.error.

    Everything is validated here, before any file is touched, so a run either
    applies all of what it was asked for or changes nothing. The three clear
    flags mirror calibrate's exactly: there is one ceiling per window, so
    offering a subset would imply a distinction that does not exist downstream.
    """
    to_set = [
        (label, CEILINGS[label], value)
        for label, value in (("5h window", args.five_hour), ("week", args.week))
        if value is not None
    ]
    to_clear = [
        (label, CEILINGS[label])
        for label, flag in (("5h window", args.clear or args.clear_5h),
                            ("week", args.clear or args.clear_week))
        if flag
    ]

    if not to_set and not to_clear:
        parser.error("give at least one of --5h, --week, --clear, --clear-5h "
                     "or --clear-week")
    if to_set and to_clear:
        # Contradictory in one run, and worse than useless if half-applied:
        # rejected up front rather than resolved by argument order.
        parser.error("--clear flags cannot be combined with --5h or --week; "
                     "run them separately")

    for _, _, value in to_set:
        if not math.isfinite(value):
            # argparse's float() accepts "nan" and "inf": neither fails on the
            # way in, and both would poison summary._pct on the way out.
            parser.error("a ceiling must be a finite number")
        if value <= 0:
            parser.error("a ceiling must be greater than zero; it is the "
                         "divisor the percentage comes from")
    return to_set, to_clear
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run --frozen python -m unittest discover -s tests -p test_limit.py -v`
Expected: PASS — 12 tests OK

- [ ] **Step 5: Commit**

```bash
git add limit.py tests/test_limit.py
git commit -m "feat: add the limit CLI contract

Parses and validates a declared ceiling without writing anything yet. Rejects
non-finite and non-positive values -- argparse's float() accepts nan and inf,
and either would poison the percentage rather than fail -- and refuses setting
and clearing in one run, as calibrate does.

The three clear flags mirror calibrate's exactly: one ceiling per window means
a subset would imply a distinction the read path does not make."
```

---

### Task 3: Declare a ceiling — the write path

**Files:**
- Modify: `limit.py` (add `main`, the `tally` import, and the `__main__` footer)
- Modify: `tests/test_limit.py` (add the write-path cases)
- Modify: `pixi.toml:59-61` (register the task after `calibrate`)

**Interfaces:**
- Consumes: `limit.build_parser`, `limit.plan`, `ceilings.set_ceilings`, `ceilings.clear`, `ceilings.REMOVED`, `store.LockTimeout`, `tally.refresh`.
- Produces: `limit.main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_limit.py`, before the `unittest.main()` footer. Note these subclass `TempHome`, so both environment variables are redirected and `main` never reads the real ledger or the real transcripts.

```python
class WriteTest(TempHome):
    """main() writes, reports, and refreshes -- in that order."""

    def test_a_declared_week_lands_in_config(self):
        self.assertEqual(limit.main(["--week", "2000"]), 0)
        self.assertEqual(self.config()["ceiling_7d_usd"], 2000.0)

    def test_both_windows_land_together(self):
        limit.main(["--5h", "130", "--week", "2000"])
        self.assertEqual(self.config(), {"ceiling_5h_usd": 130.0,
                                         "ceiling_7d_usd": 2000.0})

    def test_a_declaration_needs_no_spend(self):
        # The case calibrate refuses: with nothing recorded there is no ratio to
        # take, but a declared ceiling does not come from one. TempHome's
        # transcript directory is empty, so no event exists in any window.
        self.assertEqual(limit.main(["--5h", "130"]), 0)
        self.assertEqual(self.config()["ceiling_5h_usd"], 130.0)

    def test_the_saved_window_position_survives(self):
        self.write_config({"widget_position": [100, 200]})
        limit.main(["--week", "2000"])
        self.assertEqual(self.config(), {"widget_position": [100, 200],
                                         "ceiling_7d_usd": 2000.0})

    def test_a_declaration_replaces_a_derived_ceiling(self):
        # One ceiling per window: whichever tool wrote it, the other overwrites.
        self.write_config({"ceiling_7d_usd": 28.58})
        limit.main(["--week", "2000"])
        self.assertEqual(self.config()["ceiling_7d_usd"], 2000.0)

    def test_a_rejected_run_writes_nothing(self):
        self.write_config({"ceiling_7d_usd": 2000.0})
        with self.assertRaises(SystemExit):
            limit.main(["--week", "0"])
        self.assertEqual(self.config(), {"ceiling_7d_usd": 2000.0})

    def test_clearing_goes_through_the_shared_path(self):
        self.write_config({"ceiling_7d_usd": 2000.0})
        self.assertEqual(limit.main(["--clear-week"]), 0)
        self.assertEqual(self.config(), {})
```

Add `from support import TempHome` to the imports at the top of the file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run --frozen python -m unittest discover -s tests -p test_limit.py -v`
Expected: FAIL — `AttributeError: module 'limit' has no attribute 'main'`

- [ ] **Step 3: Write `main`**

Append to `limit.py`:

```python
def main(argv=None):
    parser = build_parser()
    to_set, to_clear = plan(parser, parser.parse_args(argv))

    if to_clear:
        return ceilings.clear(to_clear, refresh,
                              warn=lambda line: print(line, file=sys.stderr))

    # Written first and reported second, so a run can never print a ceiling it
    # failed to persist. One lock hold for every window named, so a two-window
    # run cannot half-apply.
    try:
        ceilings.set_ceilings({key: value for _, key, value in to_set})
    except store.LockTimeout as exc:
        print(f"could not save the ceiling: {exc}", file=sys.stderr)
        return 1
    for label, _, value in to_set:
        print(f"{label}: ceiling set to ${value:.2f} (declared)")

    # The panel redraws from the file monitor, so without this the rows would
    # keep showing no percentage until the next assistant turn. A separate lock
    # hold: exclusive_lock is not reentrant, and set_ceilings has released.
    try:
        with store.exclusive_lock(paths.lock_path()):
            refresh(session_id="")
    except store.LockTimeout as exc:
        print(f"ceiling saved, but the refresh could not run: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Extend the imports at the top of `limit.py` to cover what `main` now uses.
`refresh` comes in at module scope, matching `calibrate.py:24` — the codebase's
established pattern, and worth more here than saving `tally`'s import on a
`--help` run:

```python
from cost_meter import ceilings, paths, store
from cost_meter.ceilings import CEILINGS
from tally import refresh
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run --frozen python -m unittest discover -s tests -p test_limit.py -v`
Expected: PASS — 19 tests OK

- [ ] **Step 5: Run the full suite and the smoke test**

Run: `pixi run test`
Expected: PASS, every module.

Run: `pixi run smoke`
Expected: OK, GTK render included.

- [ ] **Step 6: Register the pixi task, then verify by hand against the real ledger**

Add to `pixi.toml`, immediately after the `calibrate` entry at line 61:

```toml
# Declaring a ceiling you already know, rather than deriving one from a reported
# percentage. Same bare `--` as calibrate: pixi run limit -- --week 2000
limit = "python limit.py"
```

Run: `pixi run limit -- --help`
Expected: the usage block listing `--5h`, `--week`, `--clear`, `--clear-5h`, `--clear-week`.

Run: `pixi run limit -- --week 2000`
Expected: `week: ceiling set to $2000.00 (declared)`, the week row on the panel gains a `~N %`, and `data/config.json` still holds its `widget_position`.

Run: `pixi run calibrate -- --clear-week`
Expected: `week: ceiling removed, back to dollars` — the same line `limit -- --clear-week` prints, since both call the one shared path.

- [ ] **Step 7: Commit**

```bash
git add limit.py tests/test_limit.py pixi.toml
git commit -m "feat: write a declared ceiling to config.json

Reports after the write lands and refreshes in a separate lock hold, as
calibrate does. Needs no recorded spend, which is the case calibrate refuses:
a declared ceiling does not come from a ratio, so there is nothing to divide.

Clearing routes into the shared path, so limit --clear-week and
calibrate --clear-week are one operation printing one line."
```

---

### Task 4: Documentation

**Files:**
- Modify: `README.md:66-74` (the estimates caveat), `README.md:299-345` (the Calibration section)
- Modify: `cost_meter/store.py:110-115` (the `update_json_locked` docstring)

**Interfaces:**
- Consumes: nothing. Produces: nothing. Documentation only.

- [ ] **Step 1: Correct the store docstring**

`cost_meter/store.py:113-114` names one writer of the ceilings and there are now two. Replace that sentence:

```python
    Several writers share config.json — the widget stores its position there and
    cost_meter/ceilings.py stores the ceilings, for both calibrate.py and
    limit.py. An unlocked read-modify-write silently drops whichever value the
    other side wrote in between.
```

- [ ] **Step 2: Redirect the recalibration advice**

`README.md:70-74` currently ends *"If you move your work between machines, recalibrate rather than trusting the percentage."* That advice fails for work that is split rather than moved. Replace the sentence with:

```markdown
If you move your work between machines occasionally, recalibrate rather than
trusting the percentage. If it is split between them habitually, recalibration
cannot help — the two numbers the ratio needs describe different scopes, and no
reading of `/usage` reconciles them. Declare the ceiling instead: see
[Declaring a known limit](#declaring-a-known-limit).
```

- [ ] **Step 3: Add the new README subsection**

Append to the Calibration section, after the `--clear` paragraph that ends it around `README.md:345`:

```markdown
### Declaring a known limit

Calibration derives the ceiling from a ratio. If you already know the ceiling —
a plan's weekly cap, say — write it in directly and skip the derivation:

```bash
pixi run limit -- --week 2000
pixi run limit -- --5h 130
```

The same bare `--` applies, and the values are USD-equivalent, on the same
scale as the dollar figures the rows already show.

This is the answer when calibration cannot work rather than merely being
imprecise: with the work split across machines, the spend the ratio divides is
this installation's while the percentage is the whole account's, so no reading
of `/usage` produces a correct ceiling. A declared one changes what the
percentage claims — this installation's share of a known bound, rather than an
estimate of account-wide consumption. It keeps its `~`, and for a reason: with
nothing absorbing the usage this installation cannot see, it understates the
account by however much the other machines contribute.

Unlike `calibrate`, `limit` needs no recorded spend: there is no ratio to take.

There is one ceiling per window however it arrived, so the clear flags are
shared — `pixi run limit -- --clear-week` and
`pixi run calibrate -- --clear-week` are the same operation.
```

- [ ] **Step 4: Check the rendered links and the run**

Run: `pixi run limit -- --help`
Expected: the docstring's usage lines match the commands the README now documents, `--week` before `--5h` in both.

Confirm the `[Declaring a known limit](#declaring-a-known-limit)` anchor matches the new `###` heading exactly.

- [ ] **Step 5: Run the full suite one last time**

Run: `pixi run test`
Expected: PASS.

Run: `pixi run smoke`
Expected: OK.

- [ ] **Step 6: Commit**

```bash
git add README.md cost_meter/store.py
git commit -m "docs: document declaring a known limit

The estimates caveat told you to recalibrate when work moves between machines.
That is right for work that moves and wrong for work that is split: the ratio's
two numbers describe different scopes and no /usage reading reconciles them.
Points at limit instead, and says what the percentage claims once a ceiling is
declared rather than derived."
```

---

## Verification

The spec's six checks, mapped to where this plan performs them:

| Spec check | Where |
| --- | --- |
| 1. `pixi run test` green | Task 1 Step 8, Task 3 Step 5, Task 4 Step 5 |
| 2. `pixi run smoke` green | Task 1 Step 9, Task 3 Step 5, Task 4 Step 5 |
| 3. Declaration preserves `widget_position` | Task 3 Step 1 (`test_the_saved_window_position_survives`) and Step 6 by hand |
| 4. `0`, `nan` and set-plus-clear refused, config untouched | Task 2 Step 1 (`RejectionTest`), Task 3 Step 1 (`test_a_rejected_run_writes_nothing`) |
| 5. `calibrate --clear-week` clears what `limit` wrote, same line | Task 3 Step 1 (`test_clearing_goes_through_the_shared_path`), Step 6 by hand |
| 6. Declaring with no spend in the block succeeds | Task 3 Step 1 (`test_a_declaration_needs_no_spend`) |
