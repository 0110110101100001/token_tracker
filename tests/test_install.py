# tests/test_install.py
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cost_meter import install, launch, paths

# An absolute path the current platform recognises as one. A POSIX-shaped path
# on Windows is not absolute, so `startswith(root + os.sep)` would compare two
# strings that never agree and every ownership test would pass vacuously.
ROOT = Path("C:/opt/cost-meter") if install.IS_WINDOWS else Path("/opt/cost-meter")
OTHER = (Path("C:/opt/cost-meter-other") if install.IS_WINDOWS
         else Path("/opt/cost-meter-other"))
# What write_autostart points at: the repo root on Windows, where the entry
# `cd /d`s there and calls a pixi task, and the launcher itself elsewhere.
OUR_AUTOSTART_TARGET = ROOT if install.IS_WINDOWS else ROOT / "run_widget.sh"


def expected(event):
    """The commands install.apply should register under `event`.

    Derived from install.HOOKS rather than spelled out, because the wrapper
    names differ by platform and a hardcoded "hooks/tally.sh" would only ever
    test one of them.
    """
    return [str(ROOT / script) for name, script, _ in install.HOOKS
            if name == event]


def settings_with(*commands, event="Stop"):
    """A settings file registering `commands` under one event, one group each."""
    return {
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": c, "timeout": 5}]}
                    for c in commands]
        }
    }


def commands(settings, event):
    """Every hook command registered under `event`.

    Skips anything that is not a group of hooks, because one test deliberately
    plants a shape the installer is only required to preserve, not to parse.
    """
    groups = settings.get("hooks", {}).get(event, [])
    return [h["command"]
            for g in groups if isinstance(g, dict)
            for h in g.get("hooks", []) if isinstance(h, dict)]


class TempHome(unittest.TestCase):
    """Base for tests that write into COST_METER_HOME."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["COST_METER_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "COST_METER_HOME", None)


class PidPathTest(unittest.TestCase):
    def test_pid_path_follows_cost_meter_home(self):
        # cost_meter/launch.py reaches the pid file through paths.pid_path(),
        # so this override is the whole mechanism that keeps a test run off the
        # real one.
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["COST_METER_HOME"] = tmp
            try:
                self.assertEqual(paths.pid_path(), Path(tmp) / "widget.pid")
            finally:
                del os.environ["COST_METER_HOME"]

    def test_pid_path_defaults_under_data(self):
        # run_tests.py runs the whole suite with COST_METER_HOME pointed at a
        # throwaway directory, so the default is only observable with it unset.
        previous = os.environ.pop("COST_METER_HOME", None)
        try:
            self.assertEqual(paths.pid_path(),
                             paths.project_root() / "data" / "widget.pid")
        finally:
            if previous is not None:
                os.environ["COST_METER_HOME"] = previous


class WidgetPidTest(TempHome):
    """The pid file's lifecycle, which cannot be reached through the UI.

    Quitting the panel for real means right-clicking a menu, so the write on
    startup and the removal on exit are covered here instead. Importing widget
    only loads the GTK bindings; it opens no display, so this runs headless.
    """

    def test_write_then_clear_round_trips(self):
        import widget
        widget.write_pid()
        self.assertEqual(paths.pid_path().read_text(encoding="utf-8").strip(),
                         str(os.getpid()))
        widget.clear_pid()
        self.assertFalse(paths.pid_path().exists())

    def test_clear_leaves_another_panels_claim_alone(self):
        # A panel killed without cleanup skips its own; if a replacement started
        # in the meantime, the corpse must not delete the survivor's file.
        import widget
        paths.pid_path().write_text("999999\n", encoding="utf-8")
        widget.clear_pid()
        self.assertEqual(paths.pid_path().read_text(encoding="utf-8").strip(),
                         "999999")

    def test_clear_tolerates_a_missing_or_junk_file(self):
        import widget
        widget.clear_pid()  # nothing there at all
        paths.pid_path().write_text("not-a-pid\n", encoding="utf-8")
        widget.clear_pid()  # must not raise
        self.assertTrue(paths.pid_path().exists())


class ReadPidTest(TempHome):
    def test_a_written_pid_reads_back(self):
        paths.pid_path().write_text("4321\n", encoding="utf-8")
        self.assertEqual(launch.read_pid(), 4321)

    def test_a_missing_file_is_no_claim(self):
        self.assertIsNone(launch.read_pid())

    def test_junk_is_no_claim(self):
        # What a hard kill mid-write leaves behind.
        paths.pid_path().write_text("not-a-pid\n", encoding="utf-8")
        self.assertIsNone(launch.read_pid())
        paths.pid_path().write_text("", encoding="utf-8")
        self.assertIsNone(launch.read_pid())


class PidLivenessTest(unittest.TestCase):
    """The probe that decides whether to start a second panel.

    Worth its own tests because the two platforms share no implementation:
    POSIX signals a pid, Windows waits on a process handle. os.kill is not an
    option on Windows at all — CPython maps it onto TerminateProcess there, so
    a signal-0 probe would kill the panel it is asking about.
    """

    def test_our_own_pid_is_alive(self):
        self.assertTrue(launch.pid_is_alive(os.getpid()))

    def test_an_exited_process_is_not_alive(self):
        # A child we started and waited for, rather than an invented number:
        # this pid is guaranteed to have existed and to be finished. On Windows
        # the Popen object still holds a handle, so the pid remains openable —
        # which is exactly why liveness is a wait, not an OpenProcess success.
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        self.assertFalse(launch.pid_is_alive(proc.pid))

    def test_a_missing_or_impossible_pid_is_not_alive(self):
        self.assertFalse(launch.pid_is_alive(None))
        self.assertFalse(launch.pid_is_alive(0))
        self.assertFalse(launch.pid_is_alive(-1))


class AutostartOwnershipTest(unittest.TestCase):
    """Removal is scoped to the entry we wrote.

    Regression test with a real cause: an early version deleted the autostart
    file unconditionally, so uninstalling against a throwaway --settings copy
    reached out and removed the live one.
    """

    def entry(self, target):
        """An autostart file in the current platform's format, naming `target`."""
        path = Path(tempfile.mkdtemp()) / install.AUTOSTART_NAME
        if install.IS_WINDOWS:
            path.write_text(f'@echo off\r\ncd /d "{target}" || exit /b 0\r\n',
                            encoding="utf-8")
        else:
            path.write_text(
                f"[Desktop Entry]\nType=Application\nExec={target}\n",
                encoding="utf-8")
        return path

    def test_our_own_entry_is_ours(self):
        self.assertTrue(
            install._autostart_is_ours(self.entry(OUR_AUTOSTART_TARGET), ROOT))

    def test_another_checkout_is_not_ours(self):
        target = OTHER if install.IS_WINDOWS else OTHER / "run_widget.sh"
        self.assertFalse(install._autostart_is_ours(self.entry(target), ROOT))

    def test_an_unrelated_entry_is_not_ours(self):
        target = ("C:/Program Files/Nextcloud/nextcloud.exe"
                  if install.IS_WINDOWS else "/usr/bin/nextcloud --background")
        self.assertFalse(install._autostart_is_ours(self.entry(target), ROOT))

    def test_a_missing_file_is_not_ours(self):
        missing = Path("C:/nonexistent") if install.IS_WINDOWS else Path("/nonexistent")
        self.assertFalse(install._autostart_is_ours(missing, ROOT))

    def test_the_entry_we_write_is_recognised_as_ours(self):
        # APPDATA is the Windows counterpart of XDG_CONFIG_HOME: the knob that
        # sends the entry somewhere disposable instead of the real Startup
        # folder.
        var = "APPDATA" if install.IS_WINDOWS else "XDG_CONFIG_HOME"
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get(var)
            os.environ[var] = tmp
            try:
                path = install.write_autostart(ROOT)
                self.assertTrue(install._autostart_is_ours(path, ROOT))
            finally:
                if previous is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = previous


