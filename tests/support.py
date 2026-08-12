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
