# tests/test_kill.py
"""Stopping a panel the right-click menu cannot reach.

Two properties carry this module, and both are about *not* killing things:

- **The lock decides, before and after.** A panel is running when the lock is
  held, and it is gone when the lock is free -- never because a pid file said
  so. The pid file is a diagnostic that outlives hard kills.
- **A pid is checked before it is signalled.** Windows reuses pid numbers, so a
  leftover `widget.pid` can name somebody else's process, and the by-hand
  `Stop-Process -Id (Get-Content data\\widget.pid)` this command replaces had no
  way to notice.

The happy path is exercised against a real subprocess holding the real lock
rather than a mock, because the thing under test is precisely whether the
operating system let go of the lock -- which a mock cannot get wrong.
"""

import contextlib
import io
import os
import signal
import subprocess
import sys
import unittest
import unittest.mock

from cost_meter import kill, launch, paths, store
from tests.support import TempHome

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The stand-in panel: claims the lock the real one claims, records its pid the
# way the real one does, then sits there. Written to a file named widget.py
# because that name is how kill.classify recognises a panel on POSIX.
FAKE_PANEL = """\
import sys, time
sys.path.insert(0, {root!r})
from cost_meter import paths, store
handle = store.try_acquire(paths.widget_lock_path())
if handle is None:
    print("lock taken", flush=True)
    raise SystemExit(1)
paths.pid_path().parent.mkdir(parents=True, exist_ok=True)
paths.pid_path().write_text(str(__import__("os").getpid()), encoding="utf-8")
print("up", flush=True)
time.sleep(120)
"""


def tasklist_row(image, pid):
    """One line of `tasklist /NH /FO CSV` output, as the real thing prints it."""
    return f'"{image}","{pid}","Console","1","20,000 K"\n'


