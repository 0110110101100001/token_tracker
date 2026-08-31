# cost_meter/utilization.py
"""The account's limit percentages, from whichever source read them last.

A limit belongs to the account, while a transcript only ever records what
happened on this machine, so these figures cannot be derived locally at all. No
calibration can bridge that -- it would divide one machine's dollars by the whole
account's ceiling, which is wrong by however much work happens elsewhere.

They arrive from the server, and two files carry them in the same shape:

- `~/.claude.json`, under `cachedUsageUtilization`, which Claude Code writes when
  it asks the server -- a session starting, or a `/usage`, and nothing else.
- `data/usage.json`, which cost_meter/usage_api.py writes by asking the same
  endpoint ourselves, on the panel's own few-second poll.

Nothing *here* reaches the network; this module only parses and chooses. The
choice is by fetch time rather than by source, because either one can be the
newer. Every way an answer can be untrustworthy -- a missing or unreadable file,
another account's, one past the sanity cap, one carrying no percentage this
module understands -- is answered with None rather than with a figure presented
as current.
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

# The three limits the panel draws, named as the server names them.
SESSION = "session"
WEEKLY = "weekly_all"
# A weekly cap on one model. Which model is not fixed -- it arrives in the
# entry's `scope`, and the row names itself from it.
SCOPED = "weekly_scoped"

# The kinds worth carrying into state.json.
KINDS = (SESSION, WEEKLY, SCOPED)

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


def _parse(cache, account, now):
    """Rows, age and fetch time out of one cache object, or None.

    Applied identically to whichever file the object came from -- Claude Code's
    `cachedUsageUtilization` or the one cost_meter/usage_api.py writes, which is
    deliberately the same shape. Every check here is for a way the object can be
    untrustworthy rather than merely old:

    - A cache left behind by a previous login describes somebody else's account.
      Claude Code makes this same comparison and drops the cache on a mismatch.
    - `fetchedAtMs` has to be a number, because everything else depends on
      knowing when the figure was true.
    - Past MAX_AGE_SECONDS no bound survives; see the note on that constant.
    - A shape carrying no percentage this module understands has nothing to say.

    An hours-old figure is *not* one of those cases -- it is the normal one for
    Claude Code's cache, and the caller states it as a floor.
    """
    if not isinstance(cache, dict):
        return None
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

    return {"age_s": round(age, 1), "rows": rows, "fetched_ms": fetched_ms}


def read(now=None):
    """The account's limit rows and their age, from whichever source is newer.

    Two sources answer the same question in the same shape, so this picks between
    them rather than preferring one: `data/usage.json`, which the panel fills by
    asking the server itself every few seconds, and Claude Code's
    `cachedUsageUtilization`, which moves when a session starts or the user runs
    `/usage`. Newer wins on `fetchedAtMs` -- with the poll turned off, or after a
    suspend, Claude Code's cache is genuinely the fresher of the two, and a
    fixed preference would show the older figure in that case.

    The account uuid both are checked against comes from `~/.claude.json`, which
    is the only file that names who is logged in. An unreadable one therefore
    withdraws both sources, which is the same answer this returned before there
    was a second source at all.

    None means there is nothing trustworthy to show, and the caller falls back to
    dollars -- what these rows showed before any of this existed. Whether a row
    still describes a live window is decided from its `resets_at` when it is
    drawn, not here: a five-hour block can turn over while nothing is writing.

    Returns `{"age_s": float, "rows": {kind: row}}`. The age travels with the
    rows because the panel has to be able to say how old a figure is.
    """
    now = time.time() if now is None else now
    config = store.read_json(paths.claude_config_path(), default=None)
    if not isinstance(config, dict):
        config = {}
    account = (config.get("oauthAccount") or {}).get("accountUuid")

    answers = [_parse(cache, account, now) for cache in
               (config.get("cachedUsageUtilization"),
                store.read_json(paths.usage_path(), default=None))]
    answers = [answer for answer in answers if answer]
    if not answers:
        return None

    newest = max(answers, key=lambda answer: answer["fetched_ms"])
    return {"age_s": newest["age_s"], "rows": newest["rows"]}
