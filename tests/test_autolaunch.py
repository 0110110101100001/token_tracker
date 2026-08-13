# tests/test_autolaunch.py
"""Suspending the panel's auto-launch, and what a suspended hook must still do.

The panel is a singleton, so a paused auto-launch is not about avoiding a second
window -- it is about a panel you closed on purpose staying closed. Without it,
the next Claude Code session brings it straight back, and the only way to keep it
away is to stop starting sessions.

Two properties here break silently rather than loudly, which is why they are
tested rather than eyeballed:

- **Recording is untouched.** Only the SessionStart launch is suspended; the Stop
  hook keeps appending to the ledger, so a paused week leaves no hole in the
  figures.
- **config.json has three writers now.** The window position, the panel's scale
  and this flag share one file, and an unlocked read-modify-write on any of them
  silently drops whichever key the other side wrote moments earlier.
"""

import contextlib
import io
import os
import tempfile
import unittest

from cost_meter import autolaunch, launch, paths, store


class TempHome(unittest.TestCase):
    """Base for tests that write into COST_METER_HOME.

    The previous value is restored rather than unset: run_tests.py points the
    variable at one throwaway directory for the whole run, and a test that
    removed it would send every test after it at the real data/ directory.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        previous = os.environ.get("COST_METER_HOME")
        os.environ["COST_METER_HOME"] = self.tmp

        def restore():
            if previous is None:
                os.environ.pop("COST_METER_HOME", None)
            else:
                os.environ["COST_METER_HOME"] = previous

        self.addCleanup(restore)

    def config(self):
        return store.read_json(paths.config_path(), default={}) or {}

    def write_config(self, config):
        store.write_json_atomic(paths.config_path(), config)

    def log_text(self):
        try:
            return paths.log_path().read_text(encoding="utf-8")
        except OSError:
            return ""


class PausedFlagTest(TempHome):
    def test_a_fresh_install_launches(self):
        # No config file at all, which is what the first ever run sees.
        self.assertFalse(autolaunch.paused())

    def test_pausing_and_resuming_round_trip(self):
        autolaunch.set_paused(True)
        self.assertTrue(autolaunch.paused())
        autolaunch.set_paused(False)
        self.assertFalse(autolaunch.paused())

    def test_pausing_twice_leaves_it_paused(self):
        # The flag names the state you want, not a transition you have to be
        # mid-way through. Anything that pauses is free to do so without
        # checking first.
        autolaunch.set_paused(True)
        autolaunch.set_paused(True)
        self.assertTrue(autolaunch.paused())

    def test_resuming_something_that_was_never_paused_is_not_an_error(self):
        autolaunch.set_paused(False)
        self.assertFalse(autolaunch.paused())

    def test_resuming_removes_the_key_rather_than_recording_a_false(self):
        # Absent means live, so the resumed state is the same shape as a config
        # that never carried the key. One state, one representation.
        autolaunch.set_paused(True)
        autolaunch.set_paused(False)
        self.assertNotIn(autolaunch.KEY, self.config())

    def test_a_corrupt_config_reads_as_live(self):
        """Fail open, and this is the whole reason.

        The alternative is a panel that silently never starts again, with the
        one file that could explain why being the one that cannot be read.
        """
        paths.config_path().parent.mkdir(parents=True, exist_ok=True)
        paths.config_path().write_text("{ not json", encoding="utf-8")
        self.assertFalse(autolaunch.paused())

    def test_the_other_settings_survive_being_paused(self):
        # The property that breaks silently: an unlocked rewrite here would drop
        # the panel's remembered geometry, and it would reopen somewhere else.
        self.write_config({"widget_scale": 1.4, "widget_position": [10, 20]})
        autolaunch.set_paused(True)
        autolaunch.set_paused(False)
        self.assertEqual(self.config(),
                         {"widget_scale": 1.4, "widget_position": [10, 20]})


class LaunchTest(TempHome):
    """What the launcher does with the flag set — for the hook, and for a human.

    The hook obeys the pause and speaks only to the log. `--force` is the other
    caller: somebody who typed a command and is owed both a panel and an answer.

    `spawn_detached` and `has_display` are swapped out rather than mocked: the
    real spawn would put a panel on the developer's screen, and the display test
    would make the answer depend on whether the suite runs headless.
    """

    def setUp(self):
        super().setUp()
        self.spawned = []
        real_spawn = launch.spawn_detached
        real_display = launch.has_display
        launch.spawn_detached = lambda command, cwd: self.record(command)
        launch.has_display = lambda: True

        def restore():
            launch.spawn_detached = real_spawn
            launch.has_display = real_display

        self.addCleanup(restore)

    def record(self, command):
        self.spawned.append(command)
        return FakeChild()

    def test_a_paused_session_starts_no_panel(self):
        autolaunch.set_paused(True)
        self.assertEqual(launch.main([]), 0)
        self.assertEqual(self.spawned, [])

    def test_a_paused_hook_says_so_in_the_log(self):
        # A hook that ran and decided to do nothing has to be distinguishable
        # from one that never ran; that is the entire convention this log keeps.
        autolaunch.set_paused(True)
        launch.main([])
        self.assertIn("launch: paused", self.log_text())

    def test_a_paused_hook_does_not_even_probe_the_lock(self):
        # Checked first, before the liveness probe: the lock file is created by
        # the probe, so its absence is the observable proof of the ordering.
        autolaunch.set_paused(True)
        launch.main([])
        self.assertFalse(paths.widget_lock_path().exists())

    def test_a_live_session_starts_the_panel(self):
        self.assertEqual(launch.main([]), 0)
        self.assertEqual(len(self.spawned), 1)

    def test_a_corrupt_config_still_starts_the_panel(self):
        paths.config_path().parent.mkdir(parents=True, exist_ok=True)
        paths.config_path().write_text("{ not json", encoding="utf-8")
        launch.main([])
        self.assertEqual(len(self.spawned), 1)

    def test_resuming_starts_the_panel_again(self):
        autolaunch.set_paused(True)
        launch.main([])
        autolaunch.set_paused(False)
        launch.main([])
        self.assertEqual(len(self.spawned), 1)

    def test_force_opens_the_panel_although_it_is_paused(self):
        autolaunch.set_paused(True)
        self.assertEqual(launch.main(["--force"]), 0)
        self.assertEqual(len(self.spawned), 1)

    def test_force_leaves_the_pause_in_place(self):
        # Opening the panel by hand says nothing about what the next session
        # should do. The pause is a statement about sessions; overriding it once
        # is not a decision to stop overriding it every time after.
        autolaunch.set_paused(True)
        launch.main(["--force"])
        self.assertTrue(autolaunch.paused())

    def test_force_reports_what_it_did(self):
        # The failure this exists to prevent, and it cost a real half hour: a
        # paused launch printed nothing and exited 0, which looks identical to a
        # panel that opened fine.
        autolaunch.set_paused(True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            launch.main(["--force"])
        self.assertIn("spawned", out.getvalue())

    def test_force_says_so_when_a_panel_is_already_up(self):
        # Swapped out rather than really held: taking the panel's lock for the
        # duration would make this test race the developer's own panel.
        real_probe = launch.panel_is_running
        launch.panel_is_running = lambda: True
        self.addCleanup(lambda: setattr(launch, "panel_is_running", real_probe))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            launch.main(["--force"])
        self.assertIn("already running", out.getvalue())
        self.assertEqual(self.spawned, [])

    def test_the_hook_prints_nothing(self):
        # It runs on the critical path of starting a session, so the log is the
        # only place it is allowed to speak.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            launch.main([])
        self.assertEqual(out.getvalue(), "")


class FakeChild:
    """Enough of a Popen for the log line that names its pid."""

    pid = 4242


class CommandLineTest(TempHome):
    def run_cli(self, argv):
        """The exit code and whatever it printed."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = autolaunch.main(argv)
        return code, out.getvalue()

    def test_off_then_on(self):
        self.assertEqual(self.run_cli(["--off"])[0], 0)
        self.assertTrue(autolaunch.paused())
        self.assertEqual(self.run_cli(["--on"])[0], 0)
        self.assertFalse(autolaunch.paused())

    def test_off_twice_is_success_rather_than_an_error(self):
        self.assertEqual(self.run_cli(["--off"])[0], 0)
        self.assertEqual(self.run_cli(["--off"])[0], 0)

    def test_status_reports_both_states(self):
        self.assertIn("live", self.run_cli(["--status"])[1])
        autolaunch.set_paused(True)
        self.assertIn("paused", self.run_cli(["--status"])[1])

    def test_status_changes_nothing(self):
        self.run_cli(["--status"])
        self.assertEqual(self.config(), {})

    def test_a_bare_run_is_rejected(self):
        # Nothing to default to: a run that silently did nothing would report
        # success for a pause that never happened.
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                autolaunch.main([])

    def test_off_and_on_together_are_rejected(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                autolaunch.main(["--off", "--on"])


if __name__ == "__main__":
    unittest.main()
