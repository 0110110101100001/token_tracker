# tests/test_paths.py
"""The one filesystem location that is not inside a directory we redirect."""

import os
import unittest
from pathlib import Path

from cost_meter import paths


class ClaudeConfigPathTest(unittest.TestCase):
    def setUp(self):
        previous = os.environ.pop("COST_METER_CLAUDE_CONFIG", None)
        self.addCleanup(self._restore, previous)

    @staticmethod
    def _restore(previous):
        if previous is None:
            os.environ.pop("COST_METER_CLAUDE_CONFIG", None)
        else:
            os.environ["COST_METER_CLAUDE_CONFIG"] = previous

    def test_default_is_the_file_beside_the_claude_directory(self):
        self.assertEqual(paths.claude_config_path(), Path.home() / ".claude.json")

    def test_it_is_a_sibling_of_claude_home_not_a_child_of_it(self):
        # The whole reason it gets its own override: deriving it from
        # claude_home() would put it inside a directory every test redirects, so
        # the real file would never be read -- and the reader would look
        # permanently empty rather than wrong, which is harder to notice.
        self.assertNotEqual(paths.claude_config_path().parent, paths.claude_home())

    def test_the_override_is_honoured(self):
        override = os.path.join("tmp", "fake.json")
        os.environ["COST_METER_CLAUDE_CONFIG"] = override
        self.assertEqual(paths.claude_config_path(), Path(override))
