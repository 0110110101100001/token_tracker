# tests/test_usage_api.py
"""The one part of this tool that opens a socket, driven by a fake opener.

No test here reaches the network or the real credentials file: TempHome redirects
both ~/.claude/ and ~/.claude.json, and every fetch is handed a `get` of its own.
A test that used the real ones would pass or fail according to how this machine
happens to be logged in.
"""

import json
import urllib.error

from cost_meter import paths, store, usage_api, utilization
from tests.support import TempHome

NOW = 1_786_000_000.0
ACCOUNT = "acct-1"
RESET_5H = "2026-08-14T17:20:00.431594+00:00"

BODY = {
    "five_hour": {"utilization": 8.0, "resets_at": RESET_5H},
    "limits": [
        {"kind": "session", "percent": 8, "severity": "normal",
         "resets_at": RESET_5H, "scope": None, "is_active": False},
        {"kind": "weekly_all", "percent": 21, "severity": "warning",
         "resets_at": "2026-08-15T01:00:00+00:00", "scope": None,
         "is_active": True},
    ],
}


def rate_limited(retry_after="196"):
    """The 429 this endpoint answers a too-fast poll with.

    Measured on 2026-08-14: polling every five seconds earned `429` with
    `Retry-After: 196`, which is how the server's own pace became something the
    panel reads rather than guesses.
    """
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return urllib.error.HTTPError(usage_api.ENDPOINT, 429, "Too Many Requests",
                                  headers, None)


class Opener:
    """A stand-in for the HTTP get: records the call, answers with `body`."""

    def __init__(self, body=None, raises=None):
        self.body = body
        self.raises = raises
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return self.body


