# cost_meter/summary.py
"""Turn a list of priced events into the handful of numbers on screen."""

from datetime import datetime, timezone

from .pricing import UnknownModel, price_event

WINDOW_5H_SECONDS = 5 * 3600
WINDOW_7D_SECONDS = 7 * 86400

# How old state.json may get before the widget stops presenting its numbers as
# current. The Stop hook fires after every assistant turn, so on a machine that
# is being used at all a gap this long means the hook is broken, not idle: ten
# minutes is longer than any single turn observed here (the slowest full refresh
# is well under a second) yet short enough that a wedged hook is caught within
# one coffee break. An idle session does go stale, and that is honest — the
# numbers really are ten minutes old.
STALE_AFTER_SECONDS = 600


def _local_midnight(now_epoch):
    local = datetime.fromtimestamp(now_epoch)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _pct(usd, ceiling):
    if not ceiling:
        return None
    return round(100.0 * usd / ceiling)


def new_turn_ids(events, session_id, mark):
    """Pick out the events of `session_id` that arrived since its previous run.

    "New" has to mean per-session, not per-run: several Claude Code sessions run
    at once here, and `parser.scan` dedups against the whole of events.jsonl, so
    whichever hook fires first absorbs *every* session's new messages. A run
    that defines last_turn as "what I appended" therefore reads 0.00 for every
    session but the luckiest one.

    `mark` is the bookmark the previous run left for this session:
    `{"ts": <newest event timestamp counted>, "ids": [ids sharing that ts]}`.
    A timestamp bookmark survives pruning and does not care which run appended
    the events, which a line offset or a running count would not. The tie list
    is what makes it exact — transcripts do carry several messages on the same
    timestamp, so `ts > mark` alone would silently drop one.

    Returns `(ids, updated_mark)`. With no bookmark yet, every event of the
    session counts, which is what a session's first run always showed.
    """
    if not session_id:
        return set(), None

    mine = [(event[0], event[1]) for event in events if event[2] == session_id]
    if not mine:
        return set(), mark

    mark = mark or {}
    mark_ts = mark.get("ts")
    mark_ids = set(mark.get("ids") or ())
    if mark_ts is None:
        ids = {message_id for _, message_id in mine}
    else:
        ids = {
            message_id
            for ts, message_id in mine
            if ts > mark_ts or (ts == mark_ts and message_id not in mark_ids)
        }

    newest = max(ts for ts, _ in mine)
    if mark_ts is not None and newest < mark_ts:
        # Everything we had counted was pruned away; don't rewind the bookmark,
        # or the events still present would be counted a second time.
        return ids, mark
    updated = {
        "ts": newest,
        "ids": sorted(message_id for ts, message_id in mine if ts == newest),
    }
    return ids, updated


def prune_marks(marks, cutoff_epoch):
    """Drop bookmarks for sessions whose newest event is older than the cutoff.

    Without this the file grows one key per session ever seen — the same leak
    offsets.json had.
    """
    return {
        session_id: mark
        for session_id, mark in marks.items()
        if isinstance(mark, dict) and (mark.get("ts") or 0) >= cutoff_epoch
    }


def parse_updated_at(value):
    """Epoch seconds for state.json's `updated_at`, or None if it is unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def staleness(state, now_epoch, threshold=STALE_AFTER_SECONDS):
    """Decide whether a state dict may still be presented as current.

    Returns `(stale, age_seconds)`. `age_seconds` is None when `updated_at` is
    missing or unreadable, and that counts as stale: an age we cannot establish
    is exactly the invisible gap the design forbids, so it gets shown rather
    than assumed fresh. A negative age (a writer whose clock runs ahead) is
    clamped to zero instead of reading as fresh-forever.
    """
    written = parse_updated_at((state or {}).get("updated_at"))
    if written is None:
        return True, None
    age = now_epoch - written
    if age < 0.0:
        age = 0.0
    return age > threshold, age


def format_age(seconds):
    """Round age down to the coarsest unit that still says something useful."""
    if seconds is None:
        return "age unknown"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def build_state(events, pricing, session_id, new_ids, now_epoch, calibration):
    midnight = _local_midnight(now_epoch)
    unknown = set()

    session_usd = today_usd = usd_5h = usd_7d = last_turn_usd = 0.0

    for event in events:
        ts, message_id, event_session, model = event[0], event[1], event[2], event[3]
        try:
            usd = price_event(pricing, model, *event[4:])
        except UnknownModel:
            unknown.add(model)
            continue

        if event_session == session_id:
            session_usd += usd
            if message_id in new_ids:
                last_turn_usd += usd
        if ts >= midnight:
            today_usd += usd
        if ts >= now_epoch - WINDOW_5H_SECONDS:
            usd_5h += usd
        if ts >= now_epoch - WINDOW_7D_SECONDS:
            usd_7d += usd

    return {
        "updated_at": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(),
        "last_turn_usd": round(last_turn_usd, 4),
        "session": {"id": session_id, "usd": round(session_usd, 4)},
        "today_usd": round(today_usd, 4),
        "window_5h": {
            "usd": round(usd_5h, 4),
            "pct": _pct(usd_5h, calibration.get("ceiling_5h_usd")),
        },
        "window_7d": {
            "usd": round(usd_7d, 4),
            "pct": _pct(usd_7d, calibration.get("ceiling_7d_usd")),
        },
        "unknown_models": sorted(unknown),
    }
