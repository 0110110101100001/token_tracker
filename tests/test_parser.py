import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from cost_meter import parser

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transcript.jsonl"


def _line(message_id, timestamp, model="claude-opus-5"):
    """Build one newline-terminated assistant transcript line."""
    return (
        '{"timestamp":"%s","sessionId":"s1",'
        '"message":{"id":"%s","model":"%s",'
        '"usage":{"input_tokens":1,"output_tokens":1}}}\n' % (timestamp, message_id, model)
    )


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
        _, offsets = parser.scan(self.root, {}, set())
        before = copy.deepcopy(offsets)
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(_line("msg_d", "2026-08-10T09:00:00.000Z"))
        parser.scan(self.root, offsets, set())
        self.assertEqual(offsets, before)

    def test_offsets_alone_suppress_reemission(self):
        _, offsets = parser.scan(self.root, {}, set())
        again, _ = parser.scan(self.root, offsets, set())
        self.assertEqual(again, [])

    def test_partial_last_line_is_not_consumed(self):
        line = _line("msg_p", "2026-08-10T09:00:00.000Z")
        head, tail = line[:60], line[60:]
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(_line("msg_d", "2026-08-10T08:30:00.000Z"))
            fh.write(head)
        first, offsets = parser.scan(self.root, {}, set())
        self.assertEqual([e[1] for e in first], ["msg_a", "msg_c", "msg_d"])

        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write(tail)
        second, _ = parser.scan(self.root, offsets, set())
        self.assertEqual([e[1] for e in second], ["msg_p"])

    def test_offsets_for_deleted_files_are_dropped(self):
        _, offsets = parser.scan(self.root, {}, set())
        self.assertIn(str(self.transcript), offsets)
        self.transcript.unlink()
        _, pruned = parser.scan(self.root, offsets, set())
        self.assertEqual(pruned, {})

    def test_non_object_json_lines_are_skipped(self):
        with open(self.transcript, "a", encoding="utf-8") as fh:
            fh.write("[1, 2]\n")
            fh.write("123\n")
            fh.write('"a string"\n')
            fh.write('{"timestamp":42,"message":"not a dict"}\n')
            fh.write('{"message":{"id":"x","model":"m","usage":"not a dict"}}\n')
            fh.write(_line("msg_d", "2026-08-10T09:00:00.000Z"))
        events, _ = parser.scan(self.root, {}, set())
        self.assertEqual([e[1] for e in events], ["msg_a", "msg_c", "msg_d"])


if __name__ == "__main__":
    unittest.main()