class UsageApiTest(TempHome):
    def write_credentials(self, expires_s=NOW + 3600.0, token="tok-abc",
                          oauth=True):
        payload = {}
        if oauth:
            payload["claudeAiOauth"] = {"accessToken": token,
                                        "expiresAt": expires_s * 1000.0}
        store.write_json_atomic(paths.credentials_path(), payload)

    def test_a_200_body_yields_the_utilization_block(self):
        self.write_credentials()
        opener = Opener(body=json.dumps(BODY).encode("utf-8"))
        self.assertEqual(usage_api.fetch(now=NOW, get=opener), (BODY, None))

    def test_a_429_carries_the_wait_the_server_asked_for(self):
        self.write_credentials()
        payload, retry_after = usage_api.fetch(now=NOW,
                                               get=Opener(raises=rate_limited()))
        self.assertIsNone(payload)
        self.assertEqual(retry_after, 196.0)

    def test_a_429_with_no_retry_after_leaves_the_wait_to_the_backoff(self):
        self.write_credentials()
        self.assertEqual(
            usage_api.fetch(now=NOW, get=Opener(raises=rate_limited(None))),
            (None, None))

    def test_an_unparseable_retry_after_is_ignored_rather_than_guessed_at(self):
        # The header may legally carry an HTTP date instead of seconds. Waiting
        # the backoff is a safe reading of one; inventing a number is not.
        self.write_credentials()
        self.assertEqual(
            usage_api.fetch(now=NOW,
                            get=Opener(raises=rate_limited("Wed, 21 Oct 2026 07:28:00 GMT"))),
            (None, None))

    def test_an_absurd_retry_after_is_capped(self):
        self.write_credentials()
        _, retry_after = usage_api.fetch(
            now=NOW, get=Opener(raises=rate_limited("999999")))
        self.assertEqual(retry_after, usage_api.MAX_RETRY_AFTER_SECONDS)

    def test_the_request_carries_the_token_as_a_bearer(self):
        self.write_credentials(token="tok-xyz")
        opener = Opener(body=json.dumps(BODY).encode("utf-8"))
        usage_api.fetch(now=NOW, get=opener)
        call = opener.calls[0]
        self.assertEqual(call["url"], usage_api.ENDPOINT)
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok-xyz")

    def test_a_missing_credentials_file_asks_for_nothing(self):
        opener = Opener(body=json.dumps(BODY).encode("utf-8"))
        self.assertEqual(usage_api.fetch(now=NOW, get=opener), (None, None))
        self.assertEqual(opener.calls, [])

    def test_a_login_that_is_not_a_subscription_asks_for_nothing(self):
        # An API-key machine has a credentials file with no claudeAiOauth in it.
        self.write_credentials(oauth=False)
        opener = Opener(body=json.dumps(BODY).encode("utf-8"))
        self.assertEqual(usage_api.fetch(now=NOW, get=opener), (None, None))
        self.assertEqual(opener.calls, [])

    def test_an_expired_token_is_not_spent_on_a_request(self):
        # It is certain to 401, and Claude Code will have written a new one by
        # the time it next runs.
        self.write_credentials(expires_s=NOW - 1.0)
        opener = Opener(body=json.dumps(BODY).encode("utf-8"))
        self.assertEqual(usage_api.fetch(now=NOW, get=opener), (None, None))
        self.assertEqual(opener.calls, [])

    def test_a_401_is_no_answer(self):
        self.write_credentials()
        opener = Opener(raises=urllib.error.HTTPError(
            usage_api.ENDPOINT, 401, "Unauthorized", {}, None))
        self.assertEqual(usage_api.fetch(now=NOW, get=opener), (None, None))

    def test_a_timeout_is_no_answer(self):
        self.write_credentials()
        self.assertEqual(usage_api.fetch(now=NOW, get=Opener(raises=TimeoutError())),
                         (None, None))

    def test_an_unparseable_body_is_no_answer(self):
        self.write_credentials()
        self.assertEqual(usage_api.fetch(now=NOW, get=Opener(body=b"{not json")),
                         (None, None))

    def test_a_body_that_is_not_an_object_is_no_answer(self):
        self.write_credentials()
        self.assertEqual(usage_api.fetch(now=NOW, get=Opener(body=b"[]")),
                         (None, None))

    def test_refresh_writes_the_answer_in_the_shape_the_reader_expects(self):
        self.write_credentials()
        self.write_claude_config({"oauthAccount": {"accountUuid": ACCOUNT}})
        opener = Opener(body=json.dumps(BODY).encode("utf-8"))
        self.assertEqual(usage_api.refresh(now=NOW, get=opener), (True, None))
        written = store.read_json(paths.usage_path())
        self.assertEqual(written["accountUuid"], ACCOUNT)
        self.assertEqual(written["fetchedAtMs"], NOW * 1000.0)
        self.assertEqual(written["utilization"], BODY)

    def test_what_refresh_writes_is_what_the_panel_then_reads(self):
        self.write_credentials()
        self.write_claude_config({"oauthAccount": {"accountUuid": ACCOUNT}})
        usage_api.refresh(now=NOW, get=Opener(body=json.dumps(BODY).encode("utf-8")))
        rows = utilization.read(now=NOW)["rows"]
        self.assertEqual(rows[utilization.SESSION]["pct"], 8)
        self.assertEqual(rows[utilization.WEEKLY]["severity"], "warning")

    def test_a_failed_fetch_writes_nothing(self):
        # The previous answer is better than no answer: it is a floor, and the
        # reader's age check is what retires it.
        self.write_credentials()
        self.write_claude_config({"oauthAccount": {"accountUuid": ACCOUNT}})
        self.assertEqual(usage_api.refresh(now=NOW, get=Opener(raises=TimeoutError())),
                         (False, None))
        self.assertIsNone(store.read_json(paths.usage_path()))

    def test_refresh_hands_on_the_wait_the_server_asked_for(self):
        self.write_credentials()
        self.write_claude_config({"oauthAccount": {"accountUuid": ACCOUNT}})
        self.assertEqual(usage_api.refresh(now=NOW, get=Opener(raises=rate_limited())),
                         (False, 196.0))

    def test_refresh_without_a_known_account_writes_nothing(self):
        # The reader compares our file's accountUuid against ~/.claude.json's, so
        # a file written with none could never be trusted afterwards.
        self.write_credentials()
        opener = Opener(body=json.dumps(BODY).encode("utf-8"))
        self.assertEqual(usage_api.refresh(now=NOW, get=opener), (False, None))
        self.assertIsNone(store.read_json(paths.usage_path()))

    def test_the_backoff_doubles_from_the_poll_interval(self):
        self.assertEqual(usage_api.backoff_seconds(0.0, 5.0), 5.0)
        self.assertEqual(usage_api.backoff_seconds(5.0, 5.0), 10.0)
        self.assertEqual(usage_api.backoff_seconds(10.0, 5.0), 20.0)

    def test_the_backoff_stops_doubling_at_the_cap(self):
        self.assertEqual(
            usage_api.backoff_seconds(usage_api.MAX_BACKOFF_SECONDS, 5.0),
            usage_api.MAX_BACKOFF_SECONDS)
