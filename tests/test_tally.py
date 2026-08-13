# tests/test_tally.py
"""End-to-end passes through tally.refresh: the parser -> store -> summary seam.

Everything runs against a throwaway COST_METER_HOME and a fixture transcript
tree, so the user's real data is never read or written. The real pricing.json is
used on purpose — these assertions are the only automated check that the whole
wiring produces the right dollar figure.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import tally
from cost_meter import paths, store

# 1M output tokens of claude-opus-5 at $25/Mtok. Chosen because the rate is
# stable, unlike the Sonnet introductory rate.
ONE_MILLION_OUT_USD = 25.0


def message(msg_id, session, epoch, output_tokens=1_000_000, model="claude-opus-5"):
    return {
        "timestamp": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
        "sessionId": session,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {"input_tokens": 0, "output_tokens": output_tokens},
        },
    }


class TallyTestCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.transcripts = Path(tempfile.mkdtemp())
        # refresh() records how the session is billed, which reads Claude Code's
        # own directory. Redirected here so no test goes near the real
        # .credentials.json and its live tokens.
        self.claude_home = Path(tempfile.mkdtemp())
        # refresh() also reads the account's cached limit figures out of
        # ~/.claude.json. Left at its default, the real file's reset time bounds
        # the 5-hour window and these fixtures fall outside it -- which reads as
        # a wrong dollar figure rather than as an error.
        self.claude_config = self.claude_home / "config-absent.json"
        for name, value in (("COST_METER_HOME", self.home),
                            ("COST_METER_TRANSCRIPTS", self.transcripts),
                            ("COST_METER_CLAUDE_HOME", self.claude_home),
                            ("COST_METER_CLAUDE_CONFIG", self.claude_config)):
            previous = os.environ.get(name)
            os.environ[name] = str(value)
            self.addCleanup(self._restore, name, previous)

        # Midday local time, so "today" never straddles a midnight boundary.
        self.now = datetime.now().replace(hour=12, minute=0, second=0,
                                         microsecond=0).timestamp()

    @staticmethod
    def _restore(name, previous):
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    def append(self, filename, *entries):
        path = self.transcripts / filename
        with open(path, "a", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def state(self):
        return store.read_json(paths.state_path())


class TestRefreshHappyPath(TallyTestCase):
    def test_one_message_is_priced_to_the_cent(self):
        self.append("a.jsonl", message("m1", "s1", self.now - 60))
        state = tally.refresh("s1", now=self.now)

        self.assertAlmostEqual(state["today_usd"], ONE_MILLION_OUT_USD)
        self.assertAlmostEqual(state["session"]["usd"], ONE_MILLION_OUT_USD)
        self.assertAlmostEqual(state["last_turn_usd"], ONE_MILLION_OUT_USD)
        self.assertAlmostEqual(state["window_5h"]["usd"], ONE_MILLION_OUT_USD)
        self.assertEqual(state["unknown_models"], [])
        self.assertEqual(self.state(), state)

    def test_a_rerun_with_nothing_new_reports_no_turn_but_keeps_the_totals(self):
        self.append("a.jsonl", message("m1", "s1", self.now - 60))
        tally.refresh("s1", now=self.now)
        state = tally.refresh("s1", now=self.now)

        self.assertAlmostEqual(state["last_turn_usd"], 0.0)
        self.assertAlmostEqual(state["session"]["usd"], ONE_MILLION_OUT_USD)

    def test_a_second_turn_is_the_only_thing_in_last_turn(self):
        self.append("a.jsonl", message("m1", "s1", self.now - 600))
        tally.refresh("s1", now=self.now)
        self.append("a.jsonl", message("m2", "s1", self.now - 60))
        state = tally.refresh("s1", now=self.now)

        self.assertAlmostEqual(state["last_turn_usd"], ONE_MILLION_OUT_USD)
        self.assertAlmostEqual(state["session"]["usd"], 2 * ONE_MILLION_OUT_USD)

    def test_a_turn_of_several_messages_is_summed(self):
        self.append("a.jsonl",
                    message("m1", "s1", self.now - 90),
                    message("m2", "s1", self.now - 60))
        state = tally.refresh("s1", now=self.now)
        self.assertAlmostEqual(state["last_turn_usd"], 2 * ONE_MILLION_OUT_USD)


class TestBillingIsRecorded(TallyTestCase):
    """The hook is the only place that can answer how this session pays.

    It runs inside the session's own process, so a key exported for one session
    is visible to it and to nothing else. Asking from the panel instead would
    report the panel's environment for every session on the machine.
    """

    def test_the_state_records_how_the_session_is_billed(self):
        (self.claude_home / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"subscriptionType": "team",
                                          "rateLimitTier": "default_claude_max_5x"}}),
            encoding="utf-8")
        self.append("a.jsonl", message("m1", "s1", self.now - 60))

        state = tally.refresh("s1", now=self.now)
        self.assertEqual(state["billing"],
                         {"mode": "seat", "label": "team · max 5x"})

    def test_an_unanswerable_environment_is_recorded_as_unknown(self):
        # Nothing written into claude_home at all. The row has to say so rather
        # than let the previous run's answer stand.
        self.append("a.jsonl", message("m1", "s1", self.now - 60))
        state = tally.refresh("s1", now=self.now)
        self.assertEqual(state["billing"]["mode"], "unknown")


class TestConcurrentSessions(TallyTestCase):
    def test_two_interleaved_sessions_each_report_their_own_turn(self):
        self.append("a.jsonl", message("a1", "s1", self.now - 300))
        self.append("b.jsonl", message("b1", "s2", self.now - 290))
        # Both hooks fire; whichever runs first absorbs both messages.
        first = tally.refresh("s2", now=self.now)
        second = tally.refresh("s1", now=self.now)
        self.assertAlmostEqual(first["last_turn_usd"], ONE_MILLION_OUT_USD)
        self.assertAlmostEqual(second["last_turn_usd"], ONE_MILLION_OUT_USD)

        # Second round of turns, in the opposite order.
        self.append("a.jsonl", message("a2", "s1", self.now - 60))
        self.append("b.jsonl", message("b2", "s2", self.now - 50))
        third = tally.refresh("s1", now=self.now)
        fourth = tally.refresh("s2", now=self.now)
        self.assertAlmostEqual(third["last_turn_usd"], ONE_MILLION_OUT_USD)
        self.assertAlmostEqual(third["session"]["usd"], 2 * ONE_MILLION_OUT_USD)
        self.assertAlmostEqual(fourth["last_turn_usd"], ONE_MILLION_OUT_USD)
        self.assertAlmostEqual(fourth["session"]["usd"], 2 * ONE_MILLION_OUT_USD)

    def test_events_appended_by_another_sessions_run_still_count_as_ours(self):
        # The reproduced failure: session 1 is up to date, then both sessions
        # produce a turn and session 2's hook fires first, so its scan appends
        # session 1's message as well. Session 1's own run must still see it.
        self.append("a.jsonl", message("a1", "s1", self.now - 600))
        tally.refresh("s1", now=self.now)

        self.append("a.jsonl", message("a2", "s1", self.now - 90))
        self.append("b.jsonl", message("b1", "s2", self.now - 60))
        absorbing = tally.refresh("s2", now=self.now)
        self.assertAlmostEqual(absorbing["last_turn_usd"], ONE_MILLION_OUT_USD)

        state = tally.refresh("s1", now=self.now)
        self.assertAlmostEqual(state["last_turn_usd"], ONE_MILLION_OUT_USD)
        self.assertAlmostEqual(state["session"]["usd"], 2 * ONE_MILLION_OUT_USD)

    def test_a_sessions_first_ever_run_is_unaffected_by_older_sessions(self):
        self.append("a.jsonl", message("a1", "s1", self.now - 600))
        tally.refresh("s1", now=self.now)
        self.append("b.jsonl", message("b1", "s2", self.now - 60))
        state = tally.refresh("s2", now=self.now)
        self.assertAlmostEqual(state["last_turn_usd"], ONE_MILLION_OUT_USD)
        self.assertAlmostEqual(state["today_usd"], 2 * ONE_MILLION_OUT_USD)


class TestSessionMarks(TallyTestCase):
    def test_a_bookmark_is_stored_per_session(self):
        self.append("a.jsonl", message("a1", "s1", self.now - 60))
        self.append("b.jsonl", message("b1", "s2", self.now - 50))
        tally.refresh("s1", now=self.now)
        tally.refresh("s2", now=self.now)

        marks = store.read_json(paths.session_marks_path())
        self.assertEqual(sorted(marks), ["s1", "s2"])
        self.assertEqual(marks["s1"]["ids"], ["a1"])
        self.assertEqual(marks["s2"]["ids"], ["b1"])

    def test_a_bookmark_older_than_the_prune_window_is_dropped(self):
        self.append("a.jsonl", message("a1", "s1", self.now - 60))
        tally.refresh("s1", now=self.now)
        # A later run, a fortnight on: s1's events are long pruned.
        self.append("b.jsonl", message("b1", "s2", self.now + 14 * 86400))
        tally.refresh("s2", now=self.now + 14 * 86400)

        marks = store.read_json(paths.session_marks_path())
        self.assertEqual(sorted(marks), ["s2"])

    def test_an_empty_session_id_writes_no_bookmark(self):
        self.append("a.jsonl", message("a1", "s1", self.now - 60))
        state = tally.refresh("", now=self.now)

        self.assertAlmostEqual(state["last_turn_usd"], 0.0)
        self.assertAlmostEqual(state["today_usd"], ONE_MILLION_OUT_USD)
        self.assertFalse(paths.session_marks_path().exists())


if __name__ == "__main__":
    unittest.main()
