# tests/test_billing.py
"""How the session is paying: per token, or against a seat.

The distinction is not in the transcripts -- `usage.service_tier` reads
`standard` whichever way the account is billed, because it names the API's
latency tier and not its billing. It has to be read from the environment the
session is running in, which is why the Stop hook is what asks: a key exported
for one session is visible there and in no other process.

Nothing here reads the real ~/.claude. TempHome redirects it, so these tests
never touch live OAuth tokens and never depend on how the machine is logged in.
"""

import json
import os
import unittest

from cost_meter import billing, paths
from support import TempHome

OAUTH = {"claudeAiOauth": {"accessToken": "x",
                           "subscriptionType": "team",
                           "rateLimitTier": "default_claude_max_5x"}}


class BillingTest(TempHome):
    def setUp(self):
        super().setUp()
        # Detection reads the process environment, and the test runner's own may
        # carry any of these. Cleared per test so the result is decided by what
        # the test sets up and nothing else.
        for name in billing.API_ENV_VARS + billing.CLOUD_ENV_VARS:
            self.addCleanup(self._restore, name, os.environ.get(name))
            os.environ.pop(name, None)

    def write_credentials(self, payload):
        paths.credentials_path().write_text(json.dumps(payload),
                                            encoding="utf-8")

    def write_settings(self, payload):
        paths.claude_settings_path().write_text(json.dumps(payload),
                                                encoding="utf-8")


class ApiBillingTest(BillingTest):
    def test_an_api_key_in_the_environment_is_api_billing(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-whatever"
        self.assertEqual(billing.detect(),
                         {"mode": "api", "label": "API"})

    def test_an_auth_token_counts_as_well(self):
        os.environ["ANTHROPIC_AUTH_TOKEN"] = "whatever"
        self.assertEqual(billing.detect()["mode"], "api")

    def test_bedrock_and_vertex_are_billed_per_token_too(self):
        # Not Anthropic's own billing, but still paid by the token rather than
        # against a seat, which is the distinction the row exists to draw.
        for name in billing.CLOUD_ENV_VARS:
            with self.subTest(name=name):
                os.environ[name] = "1"
                self.assertEqual(billing.detect()["mode"], "api")
                os.environ.pop(name)

    def test_an_api_key_helper_in_settings_is_api_billing(self):
        self.write_settings({"apiKeyHelper": "/usr/local/bin/get-key.sh"})
        self.assertEqual(billing.detect()["mode"], "api")

    def test_an_empty_key_is_not_a_key(self):
        # Exported but blank is how a shell profile unsets one in practice, and
        # reading it as API billing would mislabel a perfectly ordinary seat.
        os.environ["ANTHROPIC_API_KEY"] = ""
        self.write_credentials(OAUTH)
        self.assertEqual(billing.detect()["mode"], "seat")


class SeatBillingTest(BillingTest):
    def test_stored_oauth_credentials_are_a_seat(self):
        self.write_credentials(OAUTH)
        self.assertEqual(billing.detect(),
                         {"mode": "seat", "label": "team · max 5x"})

    def test_a_seat_with_no_tier_names_the_subscription_alone(self):
        self.write_credentials({"claudeAiOauth": {"subscriptionType": "pro"}})
        self.assertEqual(billing.detect()["label"], "pro")

    def test_a_seat_with_no_subscription_names_the_tier_alone(self):
        self.write_credentials(
            {"claudeAiOauth": {"rateLimitTier": "default_claude_max_5x"}})
        self.assertEqual(billing.detect()["label"], "max 5x")

    def test_a_key_in_the_environment_wins_over_stored_credentials(self):
        # Claude Code prefers an exported key over the login it has on disk, so
        # a machine that is signed in can still be spending real money.
        os.environ["ANTHROPIC_API_KEY"] = "sk-whatever"
        self.write_credentials(OAUTH)
        self.assertEqual(billing.detect()["mode"], "api")


class UnknownBillingTest(BillingTest):
    """No answer beats a guessed one: the row shows a dash instead."""

    def test_no_key_and_no_credentials_is_unknown(self):
        self.assertEqual(billing.detect(),
                         {"mode": "unknown", "label": None})

    def test_unreadable_credentials_are_unknown_rather_than_a_crash(self):
        paths.credentials_path().write_text("{not json", encoding="utf-8")
        self.assertEqual(billing.detect()["mode"], "unknown")

    def test_credentials_without_an_oauth_block_are_unknown(self):
        self.write_credentials({"somethingElse": {}})
        self.assertEqual(billing.detect()["mode"], "unknown")

    def test_unreadable_settings_do_not_stop_the_credentials_being_read(self):
        paths.claude_settings_path().write_text("{not json", encoding="utf-8")
        self.write_credentials(OAUTH)
        self.assertEqual(billing.detect()["mode"], "seat")


class TierNameTest(unittest.TestCase):
    """`default_claude_max_5x` is not something to put on a panel."""

    def test_the_boilerplate_is_stripped(self):
        self.assertEqual(billing.tidy_tier("default_claude_max_5x"), "max 5x")

    def test_a_plain_tier_survives(self):
        self.assertEqual(billing.tidy_tier("pro"), "pro")

    def test_nothing_stays_nothing(self):
        self.assertIsNone(billing.tidy_tier(None))
        self.assertIsNone(billing.tidy_tier(""))

    def test_a_tier_that_is_only_boilerplate_does_not_become_empty(self):
        # Rather than an empty string, which would render as a stray separator.
        self.assertIsNone(billing.tidy_tier("default_claude"))


if __name__ == "__main__":
    unittest.main()
