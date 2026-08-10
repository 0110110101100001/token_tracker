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
