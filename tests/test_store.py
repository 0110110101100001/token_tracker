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


def _spawn_config_writer(config_path, lock_path, hold_seconds):
    """A calibrate.py-shaped competitor: hold the lock, then write a ceiling
    into config.json just before releasing it.

    Writing late in the hold is the point — it is the value an unlocked read
    performed before the lock was acquired would silently destroy.
    """
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        "from pathlib import Path\n"
        "from cost_meter import store\n"
        f"lock = Path({str(lock_path)!r})\n"
        f"config = Path({str(config_path)!r})\n"
        "with store.exclusive_lock(lock, timeout=30):\n"
        "    print('locked', flush=True)\n"
        f"    time.sleep({hold_seconds!r})\n"
        "    data = store.read_json(config, default={}) or {}\n"
        "    data['ceiling_5h_usd'] = 42.0\n"
        "    store.write_json_atomic(config, data)\n"
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
        raise RuntimeError(f"config writer subprocess failed: {line!r} {proc.stderr.read()!r}")
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


class TestUpdateJsonLocked(unittest.TestCase):
    """config.json has two writers: the widget's position and calibrate.py's
    ceilings. Whichever writes second must not drop the other's key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.json"
        self.lock = Path(self.tmp.name) / "tally.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_unrelated_keys_survive_a_mutation(self):
        store.write_json_atomic(self.config, {"ceiling_5h_usd": 42.0})
        with store.update_json_locked(self.config, self.lock) as config:
            config["widget_position"] = [10, 20]
        self.assertEqual(
            store.read_json(self.config),
            {"ceiling_5h_usd": 42.0, "widget_position": [10, 20]},
        )

    def test_missing_file_starts_from_an_empty_dict(self):
        with store.update_json_locked(self.config, self.lock) as config:
            config["widget_position"] = [1, 2]
        self.assertEqual(store.read_json(self.config), {"widget_position": [1, 2]})

    def test_lock_is_held_across_the_read(self):
        """The read must happen inside the lock, not before it.

        A competing writer holds the lock and only writes its key just before
        releasing. If update_json_locked read the file up front, that key would
        be overwritten by the stale copy; holding the lock across both halves
        means the read observes it.
        """
        store.write_json_atomic(self.config, {})
        holder = _spawn_config_writer(self.config, self.lock, hold_seconds=1.0)
        try:
            start = time.monotonic()
            with store.update_json_locked(self.config, self.lock, timeout=10.0) as config:
                config["widget_position"] = [10, 20]
            elapsed = time.monotonic() - start
        finally:
            _wait_for_holder(holder)

        # Genuinely waited for the holder rather than racing past it.
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertEqual(
            store.read_json(self.config),
            {"ceiling_5h_usd": 42.0, "widget_position": [10, 20]},
        )

    def test_lock_timeout_propagates_and_leaves_the_file_alone(self):
        store.write_json_atomic(self.config, {"ceiling_5h_usd": 42.0})
        holder = _spawn_lock_holder(self.lock, hold_seconds=1.0)
        try:
            with self.assertRaises(store.LockTimeout):
                with store.update_json_locked(self.config, self.lock, timeout=0.2):
                    pass  # pragma: no cover - must never be entered
        finally:
            _wait_for_holder(holder)
        self.assertEqual(store.read_json(self.config), {"ceiling_5h_usd": 42.0})

    def test_an_exception_in_the_body_does_not_write(self):
        store.write_json_atomic(self.config, {"ceiling_5h_usd": 42.0})
        with self.assertRaises(ValueError):
            with store.update_json_locked(self.config, self.lock) as config:
                config["widget_position"] = [10, 20]
                raise ValueError("boom")
        self.assertEqual(store.read_json(self.config), {"ceiling_5h_usd": 42.0})


class TestReplaceWithRetry(unittest.TestCase):
    """The rename at the end of every atomic write.

    Regression test with a real cause: a Stop hook lost a refresh to
    `PermissionError: [WinError 5]` from os.replace. On Windows the call fails
    whenever anyone holds the destination open without FILE_SHARE_DELETE, which
    is what the panel does every time it reads state.json, and what a virus
    scanner does to the temp file. Both clear in milliseconds; the write used to
    give up on the first one, and a lost write is what puts `! stale` on a panel
    whose numbers were being updated the whole time.

    The retry is Windows-only, so the tests drive `os.replace` directly rather
    than racing a real reader -- that would only ever fail on one platform, and
    only sometimes.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / "state.json"
        self.real_replace = os.replace
        self.addCleanup(setattr, os, "replace", self.real_replace)

    def _failing_replace(self, failures):
        """An os.replace that raises PermissionError its first `failures` calls."""
        calls = {"n": 0}

        def replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= failures:
                raise PermissionError(5, "Access is denied")
            return self.real_replace(src, dst)

        return replace, calls

    @unittest.skipUnless(os.name == "nt", "the retry is Windows-only")
    def test_a_transient_denial_is_retried(self):
        replace, calls = self._failing_replace(failures=2)
        os.replace = replace
        store.write_json_atomic(self.target, {"today_usd": 1.5})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(store.read_json(self.target), {"today_usd": 1.5})

    @unittest.skipUnless(os.name == "nt", "the retry is Windows-only")
    def test_a_permanent_denial_still_raises(self):
        # A destination still busy after every attempt is not a race any more,
        # and tally.py's caller logs it rather than pretending the write landed.
        replace, calls = self._failing_replace(failures=99)
        os.replace = replace
        with self.assertRaises(PermissionError):
            store.write_json_atomic(self.target, {"today_usd": 1.5})
        self.assertEqual(calls["n"], 5)

    @unittest.skipIf(os.name == "nt", "POSIX rename cannot fail this way")
    def test_posix_does_not_retry(self):
        replace, calls = self._failing_replace(failures=1)
        os.replace = replace
        with self.assertRaises(PermissionError):
            store.write_json_atomic(self.target, {"today_usd": 1.5})
        self.assertEqual(calls["n"], 1)


class TestTryAcquire(unittest.TestCase):
    """The lock a panel holds for its whole run, checked by the launcher."""

    def setUp(self):
        self.lock = Path(tempfile.mkdtemp()) / "widget.lock"

    def test_an_uncontended_lock_is_granted(self):
        handle = store.try_acquire(self.lock)
        self.assertIsNotNone(handle)
        store.release(handle)

    def test_a_held_lock_is_refused(self):
        # A subprocess rather than a second open() in this process, for the same
        # reason _spawn_lock_holder exists: msvcrt's byte-range lock is
        # re-entrant within one process on some paths, so only a genuinely
        # separate process proves exclusion.
        holder = _spawn_lock_holder(self.lock, hold_seconds=1.0)
        try:
            self.assertIsNone(store.try_acquire(self.lock))
        finally:
            _wait_for_holder(holder)

    def test_releasing_lets_the_next_caller_in(self):
        handle = store.try_acquire(self.lock)
        store.release(handle)
        second = store.try_acquire(self.lock)
        self.assertIsNotNone(second)
        store.release(second)

    def test_the_directory_is_created_if_missing(self):
        nested = self.lock.parent / "data" / "widget.lock"
        handle = store.try_acquire(nested)
        self.assertIsNotNone(handle)
        store.release(handle)
        self.assertTrue(nested.exists())


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
