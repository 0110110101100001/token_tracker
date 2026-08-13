#!/usr/bin/env python3
"""Stop-hook entry point: refresh the cost meter after an assistant turn.

This runs on the user's critical path, so every failure is swallowed and
logged. A broken tally costs a number on screen, never the ability to work.
"""

import json
import sys
import time
import traceback

from cost_meter import billing, paths, store, utilization
from cost_meter.log import write as _log
from cost_meter.parser import scan
from cost_meter.pricing import load_pricing
from cost_meter.store import PRUNE_DAYS
from cost_meter.summary import build_state, new_turn_ids, prune_marks


def _safe_log(message):
    """Belt-and-braces wrapper: logging itself must never be allowed to
    escape, whether _log raises or building `message` raised before this
    was even called."""
    try:
        _log(message)
    except Exception:
        pass


def _session_id_from_stdin():
    try:
        if sys.stdin.isatty():
            return ""
        payload = json.load(sys.stdin)
        return payload.get("session_id") or ""
    except (json.JSONDecodeError, ValueError, OSError):
        return ""


def refresh(session_id, now=None):
    """Do one incremental pass. Returns the state dict that was written.

    The caller must hold the lock from `paths.lock_path()`: this reads, appends
    to and prunes events.jsonl, and rewrites offsets.json and session_marks.json,
    none of it individually atomic.
    """
    now = time.time() if now is None else now

    events_path = paths.events_path()
    existing = store.read_events(events_path)
    known_ids = {e[1] for e in existing}
    offsets = store.read_json(paths.offsets_path(), default={}) or {}

    fresh, new_offsets = scan(paths.transcripts_root(), offsets, known_ids)
    store.append_events(events_path, fresh)
    store.write_json_atomic(paths.offsets_path(), new_offsets)

    store.prune_events(events_path, now - PRUNE_DAYS * 86400)

    pricing = load_pricing(paths.pricing_path())
    events = store.read_events(events_path)

    # last_turn is scoped to this session's own bookmark rather than to what this
    # run happened to append: with parallel sessions the first hook to fire picks
    # up everybody's new messages, so "appended by me" reads 0.00 for the rest.
    marks_path = paths.session_marks_path()
    marks = store.read_json(marks_path, default={}) or {}
    turn_ids, mark = new_turn_ids(events, session_id, marks.get(session_id))

    # Read here rather than inside build_state, which turns events into figures
    # and has no business reading files outside the ledger -- the same split
    # billing.detect() gets below.
    state = build_state(events, pricing, session_id, turn_ids, now,
                        utilization.read(now))
    # Added here rather than inside build_state, which turns events into figures
    # and has no business reading the environment. This is also the only place
    # that *can* read it: the hook runs inside the session it is reporting on, so
    # a key exported for that session alone is visible in this process and no
    # other.
    state["billing"] = billing.detect()
    store.write_json_atomic(paths.state_path(), state)

    # Advance the bookmark only after the state it describes is on disk, so a
    # crash in between costs a repeated last_turn rather than a lost one.
    if session_id and mark:
        marks[session_id] = mark
        store.write_json_atomic(
            marks_path, prune_marks(marks, now - PRUNE_DAYS * 86400)
        )
    return state


def main():
    try:
        session_id = _session_id_from_stdin()
        with store.exclusive_lock(paths.lock_path()):
            refresh(session_id)
    except store.LockTimeout as exc:
        try:
            _safe_log(f"skipped: {exc}")
        except Exception:
            pass
    except BaseException:
        try:
            _safe_log("tally failed:\n" + traceback.format_exc())
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
