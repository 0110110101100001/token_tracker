# token_calculator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An always-on-top desktop widget, bottom-right, showing the USD-equivalent cost of Claude Code work and an estimate of subscription limit consumption, refreshed after every assistant turn.

**Architecture:** A `Stop` hook runs `tally.py`, which incrementally reads new bytes of the Claude Code transcripts, prices each usage record, appends it to `events.jsonl`, and rewrites a small `state.json`. A separate GTK widget watches `state.json` with a file monitor and only renders. The two halves never block each other.

**Tech Stack:** Python 3.14 (system interpreter), stdlib only, plus system PyGObject/GTK 3 for the widget. `unittest` for tests — pytest is not installed and this project deliberately has no dependency install step.

## Global Constraints

- **No dependencies.** Standard library plus the already-installed system PyGObject. No venv, no pip install, no pixi environment. This is a documented departure from the standing pixi rule, agreed during design.
- **Git is local only.** The repository lives at `~/Desktop/token_calculator/` with no remote; nothing is ever pushed. Each task ends with one commit covering its own files, after its tests pass. `data/` and `.superpowers/` are git-ignored.
- **All file content in English**, including the widget's user-facing strings.
- **Code lives in `~/Desktop/token_calculator/`.** Generated runtime data lives in `~/Desktop/token_calculator/data/`, overridable with the `COST_METER_HOME` environment variable so tests never touch real data. This resolves a detail the spec left open.
- **`tally.py` must always exit 0.** It runs on the user's critical path; a failure must cost a number on screen, never the ability to work.
- **Unknown models must never be priced as zero.** They surface in `state.json` as `unknown_models` and render as a warning row.
- **Timestamps are stored as epoch seconds (UTC).** Day and window boundaries are computed in local time.
- Pricing per million tokens: `claude-fable-5` 10/50, `claude-opus-5` 5/25, `claude-opus-4-8` 5/25, `claude-sonnet-5` 3/15. Cache read is 0.1x the input rate, a 5-minute cache write 1.25x, a 1-hour cache write 2x.

## File Structure

```
~/Desktop/token_calculator/
├── README.md                   # usage, install, known limitations
├── smoke.sh                    # runs the whole test suite + widget selftest
├── pricing.json                # model -> {input, output} USD per Mtok
├── run_widget.sh               # launches widget.py with GDK_BACKEND=x11
├── tally.py                    # Stop-hook entry point
├── calibrate.py                # one-off limit calibration
├── widget.py                   # GTK 3 window
├── cost_meter/
│   ├── __init__.py
│   ├── paths.py                # every filesystem path, env-overridable
│   ├── pricing.py              # pure pricing math
│   ├── store.py                # events.jsonl + locking + pruning
│   ├── parser.py               # incremental transcript scan
│   └── summary.py              # events -> state.json
├── data/                       # generated at runtime, git-ignorable
└── tests/
    ├── fixtures/sample_transcript.jsonl
    ├── test_pricing.py
    ├── test_store.py
    ├── test_parser.py
    └── test_summary.py
```

The split follows responsibility, not layering. `pricing.py` is pure arithmetic
with no I/O, which makes the money math trivially testable. `parser.py` owns the
one genuinely tricky algorithm (incremental reads with truncation recovery).
`store.py` owns every write and the lock. `summary.py` turns a list of events
into the numbers on screen. The widget imports none of them except `paths`.

---

### Task 1: Pricing math

**Files:**
- Create: `pricing.json`
- Create: `cost_meter/__init__.py` (empty)
- Create: `cost_meter/pricing.py`
- Test: `tests/test_pricing.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `load_pricing(path) -> dict`, `price_event(pricing, model, input_tokens, output_tokens, cache_write_5m, cache_write_1h, cache_read) -> float`, and `class UnknownModel(Exception)` carrying a `.model` attribute.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pricing.py
import unittest
from cost_meter.pricing import UnknownModel, price_event

PRICING = {
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
}


class TestPriceEvent(unittest.TestCase):
    def test_input_and_output_tokens(self):
        usd = price_event(PRICING, "claude-opus-5", 1_000_000, 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(usd, 30.0)

    def test_cache_read_is_a_tenth_of_input(self):
        usd = price_event(PRICING, "claude-opus-5", 0, 0, 0, 0, 1_000_000)
        self.assertAlmostEqual(usd, 0.5)

    def test_cache_writes_use_their_own_multipliers(self):
        usd = price_event(PRICING, "claude-opus-5", 0, 0, 1_000_000, 1_000_000, 0)
        self.assertAlmostEqual(usd, 5.0 * 1.25 + 5.0 * 2.0)

    def test_rates_are_per_model(self):
        usd = price_event(PRICING, "claude-sonnet-5", 1_000_000, 0, 0, 0, 0)
        self.assertAlmostEqual(usd, 3.0)

    def test_unknown_model_raises_and_names_the_model(self):
        with self.assertRaises(UnknownModel) as ctx:
            price_event(PRICING, "claude-nonexistent-9", 100, 100, 0, 0, 0)
        self.assertEqual(ctx.exception.model, "claude-nonexistent-9")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/Desktop/token_calculator && python3 -m unittest tests.test_pricing -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cost_meter'`

- [ ] **Step 3: Write `pricing.json`**

```json
{
  "claude-fable-5":   {"input": 10.0, "output": 50.0},
  "claude-opus-5":    {"input": 5.0,  "output": 25.0},
  "claude-opus-4-8":  {"input": 5.0,  "output": 25.0},
  "claude-sonnet-5":  {"input": 3.0,  "output": 15.0}
}
```

- [ ] **Step 4: Write the implementation**