class ApplyTest(unittest.TestCase):
    def test_registers_both_hooks(self):
        settings = {}
        install.apply(settings, ROOT)
        self.assertEqual(commands(settings, "Stop"), expected("Stop"))
        self.assertEqual(commands(settings, "SessionStart"),
                         expected("SessionStart"))

    def test_is_idempotent(self):
        settings = {}
        install.apply(settings, ROOT)
        install.apply(settings, ROOT)
        self.assertEqual(len(commands(settings, "Stop")), 1)
        self.assertEqual(len(commands(settings, "SessionStart")), 1)

    def test_preserves_unrelated_hooks(self):
        # The real settings.json on the development machine carries a PostToolUse
        # smoke-test hook. Losing somebody else's hook would be the worst way for
        # this installer to fail, because nothing would report it.
        other = str(OTHER / "run-smoke-tests")
        settings = settings_with(other, event="PostToolUse")
        settings["hooks"]["Stop"] = [
            {"hooks": [{"type": "command", "command": "/usr/local/bin/other"}]}
        ]
        install.apply(settings, ROOT)
        self.assertEqual(commands(settings, "PostToolUse"), [other])
        self.assertIn("/usr/local/bin/other", commands(settings, "Stop"))

    def test_replaces_a_legacy_entry_from_the_same_repo(self):
        # tally.py used to be registered directly, before the pixi wrapper. Two
        # live Stop hooks would both run, and the second would find no new events
        # and overwrite last_turn with 0.00 -- so the old entry must be replaced,
        # not accompanied.
        settings = settings_with(str(ROOT / "tally.py"))
        install.apply(settings, ROOT)
        self.assertEqual(commands(settings, "Stop"), expected("Stop"))

    @unittest.skipUnless(install.IS_WINDOWS, "Windows paths are case-insensitive")
    def test_replaces_an_entry_spelled_in_a_different_case(self):
        # The same repo reached through a differently-cased path is the same
        # repo. Matching case-sensitively would leave both Stop hooks live.
        settings = settings_with(str(ROOT).upper() + os.sep + "tally.py")
        install.apply(settings, ROOT)
        self.assertEqual(commands(settings, "Stop"), expected("Stop"))

    def test_leaves_another_checkout_alone(self):
        stranger = str(OTHER / "tally.py")
        settings = settings_with(stranger)
        install.apply(settings, ROOT)
        self.assertIn(stranger, commands(settings, "Stop"))

    def test_uninstall_restores_the_original_shape(self):
        original = settings_with("/usr/local/bin/other")
        settings = settings_with("/usr/local/bin/other")
        install.apply(settings, ROOT)
        install.apply(settings, ROOT, uninstall=True)
        self.assertEqual(settings, original)

    def test_uninstall_drops_the_hooks_key_when_it_was_ours_alone(self):
        settings = {}
        install.apply(settings, ROOT)
        install.apply(settings, ROOT, uninstall=True)
        self.assertEqual(settings, {})

    def test_tolerates_a_group_shape_it_does_not_understand(self):
        settings = {"hooks": {"Stop": ["not-a-group"]}}
        install.apply(settings, ROOT)
        self.assertIn("not-a-group", settings["hooks"]["Stop"])
        for command in expected("Stop"):
            self.assertIn(command, commands(settings, "Stop"))


if __name__ == "__main__":
    unittest.main()
