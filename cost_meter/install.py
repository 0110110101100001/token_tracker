# cost_meter/install.py
"""Register (or remove) the two hooks this tool needs in Claude Code's settings.

Run through install.sh, or `pixi run install-hooks`.

The hooks have to be registered by absolute path, which is the one thing that
cannot be committed to the repo: it differs on every machine. This module
derives it from where the repo actually sits and edits ~/.claude/settings.json
in place.

Editing that file by hand is the part of the old install instructions most
likely to go wrong, in two ways this module is built to avoid: replacing the
whole `hooks` object and silently dropping unrelated hooks the user already
depends on, and leaving a stale entry behind after the repo moves, so two Stop
hooks race and the second one reports a `last turn` of zero.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import paths, store

# Every hook this tool owns: the settings event, the script, and the timeout.
# The Stop hook's budget is generous because it is bounded by real work (a full
# rescan of the transcripts is well under a second); SessionStart only has to
# check a pid and fork.
HOOKS = (
    ("Stop", Path("hooks") / "tally.sh", 20),
    ("SessionStart", Path("launch_widget.sh"), 10),
)

AUTOSTART_NAME = "claude-cost-meter.desktop"


def settings_path():
    return Path.home() / ".claude" / "settings.json"


def autostart_path():
    config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config) if config else Path.home() / ".config"
    return base / "autostart" / AUTOSTART_NAME


def _owned_by_us(command, root):
    """True when `command` is one of our scripts, wherever the repo now lives.

    Deliberately broader than the exact commands we install: it also matches a
    previous layout's entry (tally.py was registered directly before the pixi
    wrapper existed) and a copy of the repo at a path we are moving away from.
    Those are the entries that must be replaced rather than added alongside.
    """
    if not isinstance(command, str):
        return False
    root = str(root)
    return command == root or command.startswith(root + os.sep)


def _strip_ours(settings, root, events):
    """Drop every hook of ours from `events`, leaving everyone else's alone.

    Returns how many were removed. A group the strip leaves empty is dropped,
    and so is an event key with no groups left, so uninstalling restores the
    file to the shape it had before we touched it.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0

    removed = 0
    for event in events:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept_groups.append(group)  # not a shape we understand; preserve it
                continue
            kept = [h for h in group["hooks"]
                    if not (isinstance(h, dict)
                            and _owned_by_us(h.get("command"), root))]
            removed += len(group["hooks"]) - len(kept)
            if not kept:
                continue  # the whole group was ours
            group["hooks"] = kept
            kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)

    if not hooks:
        settings.pop("hooks", None)
    return removed


def plan(root):
    """The commands we would register, in settings order."""
    return [(event, str(root / script), timeout) for event, script, timeout in HOOKS]


def apply(settings, root, uninstall=False):
    """Rewrite `settings` in place. Returns a list of human-readable changes."""
    events = [event for event, _, _ in HOOKS]
    removed = _strip_ours(settings, root, events)
    changes = []
    if removed:
        changes.append(f"removed {removed} existing entr"
                       f"{'y' if removed == 1 else 'ies'} pointing into {root}")
    if uninstall:
        if not removed:
            changes.append("nothing of ours was registered")
        return changes

    hooks = settings.setdefault("hooks", {})
    for event, command, timeout in plan(root):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SystemExit(
                f"{event} in settings.json is a {type(groups).__name__}, not a list; "
                "fix it by hand and re-run"
            )
        groups.append({"hooks": [{"type": "command",
                                  "command": command,
                                  "timeout": timeout}]})
        changes.append(f"registered {event} -> {command}")
    return changes


def _autostart_is_ours(path, root):
    """True when the autostart entry launches this checkout's run_widget.sh.

    Checked before removing it, for the same reason the hook entries are: the
    file lives outside the repo, is not in version control, and may predate this
    installer or point somewhere else entirely. Deleting an entry we did not
    write is not ours to do — and without this check, uninstalling against a
    throwaway `--settings` copy would still reach out and delete the real one.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in content.splitlines():
        if line.startswith("Exec="):
            return _owned_by_us(line[len("Exec="):].strip(), root)
    return False


def write_autostart(root):
    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # NoDisplay keeps it out of the session's application list; it is a panel,
    # not something to launch from a menu.
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Claude cost meter\n"
        "Comment=Always-on-top panel showing what Claude Code work costs\n"
        f"Exec={root / 'run_widget.sh'}\n"
        "NoDisplay=true\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--uninstall", action="store_true",
                        help="remove our hooks (and the autostart entry) again")
    parser.add_argument("--autostart", action="store_true",
                        help="also start the panel on login, not just on a "
                             "Claude Code session")
    parser.add_argument("--settings", metavar="PATH", default=None,
                        help="settings file to edit (default ~/.claude/settings.json)")
    args = parser.parse_args(argv)

    root = paths.project_root()
    target = Path(args.settings) if args.settings else settings_path()

    settings = store.read_json(target, default=None)
    if settings is None:
        if target.exists():
            raise SystemExit(f"{target} is not readable JSON; fix it by hand and re-run")
        settings = {}
    elif not isinstance(settings, dict):
        raise SystemExit(f"{target} does not contain a JSON object; refusing to edit it")
    else:
        # Only worth a backup when there was something to lose. Copied rather
        # than re-serialised so the original bytes survive verbatim.
        backup = target.with_suffix(target.suffix + ".bak")
        shutil.copyfile(target, backup)
        print(f"backed up {target} -> {backup}")

    changes = apply(settings, root, uninstall=args.uninstall)
    store.write_json_atomic(target, settings)

    for change in changes:
        print(change)

    if args.uninstall:
        path = autostart_path()
        if path.exists():
            if _autostart_is_ours(path, root):
                path.unlink()
                print(f"removed {path}")
            else:
                print(f"left {path} alone: it does not launch {root}")
        print(f"wrote {target}")
        print("The panel keeps running until you quit it: right click -> Quit.")
        return 0

    if args.autostart:
        print(f"wrote {write_autostart(root)}")

    print(f"wrote {target}")
    print("Start (or restart) a Claude Code session to pick the hooks up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
