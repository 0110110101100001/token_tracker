# cost_meter/billing.py
"""Whether this session is paid per token or against a seat.

The transcripts cannot answer this. `usage.service_tier` is the one field that
looks like it might, and it reads `standard` on every message either way: it
names the API's latency tier, not how the account is billed. So the answer is
read from the environment the session is running in, which is why the Stop hook
is what asks — a key exported for one session is visible in that process and
nowhere else, and asking from the panel instead would report the panel's
environment for every session on the machine.

Nothing here reads a token's value. The question is only ever whether a
credential is present and what plan it names.
"""

import json
import os

from . import paths

# A key in the environment is what Claude Code prefers over the login it has on
# disk, so these are checked first: a machine that is signed in perfectly well
# can still be spending real money per token.
API_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Not Anthropic's own billing, but still paid by the token rather than against a
# seat, which is the distinction this row exists to draw.
CLOUD_ENV_VARS = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")

# Boilerplate that every rate-limit tier carries and no reader needs on a panel.
TIER_NOISE = ("default", "claude")


def _read_json(path):
    """The parsed file, or None. A missing or broken one is simply not an answer.

    This runs inside the Stop hook, on the user's critical path: an unparseable
    settings file must cost the billing row and nothing else.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tidy_tier(tier):
    """`default_claude_max_5x` as `max 5x`, or None if nothing is left of it.

    None rather than an empty string: the label joins its parts with a
    separator, and an empty part would render as a stray one.
    """
    if not tier or not isinstance(tier, str):
        return None
    words = [word for word in tier.split("_") if word and word not in TIER_NOISE]
    return " ".join(words) or None


def _has_api_key(environ):
    if any((environ.get(name) or "").strip() for name in API_ENV_VARS):
        return True
    if any((environ.get(name) or "").strip() for name in CLOUD_ENV_VARS):
        return True
    settings = _read_json(paths.claude_settings_path())
    return bool(isinstance(settings, dict) and settings.get("apiKeyHelper"))


def _seat_label(oauth):
    """`team · max 5x` from a stored login, or None if it names neither.

    Both halves are optional because only one of them is worth much alone: the
    subscription says who is paying, the tier says how much of a share this seat
    has, and a login carrying neither is a seat whose plan we cannot name.
    """
    parts = [part for part in (oauth.get("subscriptionType"),
                               tidy_tier(oauth.get("rateLimitTier"))) if part]
    return " · ".join(parts) or None


def detect(environ=None):
    """How this process is being billed, as `{"mode": ..., "label": ...}`.

    `mode` is one of `api`, `seat` or `unknown`; `label` is what the panel puts
    on the row, and None when there is nothing to say. Unknown is a real answer
    and is reported as one — a guessed billing mode would be worse than a dash,
    because the whole point of the row is to tell you which of the panel's
    figures are money you owe.
    """
    environ = os.environ if environ is None else environ
    if _has_api_key(environ):
        return {"mode": "api", "label": "API"}

    credentials = _read_json(paths.credentials_path())
    oauth = (credentials or {}).get("claudeAiOauth")
    if isinstance(oauth, dict):
        return {"mode": "seat", "label": _seat_label(oauth)}

    return {"mode": "unknown", "label": None}
