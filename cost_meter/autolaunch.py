# cost_meter/autolaunch.py
"""Whether a new Claude Code session may bring the panel up.

The panel is a singleton -- `launch.panel_is_running` sees the lock a live one
holds -- so this is not about avoiding a second window. It is about a panel you
closed on purpose staying closed: without it, the next session starts one
straight back, and there is no way to be rid of the thing short of not starting
sessions.

Only the SessionStart launch is suspended. The Stop hook keeps recording, so a
paused fortnight leaves no hole in the ledger and the figures are whole again the
moment the panel comes back. A running panel is not touched either: pausing is a
statement about the next session, not this one.

Not to be confused with `install.py --autostart`, which writes the desktop entry
that starts the panel when you log in. That one is about the machine coming up;
this one is about a Claude session starting.

Usage (the bare -- keeps pixi from reading --off as one of its own flags):
    pixi run autolaunch -- --off      # sessions stop opening the panel
    pixi run autolaunch -- --on       # they open it again
    pixi run autolaunch -- --status   # which of the two is in force
"""

import argparse
import sys

from . import paths, store

KEY = "autolaunch_paused"


def paused():
    """True when a new session must not open the panel.

    Fails open: a missing or unreadable config reads as live, because the
    alternative is a panel that silently never appears again with the one file
    that could explain why being the one that cannot be read.
    """
    config = store.read_json(paths.config_path(), default={}) or {}
    return bool(config.get(KEY))


def set_paused(value):
    """Record the state asked for. Raises store.LockTimeout if the file is busy.

    Read-modify-write under the lock, like every other writer of this file: the
    ceilings and the panel's window position live here too, and a wholesale
    rewrite would drop whichever of them this side does not know about.

    Resuming removes the key rather than storing a false, so a resumed config is
    the same shape as one that was never paused -- one state, one representation.
    Setting the state you are already in is not an error, which is what makes
    this safe to call without reading first.
    """
    with store.update_json_locked(paths.config_path(), paths.lock_path()) as config:
        if value:
            config[KEY] = True
        else:
            config.pop(KEY, None)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    # Mutually exclusive and required: --off --on together has no defensible
    # meaning, and a bare run that did nothing would look like a pause that
    # worked.
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--off", action="store_true",
                       help="stop new sessions opening the panel; a running "
                            "one stays up and recording continues")
    group.add_argument("--on", action="store_true",
                       help="let new sessions open the panel again")
    group.add_argument("--status", action="store_true",
                       help="report which of the two is in force")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.status:
        print("auto-launch: paused" if paused() else "auto-launch: live")
        return 0

    try:
        set_paused(args.off)
    except store.LockTimeout as exc:
        # Printed only after the write lands, so a run can never report a state
        # it failed to persist.
        print(f"could not change auto-launch: {exc}", file=sys.stderr)
        return 1

    if args.off:
        print("auto-launch paused: new sessions will not open the panel. "
              "Recording continues, and any panel already up stays up.")
    else:
        print("auto-launch live: the next session will open the panel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
