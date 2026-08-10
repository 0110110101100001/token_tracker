# cost_meter/store.py
"""Append-only event log, atomic JSON writes, and the cross-process lock."""

import fcntl
import json
import os
import time
from contextlib import contextmanager

PRUNE_DAYS = 8


class LockTimeout(Exception):
    """Raised when another run holds the lock longer than we are willing to wait."""


@contextmanager
def exclusive_lock(path, timeout=10.0, poll=0.1):
    """Serialise concurrent tally runs from parallel Claude Code sessions.

    Bounded on purpose: a wedged holder must not queue every later hook
    invocation behind it forever. On timeout the caller skips this refresh and
    leaves the previous state in place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w", encoding="utf-8")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"another run held {path} for over {timeout}s")
                time.sleep(poll)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


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
