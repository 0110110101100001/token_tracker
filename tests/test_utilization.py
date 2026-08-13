# tests/test_utilization.py
"""Every way the cached account figures can be untrustworthy."""

from cost_meter import utilization
from tests.support import TempHome

NOW = 1_786_000_000.0
ACCOUNT = "acct-1"
RESET_5H = "2026-08-13T11:29:59.675479+00:00"
RESET_7D = "2026-08-15T00:59:59.675518+00:00"

LIMITS = {"limits": [
    {"kind": "session", "percent": 11, "severity": "normal",
     "resets_at": RESET_5H, "scope": None, "is_active": False},
    {"kind": "weekly_all", "percent": 15, "severity": "normal",
     "resets_at": RESET_7D, "scope": None, "is_active": True},
    {"kind": "weekly_scoped", "percent": 2, "severity": "normal",
     "resets_at": RESET_7D,
     "scope": {"model": {"id": None, "display_name": "Fable"}},
     "is_active": False},
]}

LEGACY = {
    "five_hour": {"utilization": 11, "resets_at": RESET_5H,
                  "limit_dollars": None, "used_dollars": None},
    "seven_day": {"utilization": 15, "resets_at": RESET_7D,
                  "limit_dollars": None, "used_dollars": None},
}


class UtilizationTest(TempHome):
    def write(self, utilization_block, account=ACCOUNT,
              cache_account=ACCOUNT, fetched_s=NOW - 60.0):
        self.write_claude_config({
            "oauthAccount": {"accountUuid": account},
            "cachedUsageUtilization": {
                "fetchedAtMs": fetched_s * 1000.0,
                "accountUuid": cache_account,
                "utilization": utilization_block,
            },
        })

    def test_the_limits_array_gives_both_rows_with_severity(self):
        self.write(LIMITS)
        result = utilization.read(now=NOW)
        self.assertEqual(result["rows"][utilization.SESSION],
                         {"pct": 11, "severity": "normal",
                          "resets_at": RESET_5H, "scope": None})
        self.assertEqual(result["rows"][utilization.WEEKLY]["pct"], 15)

    def test_the_scoped_weekly_limit_is_carried_with_its_model_named(self):
        # No row draws it, but parsing it here means adding that row later is a
        # widget change and nothing more.
        self.write(LIMITS)
        scoped = utilization.read(now=NOW)["rows"]["weekly_scoped"]
        self.assertEqual((scoped["pct"], scoped["scope"]), (2, "Fable"))

    def test_the_age_of_the_cache_is_reported(self):
        self.write(LIMITS, fetched_s=NOW - 1800.0)
        self.assertAlmostEqual(utilization.read(now=NOW)["age_s"], 1800.0, places=1)

    def test_the_older_shape_is_read_when_there_is_no_limits_array(self):
        self.write(LEGACY)
        rows = utilization.read(now=NOW)["rows"]
        self.assertEqual(rows[utilization.SESSION]["pct"], 11)
        self.assertEqual(rows[utilization.WEEKLY]["resets_at"], RESET_7D)
        # No severity in that shape; the panel colours from the percentage.
        self.assertIsNone(rows[utilization.SESSION]["severity"])

    def test_a_missing_file_is_no_answer(self):
        self.assertIsNone(utilization.read(now=NOW))

    def test_an_unreadable_file_is_no_answer(self):
        with open(self.claude_config, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(utilization.read(now=NOW))

    def test_another_accounts_cache_is_refused(self):
        # What a re-login leaves behind. Presenting it would report somebody
        # else's usage as this account's.
        self.write(LIMITS, account="acct-2", cache_account="acct-1")
        self.assertIsNone(utilization.read(now=NOW))

    def test_a_cache_past_the_sanity_cap_is_refused(self):
        self.write(LIMITS, fetched_s=NOW - utilization.MAX_AGE_SECONDS - 1.0)
        self.assertIsNone(utilization.read(now=NOW))

    def test_an_hours_old_cache_is_still_an_answer(self):
        # The normal case, not an error: the server is re-asked on session start
        # and on /usage, so hours pass between refreshes. The figure is a floor,
        # and the panel says so with the >= marker rather than throwing it away.
        self.write(LIMITS, fetched_s=NOW - 4 * 3600.0)
        rows = utilization.read(now=NOW)["rows"]
        self.assertEqual(rows[utilization.SESSION]["pct"], 11)

    def test_a_cache_written_by_a_clock_ahead_of_ours_is_not_fresh_forever(self):
        self.write(LIMITS, fetched_s=NOW + 600.0)
        self.assertEqual(utilization.read(now=NOW)["age_s"], 0.0)

    def test_a_shape_carrying_no_percentage_is_no_answer(self):
        self.write({"limits": [{"kind": "session", "percent": None}]})
        self.assertIsNone(utilization.read(now=NOW))

    def test_a_cache_with_no_utilization_block_is_no_answer(self):
        self.write(None)
        self.assertIsNone(utilization.read(now=NOW))
