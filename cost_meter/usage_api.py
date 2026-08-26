# cost_meter/usage_api.py
"""Asking the server for the account's limit percentages ourselves.

This is the only place in the project that opens a socket, and it exists because
the figures move all day while the local copy of them does not: Claude Code asks
for them when a session starts and when the user runs `/usage`, so between those
the panel had nothing newer to show -- measured, a four-hour gap across
continuous work.

`GET /api/oauth/usage` is the request Claude Code itself makes (its bundle names
it `fetchUtilization`), with the subscription token already on disk, and the body
it answers with is the same shape it caches. So the answer goes straight into
`data/usage.json` in that same shape and cost_meter/utilization.py parses both
files with one parser.

The endpoint is undocumented, which is why every failure here ends in the
behaviour the panel had before this module existed: `None`, nothing written, and
rows that go back to being however old Claude Code's cache is. A panel must not
die of a network error, and it must not report one either -- there is nothing the
reader could do about it.

Nothing here writes `.credentials.json` and nothing logs a token. The refresh
token beside the access token is deliberately never used: the refresh endpoint may
rotate it, and rotating it behind Claude Code's back could log the user out of the
very tool this panel measures. An expired token means we skip the request and
leave the cache to answer.

That last sentence is also this module's known hole. `.credentials.json` is
written by Claude Code in a terminal and by nothing else: the Claude Desktop app
runs the same Claude Code with a token of its own from
`~/.config/Claude/config.json`, and strips it back out of the environment its
hooks run in, so a panel launched from a desktop session can neither read that
file's replacement nor inherit one. On a machine used only through the desktop
app the token therefore expires -- about eight hours -- or was never written at
all, and every call here returns `None` from then on. That is silent by
construction: the check happens before any request, so there is no exception,
nothing is logged, and the caller cannot tell "fetch working" from "fetch never
attempted". The rows fall back to Claude Code's own cache, which moves on a
session start and a `/usage`, which is the behaviour this module was written to
replace. Fixing it needs a token this process can actually obtain; documented in
docs/METERING.md rather than worked around here.
"""

import json
import time
import urllib.request

from . import log, paths, store

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"

# Long enough for a slow link, short enough that a hung connection cannot keep a
# worker thread and the backoff waiting on it for minutes.
TIMEOUT_SECONDS = 10.0

# The ceiling on the failure backoff. Ten minutes of quiet is the most an
# unreachable endpoint costs in freshness, and at that rate an endpoint that has
# moved for good writes six log lines an hour rather than seven hundred.
MAX_BACKOFF_SECONDS = 600.0

# A cap on what a `Retry-After` can talk us into. The header is the server's
# instruction and is obeyed past MAX_BACKOFF_SECONDS -- asking sooner than we were
# told to would only earn another refusal -- but an hour is as long as a panel may
# go quiet on the strength of one header.
MAX_RETRY_AFTER_SECONDS = 3600.0

# Named honestly rather than impersonating Claude Code: this is a different
# program asking the same question with the same credential.
HEADERS = {"Accept": "application/json", "User-Agent": "cost-meter"}


def _get(url, headers, timeout):
    """One HTTP GET, as bytes. The seam every test replaces.

    Kept to exactly this -- no retry, no redirect handling, no parsing -- so that
    what the tests do not exercise is a five-line adapter rather than a policy.
    """
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _token(now):
    """The subscription access token, or None when there is none worth spending.

    None covers three cases that are all normal rather than faults: no credentials
    file, a login that is not a subscription (an API-key machine has no
    `claudeAiOauth`), and a token whose `expiresAt` has passed. The last is
    checked here rather than discovered as a 401 because the request is certain to
    fail.

    It was also written on the assumption that Claude Code would have replaced the
    token by the time this next ran, and that assumption holds only for a terminal.
    Under the Claude Desktop app nothing rewrites this file, so an expiry here is
    permanent rather than momentary -- see the note in the module docstring.

    Read on every call rather than held: Claude Code rewrites this file whenever
    it refreshes the token, so a cached copy would go stale once and then fail for
    as long as the panel stayed up.
    """
    credentials = store.read_json(paths.credentials_path(), default=None)
    if not isinstance(credentials, dict):
        return None
    oauth = credentials.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    expires_ms = oauth.get("expiresAt")
    if (not isinstance(expires_ms, bool)
            and isinstance(expires_ms, (int, float))
            and now >= expires_ms / 1000.0):
        return None
    return token


