# cost_meter/ceilings.py
"""The two ceiling values in config.json, and the only writers of them.

A ceiling is the divisor summary.py turns spend into a percentage with. It
reaches config.json two ways -- derived from a reported percentage by
calibrate.py, declared outright by limit.py -- and this module exists so those
two cannot grow separate notions of the same key. There is one ceiling per
window: whichever tool set it, either tool clears it, through the one `clear`
below.

`refresh` is a parameter rather than an import. tally is a root-level module and
a package reaching upwards for it would invert the layering every other module
in cost_meter/ observes; injecting it also lets the tests drive `clear` without
a transcript scan.
"""

from . import paths, store

CEILINGS = {"5h window": "ceiling_5h_usd", "week": "ceiling_7d_usd"}

# Printed by both front ends, and neither names calibration: a ceiling that
# limit.py declared was never calibrated, so wording that named the derivation
# would be wrong for half the ways one can arrive.
REMOVED = "ceiling removed, back to dollars"
NOT_SET = "no ceiling was set, nothing to remove"


def clear_ceilings(keys):
    """Remove `keys` from config.json. Returns the ones that were really set.

    Read-modify-write under the lock, like every other writer of this file: the
    panel keeps its window position here too, and a wholesale rewrite would drop
    whichever value this side does not know about.

    Clearing something already clear is not an error. The flag names the state
    you want, not a transition you have to be mid-way through, which is what
    makes it safe to run twice.
    """
    with store.update_json_locked(paths.config_path(), paths.lock_path()) as config:
        return [key for key in keys if config.pop(key, None) is not None]


def set_ceilings(mapping):
    """Write `{config key: usd}` in one lock hold, preserving every other key."""
    with store.update_json_locked(paths.config_path(), paths.lock_path()) as config:
        config.update(mapping)


def clear(labelled_keys, refresh, report=print, warn=None):
    """Drop the named ceilings and refresh, so the panel redraws at once.

    Shared by both front ends deliberately. There is one ceiling per window, so
    `calibrate --clear-week` and `limit --clear-week` are the same operation and
    must not be able to drift into printing different things.

    Returns a process exit code.
    """
    warn = report if warn is None else warn
    try:
        removed = clear_ceilings([key for _, key in labelled_keys])
    except store.LockTimeout as exc:
        warn(f"could not clear the ceiling: {exc}")
        return 1
    for label, key in labelled_keys:
        report(f"{label}: {REMOVED if key in removed else NOT_SET}")

    # state.json still carries the percentages this run just invalidated, and the
    # panel redraws from the file monitor, so without this the rows would keep
    # showing them until the next assistant turn.
    try:
        with store.exclusive_lock(paths.lock_path()):
            refresh(session_id="")
    except store.LockTimeout as exc:
        warn(f"ceiling cleared, but the refresh could not run: {exc}")
        return 1
    return 0
