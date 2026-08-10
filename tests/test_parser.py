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

    def test_input_offsets_are_not_mutated(self):
        offsets = {}
        parser.scan(self.root, offsets, set())
        self.assertEqual(offsets, {})


if __name__ == "__main__":
    unittest.main()