def _account():
    """Whose account this machine is logged into, or None.

    From `~/.claude.json`, which is the one file that names it. It is stamped onto
    what we write because utilization.read() compares it back: a `usage.json` left
    behind by a previous login describes somebody else's usage, and without the
    stamp there would be nothing to catch that.
    """
    config = store.read_json(paths.claude_config_path(), default=None)
    if not isinstance(config, dict):
        return None
    return (config.get("oauthAccount") or {}).get("accountUuid")


def _retry_after(exc):
    """Seconds out of a refusal's `Retry-After`, or None.

    This is how the endpoint's own pace stops being a guess. Measured on
    2026-08-14: a five-second poll earned `429 Retry-After: 196`, so the server
    states plainly how long it wants to be left alone, and there is no reason for
    the panel to decide that for itself.

    The header may legally carry an HTTP date instead of a number of seconds.
    Parsing that is not attempted: falling back to the caller's own backoff is a
    safe reading of a header we do not understand, while a guessed number is not.
    """
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        seconds = float(headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None
    if seconds <= 0.0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def fetch(now=None, get=None):
    """The account's `utilization` block, as `(block, retry_after)`.

    Both halves can be None: the block when there is no answer, `retry_after` when
    the failure did not name a wait. A refusal that does name one is the endpoint
    telling us our poll is too fast, which is worth more than anything the caller
    could infer, so it travels back with the failure rather than being logged and
    dropped.

    `get` is the seam: the default reaches the network, and tests hand in their
    own. `now` decides only whether the token has expired.
    """
    now = time.time() if now is None else now
    token = _token(now)
    if token is None:
        return None, None

    get = _get if get is None else get
    headers = dict(HEADERS, Authorization="Bearer %s" % token)
    try:
        payload = json.loads(get(ENDPOINT, headers, TIMEOUT_SECONDS))
    except Exception as exc:  # noqa: BLE001
        # Deliberately everything: an HTTP status, a DNS failure, a timeout, a
        # TLS error and a malformed body are one case here -- there is no figure,
        # and the caller shows the older one. The exception is logged and not the
        # response, which is what keeps the token out of the log.
        log.write(f"usage fetch failed: {type(exc).__name__}: {exc}")
        return None, _retry_after(exc)
    # A JSON array or a bare string parses perfectly well and is not an answer.
    if not isinstance(payload, dict):
        return None, None
    return payload, None


def refresh(now=None, get=None):
    """Ask, and write the answer to `data/usage.json`.

    Returns `(written, retry_after)`: whether there is a new figure on disk, and
    any wait the server asked for on the way to finding out.

    The account is looked up before the request rather than after: a figure we
    cannot stamp with an account is one utilization.read() would refuse anyway, so
    fetching it would spend a request on nothing.

    A failure writes nothing at all, leaving the previous answer in place. That is
    the right fallback rather than a gap, because a percentage from a window that
    is still open remains a floor -- and the reader's own age check is what
    eventually retires it.
    """
    now = time.time() if now is None else now
    account = _account()
    if not account:
        return False, None
    payload, retry_after = fetch(now=now, get=get)
    if payload is None:
        return False, retry_after
    store.write_json_atomic(paths.usage_path(), {
        "accountUuid": account,
        "fetchedAtMs": now * 1000.0,
        "utilization": payload,
    })
    return True, None


def backoff_seconds(previous, interval):
    """How long to wait after a failure: double the last wait, from `interval`.

    A pure function so the panel's retry policy is testable without a clock and
    without a socket. The first failure waits one poll interval, which is what the
    caller would have waited anyway; each one after it doubles, up to
    MAX_BACKOFF_SECONDS.
    """
    if previous <= 0.0:
        return min(interval, MAX_BACKOFF_SECONDS)
    return min(previous * 2.0, MAX_BACKOFF_SECONDS)
