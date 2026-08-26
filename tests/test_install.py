# tests/test_install.py
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cost_meter import install, launch, paths, store

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
    test one of them. Through _shell_command for the same reason: the spelling
    a hook is registered in is that function's business, and duplicating it
    here would let the two drift.
    """
    return [install._shell_command(ROOT / script)
            for name, script, _, _ in install.HOOKS if name == event]


def groups(settings, event):
    """Every hook group registered under `event`, in settings order."""
    return [g for g in settings.get("hooks", {}).get(event, [])
            if isinstance(g, dict)]


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

    def test_the_lock_a_panel_claims_is_the_one_the_launcher_checks(self):
        # The two halves are in different modules and would drift apart in
        # silence: the panel would come up, the launcher would never see it, and
        # every session would stack another one.
        import widget
        handle = widget.claim_liveness_lock()
        self.assertIsNotNone(handle)
        try:
            self.assertTrue(launch.panel_is_running())
        finally:
            store.release(handle)
        self.assertFalse(launch.panel_is_running())

    def test_a_second_panel_does_not_get_the_claim(self):
        import widget
        handle = widget.claim_liveness_lock()
        try:
            self.assertIsNone(widget.claim_liveness_lock())
        finally:
            store.release(handle)


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


class PanelLivenessTest(TempHome):
    """The check that decides whether to start a second panel.

    It is a lock rather than a pid probe because of a bug this covers: Windows
    reuses pid numbers, so any live process landing on the number a dead panel
    left in widget.pid used to suppress the launch — in that session and in
    every session after it, silently, because the hook exits 0 and says nothing.
    """

    def test_nothing_holding_it_means_no_panel(self):
        self.assertFalse(launch.panel_is_running())

    def test_a_held_lock_means_a_panel(self):
        handle = store.try_acquire(paths.widget_lock_path())
        self.assertIsNotNone(handle)
        try:
            self.assertTrue(launch.panel_is_running())
        finally:
            store.release(handle)
        # Released, so the next session may start one again.
        self.assertFalse(launch.panel_is_running())

    def test_the_check_does_not_keep_the_lock(self):
        # A launcher that held on to what it probed would lock out the panel it
        # is about to start.
        launch.panel_is_running()
        handle = store.try_acquire(paths.widget_lock_path())
        self.assertIsNotNone(handle)
        store.release(handle)

    def test_a_stale_pid_belonging_to_a_live_process_does_not_suppress(self):
        # The regression itself: our own pid stands in for whatever unrelated
        # process Windows handed the dead panel's number to.
        paths.pid_path().write_text(f"{os.getpid()}\n", encoding="utf-8")
        self.assertFalse(launch.panel_is_running())


def cgroup_of(pid):
    """The unified-hierarchy cgroup of `pid`, or None if it cannot be read."""
    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        hierarchy, _, path = line.partition("::")
        if hierarchy == "0":
            return path
    return None


@unittest.skipUnless(launch.systemd_available(),
                     "cgroup escape only applies to a systemd Linux session")
class SpawnEscapesTheCallersCgroupTest(unittest.TestCase):
    """The panel must not be left in the cgroup of whoever launched it.

    Regression test with a real cause. setsid escapes the process group and the
    session, which is all the launcher used to do, but not the cgroup — and
    terminal emulators put each tab in a transient scope with
    KillMode=control-group. So closing the tab that happened to win the launch
    race killed the panel with it, silently: the kill is a SIGTERM from systemd,
    which the panel cannot report on its way down however its output is
    redirected. The panel then came back at the next SessionStart, which from the
    outside looks like it vanishing and returning at random.
    """

    def test_the_child_lands_outside_our_cgroup(self):
        ours = cgroup_of(os.getpid())
        self.assertIsNotNone(ours, "cannot read our own cgroup")
        child = launch.spawn_detached(
            [sys.executable, "-c", "import time; time.sleep(30)"], Path.cwd())
        try:
            # Polled rather than read once: systemd-run execs the panel only
            # after the manager has moved it, so for the first few milliseconds
            # the pid is still systemd-run sitting in our own cgroup.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                theirs = cgroup_of(child.pid)
                if theirs is not None and theirs != ours:
                    return
                time.sleep(0.05)
            self.fail(f"child stayed in our cgroup ({ours}); a terminal tab "
                      f"closing would take the panel down with it")
        finally:
            child.kill()
            child.wait(timeout=10)


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


class ShellCommandTest(unittest.TestCase):
    """The spelling a hook is registered in, which is not free-form.

    Claude Code does not execute the registered command itself: it hands the
    string to `bash -c`, on Windows as well as Linux. Everything here is about
    surviving that shell, and the regression it covers is a real one -- both
    hooks were dead on Windows for exactly this reason, reporting

        /usr/bin/bash: line 1: C:UsersLenkaDesktop...launch_widget.cmd:
        command not found

    with a non-blocking exit 127. Nothing on screen said so: a hook error is
    non-blocking, and both wrappers exit 0 by design, so the only symptom was a
    panel that never appeared and numbers that stopped moving.
    """

    def test_a_registered_command_carries_no_backslashes(self):
        # The regression itself. Bash reads a backslash as an escape, so a
        # native Windows path arrives with every separator eaten.
        for _, command, _, _ in install.plan(ROOT):
            self.assertNotIn("\\", command)

    def test_a_registered_command_still_names_the_script(self):
        # Cheap guard against the above being satisfied by mangling the path.
        for (_, command, _, _), (_, script, _, _) in zip(install.plan(ROOT),
                                                         install.HOOKS):
            self.assertIn(script.name, command)
            self.assertIn((ROOT / script).as_posix(), command)

    def test_a_path_with_spaces_is_one_word_to_the_shell(self):
        # "C:/Program Files/..." or a "My Projects" directory: unquoted, the
        # shell would run the first half of the path with the rest as
        # arguments. Distinct from the backslash fault, same shell.
        root = (Path("C:/opt/cost meter") if install.IS_WINDOWS
                else Path("/opt/cost meter"))
        for _, command, _, _ in install.plan(root):
            self.assertTrue(command.startswith('"') and command.endswith('"'),
                            f"{command!r} is not quoted")
            self.assertNotIn('"', command[1:-1])


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

    def test_session_start_carries_a_matcher(self):
        # Registered without one, the group is not reliably run -- and the whole
        # symptom is a panel that never appears, from a hook that exits 0 and
        # writes nothing either way.
        settings = {}
        install.apply(settings, ROOT)
        self.assertEqual([g.get("matcher") for g in groups(settings, "SessionStart")],
                         ["startup|resume|clear|compact"])

    def test_stop_carries_no_matcher(self):
        # Stop has no notion of one; inventing a key here would be noise in
        # somebody else's settings file.
        settings = {}
        install.apply(settings, ROOT)
        for group in groups(settings, "Stop"):
            self.assertNotIn("matcher", group)

    def test_session_start_gets_the_longer_timeout(self):
        # A cold first run pays for pixi starting, an interpreter booting and a
        # virus scanner reading the environment. Killed on the timeout, the hook
        # dies before it ever reaches the spawn.
        settings = {}
        install.apply(settings, ROOT)
        timeouts = [h["timeout"] for g in groups(settings, "SessionStart")
                    for h in g["hooks"]]
        self.assertEqual(timeouts, [30])

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

    def test_replaces_the_entry_the_shell_could_not_run(self):
        # What every Windows install wrote before the fix, and what a Linux one
        # wrote unquoted: the same script, spelled the way the shell chokes on.
        # It has to be recognised as ours and replaced -- an installer that
        # merely added the working spelling beside it would leave two live Stop
        # hooks, and the second finds no new messages and overwrites `last
        # turn` with $0.00.
        settings = settings_with(str(ROOT / "hooks" / ("tally" + install._SUFFIX)))
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
