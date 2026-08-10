# cost_meter/store.py
"""Append-only event log, atomic JSON writes, and the cross-process lock."""

import json
import os
import time
from contextlib import contextmanager

if os.name == "nt":
    import msvcrt
else:
    import fcntl

PRUNE_DAYS = 8


class LockTimeout(Exception):
    """Raised when another run holds the lock longer than we are willing to wait."""


def _try_lock(handle):
    """Take the lock without blocking. Raises OSError when somebody holds it.

    Both platforms raise OSError on contention -- `flock(LOCK_NB)` and
    `LK_NBLCK` agree on that much -- which is what lets one retry loop serve
    them both.
    """
    if os.name == "nt":
        # msvcrt locks a byte range starting at the current file position, not
        # the whole file. Without the seek, a second acquisition would lock a
        # different byte and exclude nothing at all.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle):
    if os.name == "nt":
        handle.seek(0)  # release the same byte _try_lock took
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(path, timeout=10.0, poll=0.1):
    """Serialise concurrent tally runs from parallel Claude Code sessions.

    Bounded on purpose: a wedged holder must not queue every later hook
    invocation behind it forever. On timeout the caller skips this refresh and
    leaves the previous state in place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Opened "a+" rather than "w": Windows refuses to truncate a file another
    # process holds a byte-range lock on, so "w" would fail exactly when the
    # lock is doing its job. Nothing ever reads or writes the contents, and on
    # POSIX the two modes are indistinguishable here.
    handle = open(path, "a+", encoding="utf-8")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                _try_lock(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"another run held {path} for over {timeout}s")
                time.sleep(poll)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


@contextmanager
def update_json_locked(path, lock_path, timeout=10.0):
    """Read-modify-write a JSON file with the lock held across both halves.

    Several writers share config.json — the widget stores its position there and
    calibrate.py stores the ceilings. An unlocked read-modify-write silently
    drops whichever value the other side wrote in between.
    """
    with exclusive_lock(lock_path, timeout=timeout):
        data = read_json(path, default={}) or {}
        yield data
        write_json_atomic(path, data)


def append_events(path, events):
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")


def read_events(path):
    """Return every well-formed event. Corrupt lines are skipped, not fatal."""
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def prune_events(path, cutoff_epoch):
    """Drop events older than the cutoff. Returns how many were removed."""
    events = read_events(path)
    kept = [e for e in events if e[0] >= cutoff_epoch]
    removed = len(events) - len(kept)
    if removed:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for event in kept:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    return removed


def write_json_atomic(path, obj):
    """Write via a temp file and rename, so a reader never sees a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default
