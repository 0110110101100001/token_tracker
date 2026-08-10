# tests/test_install.py
import os
import tempfile
import unittest
from pathlib import Path

from cost_meter import install, paths

ROOT = Path("/opt/cost-meter")


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


class PidPathTest(unittest.TestCase):
    def test_pid_path_follows_cost_meter_home(self):
        # The launcher hardcodes "${COST_METER_HOME:-data}/widget.pid" because it
        # must not pay for a Python start on the session critical path. That
        # duplication is only safe while this holds.
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["COST_METER_HOME"] = tmp
            try:
                self.assertEqual(paths.pid_path(), Path(tmp) / "widget.pid")
            finally:
                del os.environ["COST_METER_HOME"]

    def test_pid_path_defaults_under_data(self):
        # smoke.sh runs the whole suite with COST_METER_HOME pointed at a
        # throwaway directory, so the default is only observable with it unset.
        previous = os.environ.pop("COST_METER_HOME", None)
        try:
            self.assertEqual(paths.pid_path(),
                             paths.project_root() / "data" / "widget.pid")
        finally:
            if previous is not None:
                os.environ["COST_METER_HOME"] = previous


class WidgetPidTest(unittest.TestCase):
    """The pid file's lifecycle, which cannot be reached through the UI.

    Quitting the panel for real means right-clicking a menu, so the write on
    startup and the removal on exit are covered here instead. Importing widget
    only loads the GTK bindings; it opens no display, so this runs headless.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["COST_METER_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "COST_METER_HOME", None)

    def test_write_then_clear_round_trips(self):
        import widget
        widget.write_pid()
        self.assertEqual(paths.pid_path().read_text(encoding="utf-8").strip(),
                         str(os.getpid()))
        widget.clear_pid()
        self.assertFalse(paths.pid_path().exists())

    def test_clear_leaves_another_panels_claim_alone(self):
        # A panel killed with SIGKILL skips its own cleanup; if a replacement
        # started in the meantime, the corpse must not delete the survivor's file.
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


class AutostartOwnershipTest(unittest.TestCase):
    """Removal is scoped to the entry we wrote.

    Regression test with a real cause: an early version deleted the autostart
    file unconditionally, so uninstalling against a throwaway --settings copy
    reached out and removed the live one.
    """

    def entry(self, exec_path):
        tmp = Path(tempfile.mkdtemp()) / "claude-cost-meter.desktop"
        tmp.write_text(f"[Desktop Entry]\nType=Application\nExec={exec_path}\n",
                       encoding="utf-8")
        return tmp

    def test_our_own_entry_is_ours(self):
        path = self.entry(ROOT / "run_widget.sh")
        self.assertTrue(install._autostart_is_ours(path, ROOT))

    def test_another_checkout_is_not_ours(self):
        path = self.entry("/opt/cost-meter-other/run_widget.sh")
        self.assertFalse(install._autostart_is_ours(path, ROOT))

    def test_an_unrelated_entry_is_not_ours(self):
        path = self.entry("/usr/bin/nextcloud --background")
        self.assertFalse(install._autostart_is_ours(path, ROOT))

    def test_a_missing_file_is_not_ours(self):
        self.assertFalse(install._autostart_is_ours(Path("/nonexistent"), ROOT))

    def test_the_entry_we_write_is_recognised_as_ours(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["XDG_CONFIG_HOME"] = tmp
            try:
                path = install.write_autostart(ROOT)
                self.assertTrue(install._autostart_is_ours(path, ROOT))
            finally:
                del os.environ["XDG_CONFIG_HOME"]


class ApplyTest(unittest.TestCase):
    def test_registers_both_hooks(self):
        settings = {}
        install.apply(settings, ROOT)
        self.assertEqual(commands(settings, "Stop"),
                         [str(ROOT / "hooks" / "tally.sh")])
        self.assertEqual(commands(settings, "SessionStart"),
                         [str(ROOT / "launch_widget.sh")])

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
        settings = settings_with("/home/me/.claude/hooks/run-smoke-tests.sh",
                                 event="PostToolUse")
        settings["hooks"]["Stop"] = [
            {"hooks": [{"type": "command", "command": "/usr/local/bin/other"}]}
        ]
        install.apply(settings, ROOT)
        self.assertEqual(commands(settings, "PostToolUse"),
                         ["/home/me/.claude/hooks/run-smoke-tests.sh"])
        self.assertIn("/usr/local/bin/other", commands(settings, "Stop"))

    def test_replaces_a_legacy_entry_from_the_same_repo(self):
        # tally.py used to be registered directly, before the pixi wrapper. Two
        # live Stop hooks would both run, and the second would find no new events
        # and overwrite last_turn with 0.00 -- so the old entry must be replaced,
        # not accompanied.
        settings = settings_with(str(ROOT / "tally.py"))
        install.apply(settings, ROOT)
        self.assertEqual(commands(settings, "Stop"),
                         [str(ROOT / "hooks" / "tally.sh")])

    def test_leaves_another_checkout_alone(self):
        settings = settings_with("/opt/cost-meter-other/tally.py")
        install.apply(settings, ROOT)
        self.assertIn("/opt/cost-meter-other/tally.py", commands(settings, "Stop"))

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
        self.assertIn(str(ROOT / "hooks" / "tally.sh"), commands(settings, "Stop"))


if __name__ == "__main__":
    unittest.main()
