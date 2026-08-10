"""Incremental reader for Claude Code transcripts.

Only bytes appended since the previous run are read, which keeps a scan in the
kilobytes even though the transcript tree is well over a hundred megabytes.
"""

import json
from datetime import datetime

SYNTHETIC_MODEL = "<synthetic>"


def _epoch(timestamp):
    if not isinstance(timestamp, str) or not timestamp:
        return 0.0
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _event_from(entry):
    """Return an event record, or None if this line is not a priceable message."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict) or not usage:
        return None
    model = message.get("model")
    if not model or model == SYNTHETIC_MODEL:
        return None
    message_id = message.get("id")
    if not message_id:
        return None
    cache_creation = usage.get("cache_creation")
    if not isinstance(cache_creation, dict):
        cache_creation = {}
    return [
        _epoch(entry.get("timestamp")),
        message_id,
        entry.get("sessionId") or "",
        model,
        usage.get("input_tokens") or 0,
        usage.get("output_tokens") or 0,
        cache_creation.get("ephemeral_5m_input_tokens") or 0,
        cache_creation.get("ephemeral_1h_input_tokens") or 0,
        usage.get("cache_read_input_tokens") or 0,
    ]


def scan(root, offsets, known_ids):
    """Read new transcript bytes under root.

    Returns (events, new_offsets). new_offsets is a fresh dict; the caller's
    copy is never mutated, and keys for transcripts that no longer exist are
    dropped rather than carried forward forever. Ids in known_ids are skipped,
    as are duplicates within this scan.

    Files are read in binary and never consumed past the last newline, so a
    line another process is still writing stays unread until it is complete.
    """
    events = []
    new_offsets = {}
    seen = set(known_ids)

    for path in sorted(root.rglob("*.jsonl")):
        key = str(path)
        previous = offsets.get(key) or {}

        try:
            size = path.stat().st_size
        except OSError:
            if previous:
                new_offsets[key] = previous  # transient error, keep the offset
            continue

        start = previous.get("offset", 0)
        if size < start:
            start = 0  # file was truncated or rotated

        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                chunk = fh.read()
        except OSError:
            if previous:
                new_offsets[key] = previous  # transient error, keep the offset
            continue

        # Stop at the last complete line; a partial tail is left for next time.
        cut = chunk.rfind(b"\n") + 1
        end = start + cut

        for raw in chunk[:cut].split(b"\n"):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            event = _event_from(entry)
            if event is None or event[1] in seen:
                continue
            seen.add(event[1])
            events.append(event)

        new_offsets[key] = {"size": size, "offset": end}

    return events, new_offsets