Create an empty `cost_meter/__init__.py`, then:

```python
# cost_meter/pricing.py
"""Pure pricing arithmetic. No I/O beyond loading the rate table."""

import json

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
TOKENS_PER_UNIT = 1_000_000


class UnknownModel(Exception):
    """Raised when a model has no entry in the pricing table."""

    def __init__(self, model):
        super().__init__(f"no pricing entry for model {model!r}")
        self.model = model


def load_pricing(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def price_event(
    pricing,
    model,
    input_tokens,
    output_tokens,
    cache_write_5m,
    cache_write_1h,
    cache_read,
):
    """Return the USD cost of one assistant message."""
    rates = pricing.get(model)
    if rates is None:
        raise UnknownModel(model)
    per_input = rates["input"] / TOKENS_PER_UNIT
    per_output = rates["output"] / TOKENS_PER_UNIT
    return (
        input_tokens * per_input
        + output_tokens * per_output
        + cache_write_5m * per_input * CACHE_WRITE_5M_MULTIPLIER
        + cache_write_1h * per_input * CACHE_WRITE_1H_MULTIPLIER
        + cache_read * per_input * CACHE_READ_MULTIPLIER
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/Desktop/token_calculator && python3 -m unittest tests.test_pricing -v`
Expected: PASS, 5 tests

---

### Task 2: Paths and the event store

**Files:**
- Create: `cost_meter/paths.py`
- Create: `cost_meter/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - From `paths`: `project_root()`, `home()`, `events_path()`, `state_path()`, `offsets_path()`, `config_path()`, `lock_path()`, `log_path()`, `pricing_path()`, `transcripts_root()` — all returning `pathlib.Path`.
  - From `store`: `exclusive_lock(path)` context manager, `append_events(path, events)`, `read_events(path) -> list[list]`, `prune_events(path, cutoff_epoch) -> int`, `write_json_atomic(path, obj)`, `read_json(path, default=None)`.
- Event record layout, relied on by Tasks 3, 4, 5 and 6:
  `[ts_epoch: float, message_id: str, session_id: str, model: str, input: int, output: int, cache_write_5m: int, cache_write_1h: int, cache_read: int]`

The `message_id` lives inside the event record on purpose: it makes
`events.jsonl` the single source of truth for deduplication, so no separate
seen-ids file has to be kept in sync or bounded.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
import json
import os
import tempfile
import unittest
from pathlib import Path

from cost_meter import store


def event(ts, msg_id="m1"):
    return [ts, msg_id, "sess", "claude-opus-5", 1, 2, 3, 4, 5]


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_then_read_roundtrips(self):
        store.append_events(self.path, [event(100.0), event(200.0, "m2")])
        self.assertEqual(store.read_events(self.path), [event(100.0), event(200.0, "m2")])

    def test_append_is_additive(self):
        store.append_events(self.path, [event(100.0)])
        store.append_events(self.path, [event(200.0, "m2")])
        self.assertEqual(len(store.read_events(self.path)), 2)

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(store.read_events(self.path), [])

    def test_read_skips_corrupt_lines(self):
        store.append_events(self.path, [event(100.0)])
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        self.assertEqual(len(store.read_events(self.path)), 1)

    def test_prune_drops_events_older_than_cutoff(self):
        store.append_events(self.path, [event(100.0), event(500.0, "m2")])
        removed = store.prune_events(self.path, cutoff_epoch=300.0)
        self.assertEqual(removed, 1)
        self.assertEqual(store.read_events(self.path), [event(500.0, "m2")])

    def test_write_json_atomic_leaves_no_partial_file(self):
        target = Path(self.tmp.name) / "state.json"
        store.write_json_atomic(target, {"a": 1})
        self.assertEqual(json.loads(target.read_text()), {"a": 1})
        self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_read_json_returns_default_when_absent(self):
        target = Path(self.tmp.name) / "nope.json"
        self.assertEqual(store.read_json(target, default={"x": 0}), {"x": 0})

    def test_exclusive_lock_creates_and_releases(self):
        lock = Path(self.tmp.name) / "t.lock"
        with store.exclusive_lock(lock):
            self.assertTrue(lock.exists())
        with store.exclusive_lock(lock):
            pass


class TestPaths(unittest.TestCase):
    def test_home_honours_env_override(self):
        from cost_meter import paths

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["COST_METER_HOME"] = tmp
            try:
                self.assertEqual(paths.home(), Path(tmp))
                self.assertEqual(paths.state_path(), Path(tmp) / "state.json")
            finally:
                del os.environ["COST_METER_HOME"]


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/Desktop/token_calculator && python3 -m unittest tests.test_store -v`
Expected: FAIL with `ImportError: cannot import name 'store'`

- [ ] **Step 3: Write `cost_meter/paths.py`**

```python
# cost_meter/paths.py
"""Every filesystem location the tool touches, in one place.

Both directories are environment-overridable so tests never read or write the
user's real data.
"""

import os
from pathlib import Path


def project_root():
    return Path(__file__).resolve().parent.parent


def home():
    override = os.environ.get("COST_METER_HOME")
    return Path(override) if override else project_root() / "data"


def events_path():
    return home() / "events.jsonl"


def state_path():
    return home() / "state.json"


def offsets_path():
    return home() / "offsets.json"


def config_path():
    return home() / "config.json"


def lock_path():
    return home() / "tally.lock"


def log_path():
    return home() / "cost-meter.log"


def pricing_path():
    return project_root() / "pricing.json"


def transcripts_root():
    override = os.environ.get("COST_METER_TRANSCRIPTS")
    return Path(override) if override else Path.home() / ".claude" / "projects"
```