class KillTest(TempHome):
    def run_stop(self, *argv):
        """kill.main with stdout captured. Returns (status, printed text)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = kill.main(list(argv))
        return status, out.getvalue()

    def start_fake_panel(self):
        """A real process holding the real lock, cleaned up however the test ends."""
        script = os.path.join(self.tmp, "panel", "widget.py")
        os.makedirs(os.path.dirname(script), exist_ok=True)
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(FAKE_PANEL.format(root=PROJECT_ROOT))
        proc = subprocess.Popen([sys.executable, script],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=dict(os.environ))
        self.addCleanup(self._reap, proc)
        line = proc.stdout.readline().strip()
        if line != "up":
            raise RuntimeError(f"fake panel failed: {line!r} {proc.stderr.read()!r}")
        return proc

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        proc.stdout.close()
        proc.stderr.close()


class TestNothingRunning(KillTest):
    def test_says_so_and_succeeds(self):
        status, printed = self.run_stop()
        self.assertEqual(status, 0)
        self.assertIn("kill: nothing running", printed)

    def test_clears_a_pid_file_no_panel_stands_behind(self):
        """The leftover a hard-killed panel leaves, and what makes `kill` idempotent.

        Without this, running the command twice prints a bare `nothing running`
        the second time and leaves a file naming a dead process for the next
        person to puzzle over.
        """
        paths.pid_path().write_text("4242\n", encoding="utf-8")
        status, printed = self.run_stop()
        self.assertEqual(status, 0)
        self.assertIn("cleared stale pid 4242", printed)
        self.assertFalse(paths.pid_path().exists())

    def test_leaves_a_pid_file_that_names_someone_else_alone(self):
        """clear_pid_file is a claim check, not an unlink.

        A panel started between the kill and the cleanup owns the file; erasing
        its number would leave the launcher logging `already running (pid None)`.
        """
        paths.pid_path().write_text("4242\n", encoding="utf-8")
        self.assertFalse(kill.clear_pid_file(99))
        self.assertTrue(paths.pid_path().exists())


class TestRefusals(KillTest):
    """The cases where something is running and killing it would be a guess."""

    def hold_the_lock(self):
        handle = store.try_acquire(paths.widget_lock_path())
        self.assertIsNotNone(handle)
        self.addCleanup(store.release, handle)

    def test_lock_held_but_no_pid_file(self):
        self.hold_the_lock()
        status, printed = self.run_stop()
        self.assertEqual(status, 1)
        self.assertIn("does not say which process", printed)

    def test_pid_names_a_dead_process(self):
        self.hold_the_lock()
        paths.pid_path().write_text("4242\n", encoding="utf-8")
        with unittest.mock.patch.object(kill, "classify", return_value=kill.GONE):
            status, printed = self.run_stop()
        self.assertEqual(status, 1)
        self.assertIn("which is not running", printed)
        # Kept: it is the only lead left for whoever goes looking by hand.
        self.assertTrue(paths.pid_path().exists())

    def test_pid_names_an_unrelated_process(self):
        """The pid-reuse accident, and the whole reason classify() exists."""
        self.hold_the_lock()
        paths.pid_path().write_text("4242\n", encoding="utf-8")
        with unittest.mock.patch.object(kill, "classify", return_value=kill.OTHER) as \
                classified, unittest.mock.patch.object(kill.os, "kill") as killed:
            status, printed = self.run_stop()
        classified.assert_called_once_with(4242)
        killed.assert_not_called()
        self.assertEqual(status, 1)
        self.assertIn("refusing to kill it", printed)

    def test_an_unanswerable_check_kills_anyway(self):
        """UNKNOWN is not a refusal: the lock already proved a panel is up."""
        self.hold_the_lock()
        paths.pid_path().write_text("4242\n", encoding="utf-8")
        with unittest.mock.patch.object(kill, "classify", return_value=kill.UNKNOWN), \
                unittest.mock.patch.object(kill.os, "kill") as killed, \
                unittest.mock.patch.object(kill, "wait_for_exit", return_value=True):
            status, _ = self.run_stop()
        self.assertEqual(status, 0)
        self.assertTrue(killed.called)


class TestClassify(unittest.TestCase):
    """What the operating system is asked, and how its answer is read.

    The Windows half runs everywhere -- tasklist is behind subprocess.run, so a
    recorded answer exercises the parsing on any platform. The POSIX half needs
    a real /proc and says so.
    """

    def windows_answer(self, stdout):
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout)
        return unittest.mock.patch.object(kill.subprocess, "run", return_value=completed)

    def test_panel_image_is_a_panel(self):
        for image in ("pythonw.exe", "python.exe", "PYTHONW.EXE"):
            with self.subTest(image=image), self.windows_answer(tasklist_row(image, 4242)):
                self.assertEqual(kill._windows_classify(4242), kill.PANEL)

    def test_another_image_is_not(self):
        with self.windows_answer(tasklist_row("notepad.exe", 4242)):
            self.assertEqual(kill._windows_classify(4242), kill.OTHER)

    def test_no_such_pid(self):
        # tasklist exits 0 and prints this rather than failing, so a parser that
        # only checked the exit status would read it as a live process.
        with self.windows_answer(
                "INFO: No tasks are running which match the specified criteria.\n"):
            self.assertEqual(kill._windows_classify(4242), kill.GONE)

    def test_a_row_for_a_different_pid_is_not_a_match(self):
        with self.windows_answer(tasklist_row("pythonw.exe", 99)):
            self.assertEqual(kill._windows_classify(4242), kill.GONE)

    def test_tasklist_that_will_not_answer(self):
        with unittest.mock.patch.object(
                kill.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("tasklist", 10)):
            self.assertEqual(kill._windows_classify(4242), kill.UNKNOWN)

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
    def test_proc_tells_this_process_from_a_panel(self):
        # The test runner is not a panel, and no pid at all is GONE.
        self.assertEqual(kill._posix_classify(os.getpid()), kill.OTHER)
        self.assertEqual(kill._posix_classify(2 ** 22), kill.GONE)

    def test_windows_escalates_no_further_than_terminate(self):
        """TerminateProcess is already the last resort; a second one adds nothing."""
        with unittest.mock.patch.object(kill.os, "name", "nt"):
            self.assertEqual([s for s, _ in kill.signal_plan()], [signal.SIGTERM])

    @unittest.skipIf(os.name == "nt", "SIGKILL is POSIX-only")
    def test_posix_escalates_to_sigkill(self):
        with unittest.mock.patch.object(kill.os, "name", "posix"):
            self.assertEqual([s for s, _ in kill.signal_plan()],
                             [signal.SIGTERM, signal.SIGKILL])


class TestAgainstARealPanel(KillTest):
    """The whole command against a process that genuinely holds the lock."""

    def test_kills_it_and_the_lock_comes_free(self):
        proc = self.start_fake_panel()
        self.assertTrue(launch.panel_is_running())

        status, printed = self.run_stop()

        self.assertEqual(status, 0)
        self.assertIn(f"kill: panel gone (pid {proc.pid})", printed)
        # The lock, not the printed line: this is the claim the launcher reads,
        # and the only one that decides whether the next session gets a panel.
        self.assertFalse(launch.panel_is_running())
        self.assertFalse(paths.pid_path().exists())
        self.assertIsNotNone(proc.poll())

    def test_restart_opens_a_fresh_panel_once_the_old_one_is_gone(self):
        """Order is the point: launch is called, and only after the lock is free.

        launch.main is stubbed rather than allowed to spawn -- a test that
        started a real GTK panel would need a display and would leave one behind.
        """
        self.start_fake_panel()
        seen = []

        def fake_launch(argv):
            seen.append((list(argv), launch.panel_is_running()))
            return 0

        with unittest.mock.patch.object(kill.launch, "main", fake_launch):
            status, _ = self.run_stop("--restart")

        self.assertEqual(status, 0)
        self.assertEqual(seen, [(["--force"], False)])

    def test_restart_does_not_start_a_second_panel_when_the_first_survives(self):
        """A failed kill must not end with two panels, or a launch that logs a race."""
        self.start_fake_panel()
        with unittest.mock.patch.object(kill, "classify", return_value=kill.OTHER), \
                unittest.mock.patch.object(kill.launch, "main") as launched:
            status, _ = self.run_stop("--restart")
        self.assertEqual(status, 1)
        launched.assert_not_called()


if __name__ == "__main__":
    unittest.main()
