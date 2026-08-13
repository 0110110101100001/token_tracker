# cost_meter/utilization.py
"""The account's limit percentages, as Claude Code last cached them.

Claude Code asks the server how much of each limit the *account* has used, and
caches the answer in ~/.claude.json under `cachedUsageUtilization`. Reading that
file is the whole of this module, and it is the only way the panel can know these
figures: a limit belongs to the account, while a transcript only ever records
what happened on this machine. No calibration can bridge that -- it would divide
one machine's dollars by the whole account's ceiling, which is wrong by however
much work happens elsewhere.

Nothing here reaches the network. The cache is a side effect of Claude Code
running, so on a machine in use it is present and recent, and on one that is not
it is absent or old. Both of those are answered with None rather than with a
figure presented as current.
"""

import time

from . import paths, store

# A sanity cap, not a freshness rule. Claude Code re-asks the server when a
# session starts and when the user runs /usage, and at no other time -- measured:
# a four-hour gap across continuous work, then an immediate refresh on /usage. So
# an hours-old figure is the normal case, and it is still worth showing: usage
# within a window only grows, which makes a stale percentage a floor rather than a
# guess. Past a week the weekly window has certainly reset and no bound survives.
#
# Claude Code's own one-hour threshold is deliberately not used here. It discards
# a figure it can re-fetch on demand; this panel cannot ask for one.
MAX_AGE_SECONDS = 7 * 86400.0

# The two limits the panel draws, named as the server names them.
SESSION = "session"
WEEKLY = "weekly_all"

# The kinds worth carrying into state.json. `weekly_scoped` -- a weekly cap on
# one model -- has no row, and is parsed anyway so that adding one later is a
# widget change and nothing more.
KINDS = (SESSION, WEEKLY, "weekly_scoped")

# The older shape, mapped onto the same kind names.
LEGACY_KEYS = {"five_hour": SESSION, "seven_day": WEEKLY}


def _whole(value):
    """`value` if it is a whole number of percent, else None.

    `bool` is rejected explicitly because it passes `isinstance(x, int)`, and
    `True` would otherwise read as 1 %.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _scope_name(scope):
    """`Fable` out of the server's nested scope object, or None."""
    model = (scope or {}).get("model") or {}
    return model.get("display_name") or None


def _row(pct, severity, resets_at, scope=None):
    return {"pct": pct, "severity": severity, "resets_at": resets_at,
            "scope": scope}


def _from_limits(limits):
    """Rows out of the `limits` array -- the shape that carries severity.

    Entries this module does not understand are skipped rather than guessed at.
    A percentage is the one field that cannot be missing: everything else on the
    row qualifies it, and a row without one has nothing to qualify.
    """
    rows = {}
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        pct = _whole(entry.get("percent"))
        if kind not in KINDS or pct is None:
            continue
        rows[kind] = _row(pct, entry.get("severity"), entry.get("resets_at"),
                          _scope_name(entry.get("scope")))
    return rows


def _from_legacy(utilization):
    """Rows out of the older `five_hour` / `seven_day` objects.

    They carry the same percentages and reset times but no severity, so a row
    built here leaves the panel to colour from the percentage instead. Kept as a
    fallback because `limits` is the newer of the two shapes: if a Claude Code
    version stops sending it, the panel should lose the colour source rather than
    the figures.
    """
    rows = {}
    for key, kind in LEGACY_KEYS.items():
        entry = utilization.get(key)
        if not isinstance(entry, dict):
            continue
        pct = _whole(entry.get("utilization"))
        if pct is None:
            continue
        rows[kind] = _row(pct, None, entry.get("resets_at"))
    return rows


def read(now=None):
    """The account's limit rows and the cache's age, or None.

    None covers every way the answer can be untrustworthy: no file, an unreadable
    one, a cache belonging to a different account, one past MAX_AGE_SECONDS, or
    one carrying no percentage this module understands. The caller shows dollars
    in all of those cases, which is what these rows showed before any of this
    existed.

    An hours-old figure is *not* one of those cases -- it is the normal one, and
    the caller states it as a floor. Whether a particular row still describes a
    live window is decided from its `resets_at` at the moment it is drawn, not
    here: a five-hour block can turn over while nothing is writing state.json.

    Returns `{"age_s": float, "rows": {kind: row}}`. The age travels with the
    rows because the panel has to be able to say how old a figure is.
    """
    now = time.time() if now is None else now
    data = store.read_json(paths.claude_config_path(), default=None)
    if not isinstance(data, dict):
        return None

    cache = data.get("cachedUsageUtilization")
    if not isinstance(cache, dict):
        return None

    # A cache left behind by a previous login describes somebody else's account.
    # Claude Code makes this same comparison and drops the cache on a mismatch.
    account = (data.get("oauthAccount") or {}).get("accountUuid")
    if not account or cache.get("accountUuid") != account:
        return None

    fetched_ms = cache.get("fetchedAtMs")
    if isinstance(fetched_ms, bool) or not isinstance(fetched_ms, (int, float)):
        return None
    # A writer whose clock runs ahead of ours produces a negative age, which
    # would otherwise read as fresh forever. summary.staleness clamps the same
    # case for the same reason.
    age = max(now - fetched_ms / 1000.0, 0.0)
    if age > MAX_AGE_SECONDS:
        return None

    utilization = cache.get("utilization")
    if not isinstance(utilization, dict):
        return None
    limits = utilization.get("limits")
    rows = _from_limits(limits) if isinstance(limits, list) else {}
    if not rows:
        rows = _from_legacy(utilization)
    if SESSION not in rows and WEEKLY not in rows:
        return None

    return {"age_s": round(age, 1), "rows": rows}