- [ ] **Step 4: Write `cost_meter/store.py`**

```python
# cost_meter/store.py
"""Append-only event log, atomic JSON writes, and the cross-process lock."""

import fcntl
import json
import os
from contextlib import contextmanager

PRUNE_DAYS = 8


@contextmanager
def exclusive_lock(path):
    """Serialise concurrent tally runs from parallel Claude Code sessions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def append_events(path, events):
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")


def read_events(path):
    """Return every well-formed event. Corrupt lines are skipped, not fatal."""
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def prune_events(path, cutoff_epoch):
    """Drop events older than the cutoff. Returns how many were removed."""
    events = read_events(path)
    kept = [e for e in events if e[0] >= cutoff_epoch]
    removed = len(events) - len(kept)
    if removed:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for event in kept:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    return removed


def write_json_atomic(path, obj):
    """Write via a temp file and rename, so a reader never sees a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/Desktop/token_calculator && python3 -m unittest tests.test_store -v`
Expected: PASS, 9 tests

---

### Task 3: Incremental transcript parser

**Files:**
- Create: `cost_meter/parser.py`
- Create: `tests/fixtures/sample_transcript.jsonl`
- Test: `tests/test_parser.py`

**Interfaces:**
- Consumes: the event record layout from Task 2.
- Produces: `scan(root, offsets, known_ids) -> (events, offsets)` where `root` is a `Path` to a transcripts directory, `offsets` is `{path_str: {"size": int, "offset": int}}`, `known_ids` is a `set[str]`, and `events` is a list of event records in the Task 2 layout. The returned `offsets` is a fresh dict, not a mutation of the input.

Three behaviours matter and each has a test: a second scan with the returned
offsets must yield nothing; `"model": "<synthetic>"` messages are never priced;
and a file that shrank was rotated, so it is reread from byte zero.

- [ ] **Step 1: Write the fixture**

```jsonl
{"timestamp":"2026-08-10T08:00:00.000Z","sessionId":"s1","message":{"id":"msg_a","model":"claude-opus-5","usage":{"input_tokens":10,"output_tokens":20,"cache_read_input_tokens":30,"cache_creation":{"ephemeral_5m_input_tokens":40,"ephemeral_1h_input_tokens":50}}}}
{"timestamp":"2026-08-10T08:00:01.000Z","sessionId":"s1","message":{"id":"msg_a","model":"claude-opus-5","usage":{"input_tokens":10,"output_tokens":20,"cache_read_input_tokens":30,"cache_creation":{"ephemeral_5m_input_tokens":40,"ephemeral_1h_input_tokens":50}}}}
{"timestamp":"2026-08-10T08:00:02.000Z","sessionId":"s1","message":{"id":"msg_b","model":"<synthetic>","usage":{"input_tokens":1,"output_tokens":1}}}
{"type":"user","timestamp":"2026-08-10T08:00:03.000Z","message":{"role":"user","content":"hi"}}
{"timestamp":"2026-08-10T08:00:04.000Z","sessionId":"s2","message":{"id":"msg_c","model":"claude-sonnet-5","usage":{"input_tokens":7,"output_tokens":8}}}
not valid json at all
```

Line 1 is a normal message. Line 2 repeats `msg_a`, the streamed-chunk
duplicate. Line 3 is synthetic. Line 4 is a user turn with no usage. Line 5 is a
different session and a model with no `cache_creation` key. Line 6 is corrupt.
Only lines 1 and 5 should become events.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_parser.py
import shutil
import tempfile
import unittest
from pathlib import Path

from cost_meter import parser

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transcript.jsonl"


class TestScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.transcript = self.root / "proj" / "a.jsonl"
        self.transcript.parent.mkdir(parents=True)
        shutil.copy(FIXTURE, self.transcript)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extracts_only_priceable_messages(self):
        events, _ = parser.scan(self.root, {}, set())
        self.assertEqual([e[1] for e in events], ["msg_a", "msg_c"])

    def test_maps_all_token_fields(self):
        events, _ = parser.scan(self.root, {}, set())
        first = events[0]
        self.assertEqual(first[2], "s1")
        self.assertEqual(first[3], "claude-opus-5")
        self.assertEqual(first[4:], [10, 20, 40, 50, 30])

    def test_missing_cache_creation_defaults_to_zero(self):
        events, _ = parser.scan(self.root, {}, set())
        self.assertEqual(events[1][4:], [7, 8, 0, 0, 0])

    def test_timestamp_becomes_epoch_seconds(self):
        events, _ = parser.scan(self.root, {}, set())
        self.assertAlmostEqual(events[0][0], 1786348800.0, places=0)

    def test_second_scan_with_returned_offsets_yields_nothing(self):
        events, offsets = parser.scan(self.root, {}, set())
        known = {e[1] for e in events}
        again, _ = parser.scan(self.root, offsets, known)
        self.assertEqual(again, [])

    def test_appended_lines_are_picked_up(self):
        _, offsets = parser.scan(self.root, {}, set())
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(
                '{"timestamp":"2026-08-10T09:00:00.000Z","sessionId":"s1",'
                '"message":{"id":"msg_d","model":"claude-opus-5",'
                '"usage":{"input_tokens":1,"output_tokens":1}}}\n'
            )
        events, _ = parser.scan(self.root, offsets, {"msg_a", "msg_c"})
        self.assertEqual([e[1] for e in events], ["msg_d"])

    def test_truncated_file_is_reread_from_zero(self):
        _, offsets = parser.scan(self.root, {}, set())
        self.transcript.write_text(
            '{"timestamp":"2026-08-10T10:00:00.000Z","sessionId":"s9",'
            '"message":{"id":"msg_z","model":"claude-opus-5",'
            '"usage":{"input_tokens":2,"output_tokens":2}}}\n',
            encoding="utf-8",
        )
        events, _ = parser.scan(self.root, offsets, set())
        self.assertEqual([e[1] for e in events], ["msg_z"])

    def test_known_ids_are_not_re_emitted(self):
        events, _ = parser.scan(self.root, {}, {"msg_a"})
        self.assertEqual([e[1] for e in events], ["msg_c"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ~/Desktop/token_calculator && python3 -m unittest tests.test_parser -v`
Expected: FAIL with `ImportError: cannot import name 'parser'`

- [ ] **Step 4: Write the implementation**

```python
# cost_meter/parser.py
"""Incremental reader for Claude Code transcripts.

Only bytes appended since the previous run are read, which keeps a scan in the
kilobytes even though the transcript tree is well over a hundred megabytes.
"""

import json
from datetime import datetime

SYNTHETIC_MODEL = "<synthetic>"


def _epoch(timestamp):
    if not timestamp:
        return 0.0
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _event_from(entry):
    """Return an event record, or None if this line is not a priceable message."""
    message = entry.get("message") or {}
    usage = message.get("usage")
    if not usage:
        return None
    model = message.get("model")
    if not model or model == SYNTHETIC_MODEL:
        return None
    message_id = message.get("id")
    if not message_id:
        return None
    cache_creation = usage.get("cache_creation") or {}
    return [
        _epoch(entry.get("timestamp")),
        message_id,
        entry.get("sessionId") or "",
        model,
        usage.get("input_tokens") or 0,
        usage.get("output_tokens") or 0,
        cache_creation.get("ephemeral_5m_input_tokens") or 0,
        cache_creation.get("ephemeral_1h_input_tokens") or 0,
        usage.get("cache_read_input_tokens") or 0,
    ]


def scan(root, offsets, known_ids):
    """Read new transcript bytes under root.

    Returns (events, new_offsets). new_offsets is a fresh dict; the caller's
    copy is never mutated. Ids in known_ids are skipped, as are duplicates
    within this scan.
    """
    events = []
    new_offsets = dict(offsets)
    seen = set(known_ids)

    for path in sorted(root.rglob("*.jsonl")):
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue

        previous = offsets.get(key) or {}
        start = previous.get("offset", 0)
        if size < start:
            start = 0  # file was truncated or rotated

        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                fh.seek(start)
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = _event_from(entry)
                    if event is None or event[1] in seen:
                        continue
                    seen.add(event[1])
                    events.append(event)
                end = fh.tell()
        except OSError:
            continue

        new_offsets[key] = {"size": size, "offset": end}

    return events, new_offsets
```

**Amended during execution.** The text-mode implementation above shipped and was
then replaced: review found that `for line in fh` consumes an unterminated final
line, `json.loads` rejects it, and `fh.tell()` then records EOF *past* it — so a
message written while the hook was reading is dropped permanently and silently.
The shipped version reads in binary and stops at the last complete newline
(`cut = chunk.rfind(b"\n") + 1`, `end = start + cut`), which also makes `offset`
a true byte count in the same unit as `size`. See `cost_meter/parser.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/Desktop/token_calculator && python3 -m unittest tests.test_parser -v`
Expected: PASS, 8 tests

---

### Task 4: Summary builder

**Files:**
- Create: `cost_meter/summary.py`
- Test: `tests/test_summary.py`

**Interfaces:**
- Consumes: `price_event` and `UnknownModel` from Task 1; the event layout from Task 2.
- Produces: `build_state(events, pricing, session_id, new_ids, now_epoch, calibration) -> dict` and the constants `WINDOW_5H_SECONDS = 5 * 3600`, `WINDOW_7D_SECONDS = 7 * 86400`.
  - `calibration` is `{"ceiling_5h_usd": float | None, "ceiling_7d_usd": float | None}`.
  - The returned dict matches the spec's `state.json` shape.

`last_turn_usd` sums only events that are both newly seen this run and belong to
the calling session. Without the session filter, a turn finishing in another
Claude Code window would inflate this row.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_summary.py
import unittest
from datetime import datetime

from cost_meter.summary import build_state

PRICING = {"claude-opus-5": {"input": 5.0, "output": 25.0}}
NO_CAL = {"ceiling_5h_usd": None, "ceiling_7d_usd": None}


def event(ts, msg_id, session="s1", model="claude-opus-5", out=1_000_000):
    return [ts, msg_id, session, model, 0, out, 0, 0, 0]


class TestBuildState(unittest.TestCase):
    def setUp(self):
        # Midday local time, so "today" never straddles a midnight boundary.
        self.now = datetime.now().replace(hour=12, minute=0, second=0,
                                          microsecond=0).timestamp()

    def test_session_total_covers_only_that_session(self):
        events = [event(self.now - 60, "a"), event(self.now - 60, "b", session="s2")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertAlmostEqual(state["session"]["usd"], 25.0)

    def test_last_turn_covers_only_new_events_in_this_session(self):
        events = [
            event(self.now - 60, "old"),
            event(self.now - 5, "new"),
            event(self.now - 5, "other", session="s2"),
        ]
        state = build_state(events, PRICING, "s1", {"new", "other"}, self.now, NO_CAL)
        self.assertAlmostEqual(state["last_turn_usd"], 25.0)

    def test_five_hour_window_excludes_older_events(self):
        events = [event(self.now - 6 * 3600, "old"), event(self.now - 60, "new")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertAlmostEqual(state["window_5h"]["usd"], 25.0)

    def test_seven_day_window_excludes_older_events(self):
        events = [event(self.now - 8 * 86400, "old"), event(self.now - 60, "new")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertAlmostEqual(state["window_7d"]["usd"], 25.0)

    def test_pct_is_none_without_calibration(self):
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, NO_CAL)
        self.assertIsNone(state["window_5h"]["pct"])

    def test_pct_uses_the_calibrated_ceiling(self):
        cal = {"ceiling_5h_usd": 50.0, "ceiling_7d_usd": None}
        state = build_state([event(self.now, "a")], PRICING, "s1", set(),
                            self.now, cal)
        self.assertEqual(state["window_5h"]["pct"], 50)

    def test_unknown_model_is_reported_and_not_priced_as_zero(self):
        events = [event(self.now, "a", model="claude-nonexistent-9")]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertEqual(state["unknown_models"], ["claude-nonexistent-9"])
        self.assertAlmostEqual(state["session"]["usd"], 0.0)

    def test_today_spans_sessions_but_not_yesterday(self):
        events = [
            event(self.now - 30 * 3600, "yesterday"),
            event(self.now - 60, "a"),
            event(self.now - 60, "b", session="s2"),
        ]
        state = build_state(events, PRICING, "s1", set(), self.now, NO_CAL)
        self.assertAlmostEqual(state["today_usd"], 50.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/Desktop/token_calculator && python3 -m unittest tests.test_summary -v`
Expected: FAIL with `ImportError: cannot import name 'summary'`

- [ ] **Step 3: Write the implementation**

```python
# cost_meter/summary.py
"""Turn a list of priced events into the handful of numbers on screen."""

from datetime import datetime, timezone

from .pricing import UnknownModel, price_event

WINDOW_5H_SECONDS = 5 * 3600
WINDOW_7D_SECONDS = 7 * 86400


def _local_midnight(now_epoch):
    local = datetime.fromtimestamp(now_epoch)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _pct(usd, ceiling):
    if not ceiling:
        return None
    return round(100.0 * usd / ceiling)


def build_state(events, pricing, session_id, new_ids, now_epoch, calibration):
    midnight = _local_midnight(now_epoch)
    unknown = set()

    session_usd = today_usd = usd_5h = usd_7d = last_turn_usd = 0.0

    for event in events:
        ts, message_id, event_session, model = event[0], event[1], event[2], event[3]
        try:
            usd = price_event(pricing, model, *event[4:])
        except UnknownModel:
            unknown.add(model)
            continue

        if event_session == session_id:
            session_usd += usd
            if message_id in new_ids:
                last_turn_usd += usd
        if ts >= midnight:
            today_usd += usd
        if ts >= now_epoch - WINDOW_5H_SECONDS:
            usd_5h += usd
        if ts >= now_epoch - WINDOW_7D_SECONDS:
            usd_7d += usd

    return {
        "updated_at": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(),
        "last_turn_usd": round(last_turn_usd, 4),
        "session": {"id": session_id, "usd": round(session_usd, 4)},
        "today_usd": round(today_usd, 4),
        "window_5h": {
            "usd": round(usd_5h, 4),
            "pct": _pct(usd_5h, calibration.get("ceiling_5h_usd")),
        },
        "window_7d": {
            "usd": round(usd_7d, 4),
            "pct": _pct(usd_7d, calibration.get("ceiling_7d_usd")),
        },
        "unknown_models": sorted(unknown),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/Desktop/token_calculator && python3 -m unittest tests.test_summary -v`
Expected: PASS, 8 tests

---

### Task 5: The `tally.py` entry point and hook registration

**Files:**
- Create: `tally.py`
- Modify: `~/.claude/settings.json` (add a `Stop` hook next to the existing `PostToolUse` entry)

**Interfaces:**
- Consumes: everything produced by Tasks 1 through 4.
- Produces: an executable script. Reads the hook payload as JSON on stdin, writes `data/state.json`, and always exits 0.

- [ ] **Step 1: Write the implementation**

```python
#!/usr/bin/env python3
"""Stop-hook entry point: refresh the cost meter after an assistant turn.

This runs on the user's critical path, so every failure is swallowed and
logged. A broken tally costs a number on screen, never the ability to work.
"""

import json
import sys
import time
import traceback

from cost_meter import paths, store
from cost_meter.parser import scan
from cost_meter.pricing import load_pricing
from cost_meter.store import PRUNE_DAYS
from cost_meter.summary import build_state


def _log(message):
    try:
        path = paths.log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _session_id_from_stdin():
    try:
        if sys.stdin.isatty():
            return ""
        payload = json.load(sys.stdin)
        return payload.get("session_id") or ""
    except (json.JSONDecodeError, ValueError, OSError):
        return ""


def refresh(session_id, now=None):
    """Do one incremental pass. Returns the state dict that was written."""
    now = time.time() if now is None else now

    events_path = paths.events_path()
    existing = store.read_events(events_path)
    known_ids = {e[1] for e in existing}
    offsets = store.read_json(paths.offsets_path(), default={}) or {}

    fresh, new_offsets = scan(paths.transcripts_root(), offsets, known_ids)
    store.append_events(events_path, fresh)
    store.write_json_atomic(paths.offsets_path(), new_offsets)

    store.prune_events(events_path, now - PRUNE_DAYS * 86400)

    pricing = load_pricing(paths.pricing_path())
    calibration = store.read_json(paths.config_path(), default={}) or {}
    state = build_state(
        store.read_events(events_path),
        pricing,
        session_id,
        {e[1] for e in fresh},
        now,
        calibration,
    )
    store.write_json_atomic(paths.state_path(), state)
    return state


def main():
    try:
        session_id = _session_id_from_stdin()
        with store.exclusive_lock(paths.lock_path()):
            refresh(session_id)
    except Exception:
        _log("tally failed:\n" + traceback.format_exc())
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Make it executable and run it by hand**

Run:
```bash
cd ~/Desktop/token_calculator
chmod +x tally.py
echo '{"session_id":"manual-test"}' | ./tally.py
echo "exit=$?"
cat data/state.json
```
Expected: `exit=0`, and `data/state.json` contains real non-zero `today_usd` and `window_5h.usd` values computed from the actual transcripts. `unknown_models` should be `[]`; if it is not, add the named model to `pricing.json`.

- [ ] **Step 3: Verify the second run is fast and adds nothing new**

Run:
```bash
cd ~/Desktop/token_calculator
time ./tally.py < /dev/null
```
Expected: well under a second, because only new bytes are read.

- [ ] **Step 4: Register the Stop hook**

Add to the `hooks` object in `~/.claude/settings.json`, alongside the existing `PostToolUse` array:

```json
"Stop": [
  {
    "hooks": [
      {
        "type": "command",
        "command": "/home/martin/Desktop/token_calculator/tally.py",
        "timeout": 20
      }
    ]
  }
]
```

- [ ] **Step 5: Verify the hook fires**

Send any prompt in a new Claude Code session, then run:
```bash
stat -c '%y' ~/Desktop/token_calculator/data/state.json
```
Expected: a modification time from seconds ago.

---

### Task 6: Calibration

**Files:**
- Create: `calibrate.py`

**Interfaces:**
- Consumes: `paths`, `store`, and `refresh` from Task 5.
- Produces: an executable script writing `{"ceiling_5h_usd": float, "ceiling_7d_usd": float}` to `data/config.json`, preserving whichever key is not being set.

- [ ] **Step 1: Write the implementation**

```python
#!/usr/bin/env python3
"""Calibrate the subscription-limit estimate against Claude Code's /usage.

Limit state is not stored locally, so it cannot be read — only estimated.
Consumption is measured in USD-equivalent rather than tokens, because Opus
draws harder on the limit than Sonnet and pricing weights the models for free.

Usage:
    ./calibrate.py --5h 62      # /usage reported 62% for the 5-hour window
    ./calibrate.py --week 31    # /usage reported 31% for the week
"""

import argparse
import sys

from cost_meter import paths, store
from tally import refresh


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--5h", dest="five_hour", type=float,
                        help="percentage /usage reports for the 5-hour window")
    parser.add_argument("--week", dest="week", type=float,
                        help="percentage /usage reports for the week")
    args = parser.parse_args()

    if args.five_hour is None and args.week is None:
        parser.error("give at least one of --5h or --week")
    for value in (args.five_hour, args.week):
        if value is not None and not 1.0 <= value <= 100.0:
            parser.error("percentages must be between 1 and 100")

    with store.exclusive_lock(paths.lock_path()):
        state = refresh(session_id="")

    config = store.read_json(paths.config_path(), default={}) or {}

    if args.five_hour is not None:
        usd = state["window_5h"]["usd"]
        if usd <= 0:
            print("no spend recorded in the last 5 hours; nothing to calibrate "
                  "against", file=sys.stderr)
            return 1
        config["ceiling_5h_usd"] = usd / (args.five_hour / 100.0)
        print(f"5h window: ${usd:.2f} = {args.five_hour:g}% "
              f"-> ceiling ${config['ceiling_5h_usd']:.2f}")

    if args.week is not None:
        usd = state["window_7d"]["usd"]
        if usd <= 0:
            print("no spend recorded in the last 7 days; nothing to calibrate "
                  "against", file=sys.stderr)
            return 1
        config["ceiling_7d_usd"] = usd / (args.week / 100.0)
        print(f"week: ${usd:.2f} = {args.week:g}% "
              f"-> ceiling ${config['ceiling_7d_usd']:.2f}")

    store.write_json_atomic(paths.config_path(), config)

    with store.exclusive_lock(paths.lock_path()):
        refresh(session_id="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

The final `refresh` call is deliberate: it rewrites `state.json` with the new
ceiling applied, so the widget switches from dollars to percentages the moment
calibration finishes rather than after the next prompt.

- [ ] **Step 2: Run it and verify the ceiling is written**

Run:
```bash
cd ~/Desktop/token_calculator
chmod +x calibrate.py
./calibrate.py --5h 50
cat data/config.json
python3 -c "import json;s=json.load(open('data/state.json'));print(s['window_5h'])"
```
Expected: `ceiling_5h_usd` is exactly twice `window_5h.usd`, and the reprinted `window_5h` now has `"pct": 50`.

- [ ] **Step 3: Verify the guard rails**

Run:
```bash
cd ~/Desktop/token_calculator
./calibrate.py --5h 0; echo "exit=$?"
./calibrate.py; echo "exit=$?"
```
Expected: both exit non-zero with an argparse error, and `data/config.json` is unchanged.

---

### Task 7: The widget

**Files:**
- Create: `widget.py`
- Create: `run_widget.sh`

**Interfaces:**
- Consumes: `paths.state_path()` and `paths.config_path()` from Task 2, and the `state.json` shape from Task 4. It imports nothing else from `cost_meter`.
- Produces: a GTK 3 window, plus a `--selftest` flag that renders one frame to a PNG and exits 0 without mapping a window.

- [ ] **Step 1: Write the launcher**

```bash
#!/usr/bin/env bash
# Run the widget as an X11 client under XWayland.
#
# A Wayland client may not position itself or raise itself above others, which
# is exactly what this widget needs. Under XWayland both work normally.
set -euo pipefail
cd "$(dirname "$0")"
export GDK_BACKEND=x11
exec python3 widget.py "$@"
```

- [ ] **Step 2: Write the widget**

```python
#!/usr/bin/env python3
"""Always-on-top cost meter, anchored bottom-right.

Reads data/state.json and nothing else. Run it through run_widget.sh, which
sets GDK_BACKEND=x11 so the window can place and raise itself.
"""

import argparse
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from cost_meter import paths, store  # noqa: E402

MARGIN = 24
WIDTH = 240
AMBER_AT = 60
RED_AT = 85

CSS = b"""
window { background-color: #1e1e22; }
label { color: #d8d8dc; font-family: monospace; font-size: 11px; }
label.value { font-weight: bold; }
label.muted { color: #8a8a92; }
label.green { color: #78d178; }
label.amber { color: #e3b341; }
label.red { color: #f06a5a; }
label.warn { color: #f06a5a; font-size: 10px; }
"""


def _fmt_usd(value):
    return "—" if value is None else f"${value:,.2f}"


def _row(grid, index, caption):
    left = Gtk.Label(label=caption, xalign=0.0)
    right = Gtk.Label(label="—", xalign=1.0)
    right.get_style_context().add_class("value")
    right.set_hexpand(True)
    grid.attach(left, 0, index, 1, 1)
    grid.attach(right, 1, index, 1, 1)
    return right


class CostMeter(Gtk.Window):
    def __init__(self):
        super().__init__(title="Claude cost meter")
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_resizable(False)
        self.set_default_size(WIDTH, -1)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self.on_click)
        self.connect("destroy", Gtk.main_quit)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        grid = Gtk.Grid(row_spacing=3, column_spacing=12)
        grid.set_border_width(10)
        self.add(grid)

        self.last_turn = _row(grid, 0, "last turn")
        self.session = _row(grid, 1, "session")
        self.today = _row(grid, 2, "today")
        grid.attach(Gtk.Separator(), 0, 3, 2, 1)
        self.window_5h = _row(grid, 4, "5h window")
        self.window_7d = _row(grid, 5, "week")

        self.warning = Gtk.Label(label="", xalign=0.0)
        self.warning.get_style_context().add_class("warn")
        self.warning.set_no_show_all(True)
        grid.attach(self.warning, 0, 6, 2, 1)

        self.place()
        self.watch()
        self.refresh()

    def place(self):
        config = store.read_json(paths.config_path(), default={}) or {}
        position = config.get("widget_position")
        if position:
            self.move(int(position[0]), int(position[1]))
            return
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()
        width, height = self.get_preferred_size()[1].width or WIDTH, 140
        self.move(area.x + area.width - width - MARGIN,
                  area.y + area.height - height - MARGIN)

    def remember_position(self):
        config = store.read_json(paths.config_path(), default={}) or {}
        config["widget_position"] = list(self.get_position())
        store.write_json_atomic(paths.config_path(), config)

    def watch(self):
        path = paths.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.monitor = Gio.File.new_for_path(str(path)).monitor_file(
            Gio.FileMonitorFlags.NONE, None
        )
        self.monitor.connect("changed", lambda *_: self.refresh())

    def refresh(self):
        state = store.read_json(paths.state_path(), default=None)
        if not state:
            return True

        delta = state.get("last_turn_usd") or 0.0
        self.last_turn.set_text(f"+{_fmt_usd(delta)}" if delta else "—")
        self.session.set_text(_fmt_usd((state.get("session") or {}).get("usd")))
        self.today.set_text(_fmt_usd(state.get("today_usd")))
        self.set_window_row(self.window_5h, state.get("window_5h") or {})
        self.set_window_row(self.window_7d, state.get("window_7d") or {})

        unknown = state.get("unknown_models") or []
        if unknown:
            self.warning.set_text("? " + ", ".join(unknown))
            self.warning.show()
        else:
            self.warning.hide()
        return True

    def set_window_row(self, label, window):
        context = label.get_style_context()
        for name in ("green", "amber", "red", "muted"):
            context.remove_class(name)

        pct = window.get("pct")
        if pct is None:
            # Not calibrated yet: show dollars rather than an invented number.
            label.set_text(f"{_fmt_usd(window.get('usd'))}")
            context.add_class("muted")
            return
        label.set_text(f"~{pct} % est.")
        context.add_class("red" if pct >= RED_AT else
                          "amber" if pct >= AMBER_AT else "green")

    def on_click(self, _widget, event):
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root),
                                 int(event.y_root), event.time)
            GLib.timeout_add(500, self._store_position_once)
            return True
        if event.button == 3:
            self.show_menu(event)
            return True
        return False

    def _store_position_once(self):
        self.remember_position()
        return False

    def show_menu(self, event):
        menu = Gtk.Menu()
        for caption, handler in (
            ("Refresh now", lambda *_: self.refresh()),
            ("Reset position", lambda *_: self.reset_position()),
            ("Quit", lambda *_: Gtk.main_quit()),
        ):
            item = Gtk.MenuItem(label=caption)
            item.connect("activate", handler)
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)

    def reset_position(self):
        config = store.read_json(paths.config_path(), default={}) or {}
        config.pop("widget_position", None)
        store.write_json_atomic(paths.config_path(), config)
        self.place()


def selftest(output):
    """Render one frame off-screen. Verifies GTK starts and the layout builds."""
    window = CostMeter()
    window.realize()
    allocation = window.get_allocation()
    surface = window.get_window()
    pixbuf = Gdk.pixbuf_get_from_window(
        surface, 0, 0, max(allocation.width, WIDTH), max(allocation.height, 100)
    )
    pixbuf.savev(output, "png", [], [])
    print(f"selftest wrote {output}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", metavar="PNG",
                        help="render one frame to PNG and exit")
    args = parser.parse_args()

    if args.selftest:
        return selftest(args.selftest)

    CostMeter().show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run the selftest**

Run:
```bash
cd ~/Desktop/token_calculator
chmod +x run_widget.sh widget.py
GDK_BACKEND=x11 python3 widget.py --selftest /tmp/cost-meter.png
echo "exit=$?"
```
Expected: exit 0 and `/tmp/cost-meter.png` exists.

- [ ] **Step 4: Run it for real**

Run: `~/Desktop/token_calculator/run_widget.sh &`
Expected: a small undecorated panel appears in the bottom-right corner, above other windows, showing the values from `data/state.json`. Send a prompt in Claude Code and confirm the `last turn` row updates within a second or two without touching the widget.

- [ ] **Step 5: Verify drag and persistence**

Drag the widget somewhere else with the left mouse button, quit it from the right-click menu, and relaunch it.
Expected: it reappears where you dropped it. `data/config.json` contains a `widget_position` entry.

---

### Task 8: Smoke test, autostart, and README

**Files:**
- Create: `smoke.sh`
- Create: `README.md`
- Create: `~/.config/autostart/claude-cost-meter.desktop`

**Interfaces:**
- Consumes: everything above.
- Produces: `./smoke.sh` exiting 0 on a healthy tree.

- [ ] **Step 1: Write the smoke test**

```bash
#!/usr/bin/env bash
# Full check: unit tests against throwaway data, then a GTK render.
set -euo pipefail
cd "$(dirname "$0")"

echo "== unit tests =="
COST_METER_HOME="$(mktemp -d)" python3 -m unittest discover -s tests -v

echo
echo "== widget selftest =="
png="$(mktemp --suffix=.png)"
GDK_BACKEND=x11 python3 widget.py --selftest "$png"
test -s "$png"
rm -f "$png"

echo
echo "smoke OK"
```

The `COST_METER_HOME` override is what keeps the suite from reading or writing
the user's real `data/` directory.

- [ ] **Step 2: Run it**

Run:
```bash
cd ~/Desktop/token_calculator
chmod +x smoke.sh
./smoke.sh
```
Expected: 30 tests pass, the selftest writes a PNG, and the script prints `smoke OK`.

- [ ] **Step 3: Add the autostart entry**

Create `~/.config/autostart/claude-cost-meter.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=Claude cost meter
Exec=/home/martin/Desktop/token_calculator/run_widget.sh
X-GNOME-Autostart-enabled=true
NoDisplay=true
```

- [ ] **Step 4: Write the README**

`README.md` must cover, in English:

- What it does and the bottom-right widget layout.
- Install: register the `Stop` hook in `~/.claude/settings.json` (show the exact JSON from Task 5), then `./run_widget.sh`.
- Calibration: run `/usage` in Claude Code, then `./calibrate.py --5h <pct>` and `./calibrate.py --week <pct>`. Explain that before calibration the two bottom rows show dollars, not percentages, and why.
- Editing `pricing.json` when rates change, and that an unpriced model shows as a `?` row rather than being counted as zero.
- Known limitations, stated plainly:
  - USD is an API-equivalent, not an invoice — the account is on a subscription.
  - Fast mode is not recorded in the transcripts, so a fast-mode turn would be understated by half. Not currently in use.
  - The weekly row is a rolling 7 days, while the real weekly limit resets on a fixed schedule, so it reads slightly pessimistic.
  - `/usage` remains the authoritative source for limits.
- Layout of `data/`: `events.jsonl`, `state.json`, `offsets.json`, `config.json`, `cost-meter.log`, and that deleting `data/` is a safe full reset.

- [ ] **Step 5: Verify a cold start from scratch**

Run:
```bash
cd ~/Desktop/token_calculator
rm -rf data
./tally.py < /dev/null
./smoke.sh
```
Expected: `data/` is rebuilt from the transcripts, `state.json` shows plausible totals, and the smoke test passes.

---

## Self-Review

**Spec coverage.** Every section of the design maps to a task: incremental
parsing with truncation recovery, `<synthetic>` filtering, and `message.id`
deduplication to Task 3; the four-rate cache pricing and the visible-failure
rule for unknown models to Tasks 1 and 4; `flock` and the always-exit-0 policy
to Task 5; USD-equivalent calibration with a dollars fallback to Tasks 6 and 4;
XWayland positioning, drag persistence, `Gio.FileMonitor` and `--selftest` to
Task 7; the three smoke checks and the documented limitations to Task 8.

**Placeholders.** None. Every code step carries the code, and the only prose
step is the README content list, which enumerates each required section.

**Type consistency.** The nine-field event layout is defined once in Task 2 and
consumed unchanged by Tasks 3, 4 and 5. `refresh(session_id, now=None)` is
defined in Task 5 and called from Task 6. `store.read_json(path, default=...)`
and `store.write_json_atomic` are defined in Task 2 and used by Tasks 5, 6 and
7. `build_state`'s `calibration` keys `ceiling_5h_usd` and `ceiling_7d_usd` are
the same keys `calibrate.py` writes.

**One deviation to note.** Task 7's widget imports `cost_meter.store` for its
JSON helpers, so it is not literally dependency-free from the engine as the spec
sketched. Duplicating atomic-write logic to avoid a two-function import would be
worse; the widget still never parses events or prices anything.
