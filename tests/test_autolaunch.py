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
import subprocess
import sys
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


class ExitedChild:
    """A spawn that was over before we looked -- what a failed scope leaves."""

    pid = 4243

    def wait(self, timeout=None):
        return 0


class LiveChild:
    """A spawn still running when we looked -- what a scope that took looks like."""

    pid = 4244

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("systemd-run", timeout)


class ScopeFallbackTest(TempHome):
    """When a scope spawn is over immediately, whether to try again without one.

    The immediate exit used to mean one thing: systemd-run could not reach the
    user manager, so a plain detached child is the best available. Since a panel
    that finds `widget.lock` taken exits at once rather than opening a second
    window, it means a second thing too -- and that one is not a failure, it is
    the race being settled. Retrying then spawns another panel that will stand
    down exactly the same way, and files a systemd complaint about it in the log
    for somebody to chase later.

    So the lock decides which of the two it was.
    """

    def setUp(self):
        super().setUp()
        self.spawned = []
        for name, value in (("systemd_available", lambda: True),
                            ("_spawn", self.record)):
            real = getattr(launch, name)
            setattr(launch, name, value)
            self.addCleanup(setattr, launch, name, real)

    def record(self, command, cwd):
        self.spawned.append(command)
        return self.children.pop(0)

    def use_panel_probe(self, *answers):
        """Answer the liveness probe once per call, last answer repeating.

        A sequence rather than one value because the race needs both: the
        launcher looks, finds nothing, spawns -- and by the time the spawn is
        over a rival's panel holds the lock. One fixed answer can only express
        one of those two moments.
        """
        answers = list(answers)
        real = launch.panel_is_running
        launch.panel_is_running = lambda: (answers.pop(0) if len(answers) > 1
                                           else answers[0])
        self.addCleanup(setattr, launch, "panel_is_running", real)

    def test_a_scope_that_took_is_the_panel(self):
        self.children = [LiveChild()]
        child = launch.spawn_detached(["pixi"], ".")
        self.assertIsInstance(child, LiveChild)
        self.assertEqual(len(self.spawned), 1)

    def test_a_failed_scope_is_retried_without_one(self):
        self.children = [ExitedChild(), FakeChild()]
        self.use_panel_probe(False)
        child = launch.spawn_detached(["pixi"], ".")
        self.assertIsInstance(child, FakeChild)
        self.assertEqual(len(self.spawned), 2)
        self.assertIn("scope spawn exited immediately", self.log_text())

    def test_a_panel_standing_down_for_another_is_not_retried(self):
        self.children = [ExitedChild()]
        self.use_panel_probe(True)
        self.assertIsNone(launch.spawn_detached(["pixi"], "."))
        self.assertEqual(len(self.spawned), 1)
        self.assertNotIn("scope spawn exited immediately", self.log_text())

    def test_the_launcher_reports_standing_down_rather_than_a_pid(self):
        # Nothing was up when the launcher looked; something is by the time its
        # spawn is over. That is the race as the hook actually meets it.
        self.children = [ExitedChild()]
        self.use_panel_probe(False, True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(launch.main(["--force"]), 0)
        self.assertIn("another panel", out.getvalue())
        self.assertNotIn("spawned", out.getvalue())


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


class PanelOutputTest(TempHome):
    """The panel's own stdout and stderr must end up somewhere a reader can find.

    Regression test with a real cause, and one that cost a diagnosis. On Windows
    the panel runs under pythonw with no console, so every word it said on the
    way up went to a discarded handle. When Smart App Control started blocking
    the unsigned GTK DLLs in the pixi environment, the panel died on `import gi`
    before it could draw anything -- and the only trace of it anywhere was
    cost-meter.log saying `launch: spawned pixi pid N`, which was true. The
    launcher had done its job perfectly; nothing had recorded that the panel
    then fell over.

    These exercise `_spawn` rather than `spawn_detached`, deliberately: the
    redirect lives in the former, and the latter adds a systemd scope on Linux
    that would make the same assertions platform-dependent for no gain.
    """

    def output_text(self):
        try:
            return paths.widget_output_path().read_text(encoding="utf-8")
        except OSError:
            return ""

    def run_child(self, code):
        """Run a child to completion through the real spawn path."""
        child = launch._spawn([sys.executable, "-c", code], os.getcwd())
        child.wait(timeout=60)
        return child

    def test_a_crashing_panel_leaves_its_traceback_behind(self):
        """The whole point: the failure is legible after the fact."""
        self.run_child("raise RuntimeError('panel fell over')")
        text = self.output_text()
        self.assertIn("Traceback", text)
        self.assertIn("panel fell over", text)

    def test_a_panel_that_starts_cleanly_says_only_that_it_was_launched(self):
        self.run_child("pass")
        # The header alone, so a reader who opens the file after a good launch
        # is not left wondering which of two silences they are looking at.
        self.assertIn("===", self.output_text())
        self.assertNotIn("Traceback", self.output_text())

    def test_the_scope_retry_keeps_both_attempts(self):
        """Append, not truncate. The first attempt is why there was a second."""
        self.run_child("import sys; sys.stderr.write('first attempt\n')")
        self.run_child("import sys; sys.stderr.write('second attempt\n')")
        text = self.output_text()
        self.assertIn("first attempt", text)
        self.assertIn("second attempt", text)

    def test_the_file_is_started_again_once_it_outgrows_the_cap(self):
        """Bounded across sessions, or it is a slow leak nobody is watching."""
        path = paths.widget_output_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * (launch.OUTPUT_LIMIT_BYTES + 1), encoding="utf-8")
        self.run_child("pass")
        self.assertLess(path.stat().st_size, launch.OUTPUT_LIMIT_BYTES)
        self.assertNotIn("xxxx", self.output_text())

    def test_a_file_that_cannot_be_opened_still_starts_the_panel(self):
        """A full disk is not a reason to refuse to draw the window.

        Simulated by putting a directory where the file belongs, which is the
        one way to make open() fail that behaves the same on both platforms.
        """
        paths.widget_output_path().mkdir(parents=True, exist_ok=True)
        child = self.run_child("pass")
        self.assertEqual(child.returncode, 0)

