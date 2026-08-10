# cost_meter/summary.py
"""Turn a list of priced events into the handful of numbers on screen."""

from datetime import datetime, timezone

from .pricing import UnknownModel, price_event

WINDOW_5H_SECONDS = 5 * 3600
WINDOW_7D_SECONDS = 7 * 86400


def _local_midnight(now_epoch):
    local = datetime.fromtimestamp(now_epoch)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _pct(usd, ceiling):
    if not ceiling:
        return None
    return round(100.0 * usd / ceiling)


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
