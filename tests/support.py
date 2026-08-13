# tests/support.py
"""Shared base for tests that write into COST_METER_HOME.

The variable is *restored* rather than unset on teardown: run_tests.py points it
at one throwaway directory for the whole run, so a test that removed it would
send every test after it at the real data/ directory.

COST_METER_TRANSCRIPTS is redirected as well. A test that reaches a front end's
main() reaches tally.refresh, which scans the transcript root; left at its
default that is the user's real ~/.claude/projects, and the test would read live
data and take as long as the real ledger is large.

COST_METER_CLAUDE_HOME for the same reason and one more: tally.refresh also asks
how the session is being billed, which reads ~/.claude/.credentials.json. A test
must not go near the real one -- it holds live OAuth tokens, and a test that
depended on it would pass or fail according to how the machine happens to be
logged in.

COST_METER_CLAUDE_CONFIG points at a stand-in for ~/.claude.json, which holds the
account's limit figures. It is a separate variable rather than something derived
from COST_METER_CLAUDE_HOME because that file is a *sibling* of ~/.claude/ rather
than a file inside it, so redirecting the directory does not move it.
"""

import os
import tempfile
import unittest

from cost_meter import paths, store


class TempHome(unittest.TestCase):
    def setUp(self):
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

    def write_claude_config(self, data):
        """Stand in for ~/.claude.json, which holds the account's limit figures.

        Redirected for the same reason as .credentials.json beside it: the real
        file describes however this machine happens to be logged in and how
        recently it was used, so a test that read it would pass or fail on the
        weather.
        """
        store.write_json_atomic(paths.claude_config_path(), data)
