#!/usr/bin/env python3
"""Unit tests, against a throwaway data directory.

COST_METER_HOME is redirected so a run never reads or writes the real ledger.
Python rather than `bash -c` with mktemp, so Windows runs the very same command
as Linux instead of a hand-written twin that can drift.
"""

import os
import sys
import tempfile
import unittest


def main():
    # ignore_cleanup_errors because Windows refuses to remove a file another
    # handle still holds open, and a teardown race must not fail a green run.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        os.environ["COST_METER_HOME"] = tmp
        suite = unittest.defaultTestLoader.discover("tests")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
