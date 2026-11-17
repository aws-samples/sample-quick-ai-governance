"""Literal-value tests for license and activity rules. No real account ids."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))

import license_rules as rules

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
ACCOUNT = "111122223333"

USERS = [
    {"userArn": f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/AdminRole/session-a",
     "userName": "AdminRole/session-a", "identityType": "IAM", "role": "ADMIN_PRO"},
    {"userArn": f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/author@example.com",
     "userName": "author@example.com", "identityType": "QUICK", "role": "AUTHOR_PRO"},
    {"userArn": f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/reader@example.com",
     "userName": "reader@example.com", "identityType": "QUICK", "role": "READER_PRO"},
    {"userArn": f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/sso-user",
     "userName": "sso-user", "identityType": "IDENTITY_CENTER", "identityCenterId": "user-111",
     "role": "READER_PRO"},
]


class AttributionTests(unittest.TestCase):
    def test_quick_managed_directory_name(self):
        arn, note = rules.attribute_user(
            {"type": "Directory", "userName": "author@example.com"}, USERS)
        self.assertEqual(arn, USERS[1]["userArn"])
        self.assertEqual(note, "")

    def test_iam_assumed_role_matches_the_role_when_the_session_changed(self):
        arn, note = rules.attribute_user(
            {"type": "AssumedRole",
             "arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/AdminRole/other-session"}, USERS)
        self.assertEqual(arn, USERS[0]["userArn"])
        self.assertEqual(note, "")
        arn, note = rules.attribute_user(
            {"type": "AssumedRole",
             "arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/AdminRole/session-a"}, USERS)
        self.assertEqual(arn, USERS[0]["userArn"])
        self.assertEqual(note, "")

    def test_iam_directory_arn(self):
        arn, _note = rules.attribute_user(
            {"type": "Directory",
             "arn": f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/iam/AdminRole:session-a"},
            USERS)
        self.assertEqual(arn, USERS[0]["userArn"])

    def test_identity_center_is_labelled_untested(self):
        arn, note = rules.attribute_user(
            {"type": "AssumedRole", "onBehalfOf": {"userId": "user-111"}}, USERS)
        self.assertEqual(arn, USERS[3]["userArn"])
        self.assertEqual(note, "untested")

    def test_missing_identity_is_not_attributed(self):
        self.assertEqual(rules.attribute_user(None, USERS), (None, ""))
        self.assertEqual(rules.attribute_user({}, USERS), (None, ""))

    def test_assumed_role_does_not_match_another_role_with_the_same_session(self):
        users = [
            {"userArn": f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/FinanceRole/bob",
             "userName": "FinanceRole/bob", "identityType": "IAM"},
            {"userArn": f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/AdminRole/bob",
             "userName": "AdminRole/bob", "identityType": "IAM"},
        ]
        arn, note = rules.attribute_user(
            {"type": "AssumedRole",
             "arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/AdminRole/bob"}, users)
        self.assertEqual(arn, users[1]["userArn"])
        self.assertEqual(note, "")

    def test_directory_iam_does_not_match_on_the_session_suffix(self):
        users = [
            {"userArn": f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/FinanceRole/bob",
             "userName": "FinanceRole/bob", "identityType": "IAM"},
            {"userArn": f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/AdminRole/bob",
             "userName": "AdminRole/bob", "identityType": "IAM"},
        ]
        arn, _note = rules.attribute_user(
            {"type": "Directory",
             "arn": f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/iam/AdminRole:bob"},
            users)
        self.assertEqual(arn, users[1]["userArn"])


class ClassificationTests(unittest.TestCase):
    def test_one_name_from_each_category(self):
        self.assertEqual(rules.classify_event("quicksight.amazonaws.com", "DeleteAnalysis"), "authoring")
        self.assertEqual(rules.classify_event("quicksight.amazonaws.com", "InvokeAgentInternal"), "AI")
        self.assertEqual(rules.classify_event("quicksight.amazonaws.com", "UpdateUser"), "administration")
        self.assertEqual(rules.classify_event("quicksight.amazonaws.com", "GetDashboard"), "viewing")

    def test_sign_in_is_not_activity(self):
        self.assertIsNone(rules.classify_event("signin.amazonaws.com", "ConsoleLogin"))


class StateTests(unittest.TestCase):
    def test_pending_wins(self):
        judged = rules.judge_user(NOW, NOW, NOW - dt.timedelta(days=90), False, 60)
        self.assertEqual(judged["usageState"], "PENDING")

    def test_evidence_on_the_boundary_is_active(self):
        judged = rules.judge_user(NOW, NOW - dt.timedelta(days=60), NOW - dt.timedelta(days=90), True, 60)
        self.assertEqual(judged["usageState"], "ACTIVE")

    def test_no_activity_and_full_coverage_is_idle(self):
        judged = rules.judge_user(NOW, None, NOW - dt.timedelta(days=90), True, 60)
        self.assertEqual(judged["usageState"], "IDLE")
        self.assertIsNone(judged["daysSince"])

    def test_short_coverage_is_unknown(self):
        judged = rules.judge_user(NOW, None, NOW - dt.timedelta(days=10), True, 60)
        self.assertEqual(judged["usageState"], "UNKNOWN")
        self.assertEqual(judged["unknownReason"], "coverage is 10 days")
        self.assertEqual(judged["recheckAfter"], "2027-01-05")


class LicenseTests(unittest.TestCase):
    def test_an_author_stays_on_enterprise(self):
        fit = rules.license_fit(NOW, "AUTHOR_PRO", "QUICK", "ACTIVE", 1.0, 2, False, "HIGH")
        self.assertEqual(fit["candidateTier"], "ENTERPRISE")
        self.assertEqual(fit["candidatePriceUsd"], 40.0)
        self.assertEqual(fit["savingUsd"], 0.0)

    def test_reader_pro_half_hour_moves_to_essentials(self):
        fit = rules.license_fit(NOW, "READER_PRO", "QUICK", "ACTIVE", 0.5, 0, False, "HIGH")
        self.assertEqual(fit["candidateTier"], "ESSENTIALS")
        self.assertEqual(fit["savingUsd"], 17.0)
        self.assertEqual(fit["changePath"], "Reader Pro > Reader")
        self.assertEqual(fit["savingFrom"], "2026-12")

    def test_reader_pro_two_hours_changes_when_the_bonus_ends(self):
        before = dt.datetime(2027, 1, 31, 0, 0, tzinfo=UTC)
        after = dt.datetime(2027, 2, 1, 0, 0, tzinfo=UTC)
        early = rules.license_fit(before, "READER_PRO", "QUICK", "ACTIVE", 2.0, 0, False, "HIGH")
        late = rules.license_fit(after, "READER_PRO", "QUICK", "ACTIVE", 2.0, 0, False, "HIGH")
        self.assertEqual(rules.included_hours("ESSENTIALS", before), 3.0)
        self.assertEqual(early["candidateTier"], "ESSENTIALS")
        self.assertEqual(early["savingUsd"], 17.0)
        self.assertEqual(rules.included_hours("ESSENTIALS", after), 1.0)
        self.assertEqual(late["candidateTier"], "PROFESSIONAL")
        self.assertEqual(late["savingUsd"], 0.0)

    def test_twelve_hour_peak_with_overage_stays_on_professional(self):
        fit = rules.license_fit(NOW, "READER_PRO", "QUICK", "ACTIVE", 12.0, 0, True, "HIGH")
        self.assertEqual(fit["candidateTier"], "PROFESSIONAL")
        self.assertEqual(fit["currentCostUsd"], 32.0)
        self.assertEqual(fit["candidatePriceUsd"], 32.0)
        self.assertNotEqual(fit["candidateTier"], "ESSENTIALS")
        self.assertEqual(fit["savingUsd"], 0.0)

    def test_twenty_hour_peak_is_cheaper_on_enterprise(self):
        fit = rules.license_fit(NOW, "READER_PRO", "QUICK", "ACTIVE", 20.0, 0, True, "HIGH")
        self.assertEqual(fit["currentCostUsd"], 56.0)
        self.assertEqual(fit["candidateTier"], "ENTERPRISE")
        self.assertEqual(fit["candidatePriceUsd"], 46.0)
        self.assertEqual(fit["savingUsd"], 10.0)

    def test_a_stale_feed_gives_no_saving(self):
        fit = rules.license_fit(NOW, "READER_PRO", "QUICK", "ACTIVE", 0.5, 0, False, "LOW")
        self.assertIsNone(fit["candidateTier"])
        self.assertIsNone(fit["savingUsd"])

    def test_anything_other_than_high_confidence_gives_no_saving(self):
        fit = rules.license_fit(NOW, "READER_PRO", "QUICK", "ACTIVE", 0.5, 0, False, "MEDIUM")
        self.assertIsNone(fit["candidateTier"])
        self.assertIsNone(fit["savingUsd"])
        self.assertEqual(fit["tierCosts"]["ESSENTIALS"], 3.0)
        self.assertEqual(fit["tierCosts"]["PROFESSIONAL"], 20.0)
        self.assertEqual(fit["tierCosts"]["ENTERPRISE"], 40.0)

    def test_one_call_prices_every_tier(self):
        fit = rules.license_fit(NOW, "READER_PRO", "QUICK", "ACTIVE", 12.0, 0, True, "HIGH")
        self.assertIsNone(fit["tierCosts"]["ESSENTIALS"])
        self.assertEqual(fit["tierCosts"]["PROFESSIONAL"], 32.0)
        self.assertEqual(fit["tierCosts"]["ENTERPRISE"], 40.0)
        self.assertEqual(fit["candidateTier"], "PROFESSIONAL")

    def test_idle_and_admin_are_not_candidates(self):
        idle = rules.license_fit(NOW, "READER_PRO", "QUICK", "IDLE", 0.5, 0, False, "HIGH")
        admin = rules.license_fit(NOW, "ADMIN_PRO", "IAM", "ACTIVE", 1.0, 0, False, "HIGH")
        self.assertIsNone(idle["savingUsd"])
        self.assertIsNone(admin["candidateTier"])


class ReadinessAndGapTests(unittest.TestCase):
    def test_readiness_order(self):
        self.assertEqual(rules.removal_readiness("ADMIN_PRO", "IDLE", 0, True), "Administrator: review by hand")
        self.assertEqual(rules.removal_readiness("READER_PRO", "UNKNOWN", 0, True), "Not enough history")
        self.assertEqual(rules.removal_readiness("READER_PRO", "IDLE", None, False), "Ownership unknown")
        self.assertEqual(rules.removal_readiness("READER_PRO", "IDLE", 2, True), "Transfer ownership first")
        self.assertEqual(rules.removal_readiness("READER_PRO", "IDLE", 0, True), "Ready for review")

    def test_gap_days_are_the_ai_days_the_feed_missed(self):
        ai = {dt.date(2026, 11, day) for day in range(6, 13)}
        feed = {dt.date(2026, 11, 5), dt.date(2026, 11, 12)}
        self.assertEqual(rules.gap_days(ai, feed), [
            "2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09",
            "2026-11-10", "2026-11-11",
        ])


class FeedAndCursorTests(unittest.TestCase):
    def test_later_activity_keeps_the_newer_feed(self):
        trail = NOW - dt.timedelta(days=40)
        hours = NOW - dt.timedelta(days=2)
        self.assertEqual(rules.later_activity(trail, hours), hours)
        self.assertEqual(rules.later_activity(hours, None), hours)
        self.assertEqual(rules.later_activity(None, None), None)

    def test_an_unread_hours_object_is_not_a_zero_peak(self):
        stamps = [(NOW - dt.timedelta(days=40), 0.5)]
        self.assertIsNone(rules.peak_for_read(stamps, NOW, False))
        self.assertEqual(rules.peak_for_read([], NOW, True), 0.0)
        unread = rules.license_fit(NOW, "READER_PRO", "QUICK", "ACTIVE", None, 0, False, "HIGH")
        self.assertIsNone(unread["savingUsd"])

    def test_one_users_missing_day_does_not_lower_another(self):
        missing = {dt.date(2026, 11, 7)}
        covered = {dt.date(2026, 11, 7)}
        self.assertEqual(
            rules.hours_confidence("OK", True, 90, 60, missing, set(), None, NOW), "LOW")
        self.assertEqual(
            rules.hours_confidence("OK", True, 90, 60, set(), set(), None, NOW), "HIGH")
        self.assertEqual(
            rules.hours_confidence("OK", True, 90, 60, covered, covered, None, NOW), "HIGH")
        self.assertEqual(
            rules.hours_confidence("OK", False, 90, 60, set(), set(), None, NOW), "LOW")
        self.assertEqual(
            rules.hours_confidence("NOT_DEPLOYED", False, 0, 60, set(), set(), None, NOW),
            "not available")

    def test_ownership_matches_the_user_name_not_the_arn_tail(self):
        name = "AdminRole/bob"
        self.assertTrue(rules.owns(
            f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/{name}", name))
        self.assertTrue(rules.owns(name, name))
        self.assertFalse(rules.owns(
            f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/FinanceRole/bob", name))
        self.assertFalse(rules.owns("bob", name))

    def test_ownership_uses_the_snapshot_form_not_a_session_suffix(self):
        user_name = ("R" * 64) + "/user@example.com"
        arn = f"arn:aws:quicksight:us-east-1:{ACCOUNT}:user/default/{user_name}"
        stored = arn.split("/", 1)[-1][-80:]
        self.assertEqual(len(user_name), 81)
        self.assertTrue(rules.owns(stored, user_name))
        self.assertTrue(rules.owns("default/AdminRole/bob", "AdminRole/bob"))
        self.assertFalse(rules.owns("default/AdminRole/bob", "bob"))

    def test_a_capped_scan_does_not_claim_the_window_is_finished(self):
        high = NOW - dt.timedelta(days=10)
        oldest = NOW - dt.timedelta(days=1)
        new_high, pending = rules.next_incremental(high, NOW, "capped", oldest)
        self.assertEqual(new_high, high)
        self.assertEqual(pending, oldest)
        finished, pending = rules.next_incremental(high, oldest, "floor", oldest)
        self.assertEqual(finished, oldest)
        self.assertIsNone(pending)
        cursor, done = rules.next_backfill(NOW, "capped", oldest, NOW - dt.timedelta(days=90))
        self.assertEqual(cursor, oldest)
        self.assertFalse(done)
        cursor, done = rules.next_backfill(oldest, "floor", None, NOW - dt.timedelta(days=90))
        self.assertTrue(done)

    def test_two_regions_stay_inside_the_lambda_timeout(self):
        cap = rules.cloudtrail_page_cap(2, 0.55)
        self.assertLess(cap * 2 * 2 * 0.55, 900)
        self.assertLess(cap * 2 * 2 * 0.55, 700)

    def test_the_sign_in_scan_cannot_write_a_cursor(self):
        text = Path(__file__).resolve().parents[1].joinpath("lambda", "collector.py").read_text()
        scan = text.split("def _scan_window", 1)[1].split("def collect_cloudtrail", 1)[0]
        self.assertNotIn("backfillCursor", scan)
        self.assertNotIn("highWater", scan)

    def test_a_repeated_authoring_event_is_counted_once(self):
        entries: list[dict] = []
        rules.remember_authoring(entries, "arn:user", "2026-11-16T12:00:00.100000Z", "UpdateAnalysis")
        rules.remember_authoring(entries, "arn:user", "2026-11-16T12:00:00.100000Z", "UpdateAnalysis")
        rules.remember_authoring(entries, "arn:user", "2026-11-16T12:00:01.100000Z", "UpdateAnalysis")
        self.assertEqual(entries, [
            {"arn": "arn:user", "at": "2026-11-16T12:00:00.100000Z", "name": "UpdateAnalysis"},
            {"arn": "arn:user", "at": "2026-11-16T12:00:01.100000Z", "name": "UpdateAnalysis"},
        ])

    def test_a_cursor_keeps_the_fraction_of_a_second(self):
        when = dt.datetime(2026, 3, 20, 15, 4, 5, 100000, tzinfo=UTC)
        text = rules.iso_cursor(when)
        self.assertEqual(text, "2026-03-20T15:04:05.100000Z")
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        self.assertEqual(parsed, when)

    def test_hour_rows_stop_at_the_peak_window(self):
        old = ["2026-07-02T00:00:00Z", 8.0]
        kept = ["2026-08-02T00:00:00Z", 4.0]
        self.assertEqual(rules.keep_hour_rows([old, kept], NOW), [kept])
        self.assertEqual(rules.stamp_keep_from(NOW), dt.date(2026, 8, 1))
        leap = dt.datetime(2024, 3, 1, 12, tzinfo=UTC)
        self.assertEqual(rules.stamp_keep_from(leap), dt.date(2023, 12, 1))
        root = Path(__file__).resolve().parents[1]
        floor = root.joinpath("lambda", "license_rules.py").read_text()
        floor = floor.split("def stamp_keep_from", 1)[1].split("def keep_hour_rows", 1)[0]
        self.assertNotIn("[-1]", floor)
        self.assertIn("AI_HISTORY_DAYS", floor)
        collector = root.joinpath("lambda", "collector.py").read_text()
        self.assertIn("rules.AI_HISTORY_DAYS", collector)
        self.assertNotIn("timedelta(days=90)", collector)

    def test_dashboard_sql_reads_included_hours_from_the_tier_row(self):
        text = Path(__file__).resolve().parents[1].joinpath(
            "cloudformation", "governance-license-activity-quick-dashboard.yaml").read_text()
        self.assertIn("u.peakhours > t.includedhours", text)
        self.assertNotIn("2027-02-01", text)
        self.assertNotIn("WHEN 'ESSENTIALS'", text)
        self.assertIn("max(ts)", text)
        self.assertIn("JOIN latest", text)

    def test_cloudtrail_is_short_while_events_exist_and_coverage_is_open(self):
        self.assertEqual(rules.cloudtrail_status(False, True), "SHORT")
        self.assertEqual(rules.cloudtrail_status(True, True), "OK")
        self.assertEqual(rules.cloudtrail_status(False, False), "MISSING")

    def test_a_later_ai_event_does_not_hide_authoring(self):
        entries = []
        rules.remember_category(entries, "arn", "2026-11-01T00:00:00.000000Z", "authoring")
        rules.remember_category(entries, "arn", "2026-11-17T12:57:00.000000Z", "AI")
        rules.remember_category(entries, "arn", "2026-11-17T12:57:00.000000Z", "AI")
        self.assertEqual(
            rules.categories_in_window(entries, "arn", "2026-09-18T00:00:00.000000Z"),
            ["authoring", "AI"],
        )

    def test_the_license_table_fits_four_users_and_the_activity_chart_counts_each_type(self):
        text = Path(__file__).resolve().parents[1].joinpath(
            "cloudformation", "governance-license-activity-quick-dashboard.yaml").read_text()
        table = text.split("ElementId: lf-tbl-fit", 1)[1].split("ElementId: lf-bar-tier", 1)[0]
        self.assertIn("RowSpan: 18", table)
        chart = text.split("VisualId: us-pivot", 1)[1].split("VisualId: us-bar-days", 1)[0]
        self.assertNotIn("activity_category", chart)
        self.assertIn("did_authoring", chart)
        self.assertIn("did_ai", chart)
        users_table = text.split("Name: license_activity_users", 1)[1].split("SourcesTable:", 1)[0]
        for column in ("didauthoring", "didai", "didviewing", "didadministration"):
            self.assertIn(column, users_table)

    def test_the_cutoff_date_is_not_counted_as_an_ai_day(self):
        self.assertEqual(
            rules.ai_days_in_window(["2026-09-18", "2026-09-19"], "2026-09-18T04:30:00.000000Z"),
            ["2026-09-19"],
        )


if __name__ == "__main__":
    unittest.main()
