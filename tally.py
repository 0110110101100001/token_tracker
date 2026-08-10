#!/usr/bin/env python3
"""Stop-hook entry point: refresh the cost meter after an assistant turn.

This runs on the user's critical path, so every failure is swallowed and
logged. A broken tally costs a number on screen, never the ability to work.
"""

import json
import sys
import time
import traceback

from cost_meter import paths, store
from cost_meter.parser import scan
from cost_meter.pricing import load_pricing
from cost_meter.store import PRUNE_DAYS
from cost_meter.summary import build_state


def _log(message):
    try:
        path = paths.log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
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
    """Do one incremental pass. Returns the state dict that was written."""
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
    calibration = store.read_json(paths.config_path(), default={}) or {}
    state = build_state(
        store.read_events(events_path),
        pricing,
        session_id,
        {e[1] for e in fresh},
        now,
        calibration,
    )
    store.write_json_atomic(paths.state_path(), state)
    return state


def main():
    try:
        session_id = _session_id_from_stdin()
        with store.exclusive_lock(paths.lock_path()):
            refresh(session_id)
    except Exception:
        _log("tally failed:\n" + traceback.format_exc())
    return 0


if __name__ == "__main__":
    sys.exit(main())
