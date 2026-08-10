# tests/test_store.py
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cost_meter import store

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def event(ts, msg_id="m1"):
    return [ts, msg_id, "sess", "claude-opus-5", 1, 2, 3, 4, 5]


def _spawn_lock_holder(lock_path, hold_seconds):
    """Launch a real subprocess that acquires store.exclusive_lock on
    lock_path and holds it for hold_seconds before releasing.

    A thread would not exercise this faithfully: flock is scoped to an open
    file description, so a second open() of the same path genuinely
    contends even within one process, but a thread sharing the same handle
    as the holder would not. A subprocess gives an independent open file
    description, matching how two separate `tally.py` invocations behave.

    Blocks until the subprocess confirms it holds the lock, so callers never
    race the holder's startup.
    """
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        "from pathlib import Path\n"
        "from cost_meter import store\n"
        f"lock = Path({str(lock_path)!r})\n"
        "with store.exclusive_lock(lock, timeout=30):\n"
        "    print('locked', flush=True)\n"
        f"    time.sleep({hold_seconds!r})\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline()
    if line.strip() != "locked":
        proc.wait(timeout=5)
        raise RuntimeError(f"lock holder subprocess failed: {line!r} {proc.stderr.read()!r}")
    return proc


def _wait_for_holder(proc):
    """Wait for the holder subprocess to exit and close its pipes, so tests
    don't leak file descriptors (and the resulting ResourceWarnings)."""
    proc.wait(timeout=5)
    proc.stdout.close()
    proc.stderr.close()


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

    def test_lock_timeout_when_already_held(self):
        lock = Path(self.tmp.name) / "held.lock"
        holder = _spawn_lock_holder(lock, hold_seconds=2.0)
        try:
            start = time.monotonic()
            with self.assertRaises(store.LockTimeout):
                with store.exclusive_lock(lock, timeout=0.2, poll=0.02):
                    pass  # pragma: no cover - must never be entered
            elapsed = time.monotonic() - start
            # Bounded acquisition: raises close to the requested timeout,
            # not instantly and not after the holder's full 2s hold.
            self.assertGreaterEqual(elapsed, 0.2)
            self.assertLess(elapsed, 1.5)
        finally:
            _wait_for_holder(holder)

    def test_lock_is_released_after_a_timeout(self):
        lock = Path(self.tmp.name) / "held2.lock"
        holder = _spawn_lock_holder(lock, hold_seconds=0.5)
        with self.assertRaises(store.LockTimeout):
            with store.exclusive_lock(lock, timeout=0.1, poll=0.02):
                pass  # pragma: no cover - must never be entered
        _wait_for_holder(holder)  # holder releases the lock on exit

        # A failed, timed-out acquisition must not leak a lock or a file
        # handle: once the original holder is gone, a fresh acquisition
        # succeeds promptly.
        entered = []
        with store.exclusive_lock(lock, timeout=2.0):
            entered.append(True)
        self.assertEqual(entered, [True])

    def test_second_acquirer_genuinely_waits_for_the_holder(self):
        lock = Path(self.tmp.name) / "held3.lock"
        holder = _spawn_lock_holder(lock, hold_seconds=0.5)
        start = time.monotonic()
        with store.exclusive_lock(lock, timeout=5.0, poll=0.02):
            elapsed = time.monotonic() - start
        _wait_for_holder(holder)
        # Must have actually blocked until the holder's sleep finished, not
        # proceeded immediately as if the lock were uncontended.
        self.assertGreaterEqual(elapsed, 0.4)


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
