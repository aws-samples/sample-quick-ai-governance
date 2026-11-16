"""Literal-value tests for the dataset lifecycle rules."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))

import lifecycle_rules as rules

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
WINDOW = 60


def _at(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))


class StateRuleTests(unittest.TestCase):
    def test_evidence_exactly_at_the_window_is_active(self):
        judged = rules.judge(NOW, NOW - dt.timedelta(days=60), NOW - dt.timedelta(days=90), NOW - dt.timedelta(days=100), WINDOW)
        self.assertEqual(judged["usageState"], "ACTIVE")
        self.assertEqual(judged["daysSince"], 60)
        self.assertIsNone(judged["unknownReason"])
        self.assertIsNone(judged["recheckAfter"])

    def test_evidence_one_day_past_the_window_is_not_active(self):
        judged = rules.judge(NOW, NOW - dt.timedelta(days=61), NOW - dt.timedelta(days=90), NOW - dt.timedelta(days=100), WINDOW)
        self.assertEqual(judged["usageState"], "IDLE")
        self.assertEqual(judged["daysSince"], 61)

    def test_no_evidence_full_coverage_and_old_asset_is_idle(self):
        judged = rules.judge(NOW, None, NOW - dt.timedelta(days=60), NOW - dt.timedelta(days=61), WINDOW)
        self.assertEqual(judged["usageState"], "IDLE")
        self.assertEqual(judged["coverageDays"], 60)
        self.assertIsNone(judged["daysSince"])

    def test_asset_created_exactly_at_the_window_is_unknown(self):
        judged = rules.judge(NOW, None, NOW - dt.timedelta(days=10), NOW - dt.timedelta(days=60), WINDOW)
        self.assertEqual(judged["usageState"], "UNKNOWN")
        self.assertEqual(judged["unknownReason"], "created 60 days ago")
        self.assertEqual(judged["recheckAfter"], "2027-01-02")

    def test_short_coverage_on_an_old_asset_names_coverage(self):
        judged = rules.judge(NOW, None, NOW - dt.timedelta(days=59), NOW - dt.timedelta(days=100), WINDOW)
        self.assertEqual(judged["usageState"], "UNKNOWN")
        self.assertEqual(judged["unknownReason"], "coverage is 59 days")
        self.assertEqual(judged["recheckAfter"], "2026-11-14")

    def test_young_asset_wins_over_short_coverage(self):
        judged = rules.judge(NOW, None, NOW - dt.timedelta(days=5), NOW - dt.timedelta(days=10), WINDOW)
        self.assertEqual(judged["usageState"], "UNKNOWN")
        self.assertEqual(judged["unknownReason"], "created 10 days ago")
        self.assertEqual(judged["recheckAfter"], "2027-01-07")

    def test_missing_created_date_is_unknown(self):
        judged = rules.judge(NOW, None, NOW - dt.timedelta(days=90), None, WINDOW)
        self.assertEqual(judged["usageState"], "UNKNOWN")
        self.assertEqual(judged["unknownReason"], "created date is missing")
        self.assertEqual(judged["recheckAfter"], "2026-10-14")


class RefreshExclusionTests(unittest.TestCase):
    def test_spice_query_inside_the_window_does_not_count(self):
        created = _at("2026-11-13T10:00:00Z")
        scan = _at("2026-11-13T12:00:00Z")
        ingestions = [{"created": created, "duration": 300}]
        self.assertFalse(rules.query_counts_as_use("SPICE", _at("2026-11-13T10:02:00Z"), ingestions, scan))

    def test_edges_of_the_margin_are_inclusive(self):
        created = _at("2026-11-13T10:00:00Z")
        scan = _at("2026-11-13T12:00:00Z")
        ingestions = [{"created": created, "duration": 300}]
        self.assertFalse(rules.query_counts_as_use("SPICE", created - dt.timedelta(seconds=120), ingestions, scan))
        self.assertFalse(rules.query_counts_as_use("SPICE", created + dt.timedelta(seconds=420), ingestions, scan))
        self.assertTrue(rules.query_counts_as_use("SPICE", created - dt.timedelta(seconds=121), ingestions, scan))
        self.assertTrue(rules.query_counts_as_use("SPICE", created + dt.timedelta(seconds=421), ingestions, scan))

    def test_missing_duration_ends_at_the_scan(self):
        created = _at("2026-11-13T10:00:00Z")
        scan = _at("2026-11-13T12:00:00Z")
        ingestions = [{"created": created, "duration": None}]
        self.assertFalse(rules.query_counts_as_use("SPICE", scan, ingestions, scan))
        self.assertTrue(rules.query_counts_as_use("SPICE", scan + dt.timedelta(seconds=1), ingestions, scan))

    def test_direct_query_inside_a_window_still_counts(self):
        created = _at("2026-11-13T10:00:00Z")
        scan = _at("2026-11-13T12:00:00Z")
        ingestions = [{"created": created, "duration": 300}]
        self.assertTrue(rules.query_counts_as_use("DIRECT_QUERY", created, ingestions, scan))


class SourceLabelTests(unittest.TestCase):
    def test_dashboard_analysis_and_unnamed_queries(self):
        names = {"dash-1": "TICKIT"}
        self.assertEqual(rules.query_source_label("dashboard", "dash-1", names), "Query from dashboard TICKIT")
        self.assertEqual(rules.query_source_label("dashboard", "dash-9", names), "Query from dashboard dash-9")
        self.assertEqual(rules.query_source_label("analysis", "an-7", names), "Query from analysis an-7")
        self.assertEqual(rules.query_source_label(None, None, names), "Query (no asset named)")

    def test_dashboard_wins_when_the_event_names_both(self):
        kind, asset_id = rules.named_asset([
            "arn:aws:quicksight:us-east-1:111122223333:analysis/an-7",
            "arn:aws:quicksight:us-east-1:111122223333:dashboard/dash-1",
        ])
        self.assertEqual((kind, asset_id), ("dashboard", "dash-1"))

    def test_evidence_labels(self):
        self.assertEqual(rules.evidence_label("CLOUDTRAIL_QUERY", "Query from dashboard TICKIT"), "Query from dashboard TICKIT")
        self.assertEqual(rules.evidence_label("AGENT_CITED_DATASET"), "Agent citation")
        self.assertEqual(rules.evidence_label("DASHBOARD_VIEW_METRIC"), "Dashboard view")
        self.assertEqual(rules.evidence_label("DASHBOARD_VIEW_CLOUDTRAIL"), "Dashboard opened (CloudTrail)")
        self.assertIsNone(rules.evidence_label(None))


class ScheduleTests(unittest.TestCase):
    def test_each_interval(self):
        minute15 = rules.expand_schedule(
            {"interval": "MINUTE15", "timezone": "UTC", "refresh_type": "INCREMENTAL_REFRESH"},
            _at("2026-11-13T10:10:00Z"),
            _at("2026-11-13T10:07:00Z"),
        )
        self.assertEqual(minute15["definition"], "Every 15 minutes, incremental, at :07, :22, :37 and :52")
        self.assertEqual(minute15["next_run_at"], "2026-11-13T10:22:00Z")
        self.assertEqual(minute15["runs_per_day"], 96.0)
        self.assertEqual(minute15["minute_source"], "scheduled refresh")
        self.assertEqual(minute15["planned"][0], "2026-11-13T10:22:00Z")
        self.assertEqual(minute15["planned"][-1], "2026-11-20T10:07:00Z")
        self.assertEqual(len(minute15["planned"]), 672)

        minute30 = rules.expand_schedule(
            {"interval": "MINUTE30", "timezone": "UTC", "refresh_type": "FULL_REFRESH"},
            _at("2026-11-13T10:10:00Z"),
            _at("2026-11-13T10:07:00Z"),
        )
        self.assertEqual(minute30["definition"], "Every 30 minutes, full, at :07 and :37")
        self.assertEqual(minute30["next_run_at"], "2026-11-13T10:37:00Z")
        self.assertEqual(minute30["runs_per_day"], 48.0)

        hourly = rules.expand_schedule(
            {"interval": "HOURLY", "timezone": "UTC", "refresh_type": "FULL_REFRESH"},
            _at("2026-11-13T10:10:00Z"),
            _at("2026-11-13T09:07:00Z"),
        )
        self.assertEqual(hourly["definition"], "Hourly at :07, full")
        self.assertEqual(hourly["next_run_at"], "2026-11-13T11:07:00Z")
        self.assertEqual(hourly["runs_per_day"], 24.0)

        daily = rules.expand_schedule(
            {"interval": "DAILY", "time_of_day": "06:00", "timezone": "America/Sao_Paulo", "refresh_type": "FULL_REFRESH"},
            NOW,
            None,
        )
        self.assertEqual(daily["definition"], "Daily 06:00 America/Sao_Paulo, full")
        self.assertEqual(daily["next_run_at"], "2026-11-14T09:00:00Z")
        self.assertEqual(daily["runs_per_day"], 1.0)

        weekly = rules.expand_schedule(
            {"interval": "WEEKLY", "time_of_day": "06:00", "timezone": "America/Sao_Paulo", "day_of_week": "MONDAY", "refresh_type": "FULL_REFRESH"},
            NOW,
            None,
        )
        self.assertEqual(weekly["definition"], "Weekly Monday 06:00 America/Sao_Paulo, full")
        self.assertEqual(weekly["next_run_at"], "2026-11-16T09:00:00Z")
        self.assertEqual(weekly["runs_per_day"], 0.143)

        monthly = rules.expand_schedule(
            {"interval": "MONTHLY", "time_of_day": "06:00", "timezone": "America/Sao_Paulo", "day_of_month": "15", "refresh_type": "FULL_REFRESH"},
            NOW,
            None,
        )
        self.assertEqual(monthly["definition"], "Monthly day 15, 06:00 America/Sao_Paulo, full")
        self.assertEqual(monthly["next_run_at"], "2026-11-15T09:00:00Z")
        self.assertEqual(monthly["runs_per_day"], 0.033)
        self.assertEqual(monthly["planned"], ["2026-11-15T09:00:00Z"])

    def test_unknown_minute_has_no_concrete_runs(self):
        expanded = rules.expand_schedule(
            {"interval": "MINUTE15", "timezone": "UTC", "refresh_type": "INCREMENTAL_REFRESH"},
            _at("2026-11-13T10:10:00Z"),
            None,
        )
        self.assertEqual(expanded["definition"], "Every 15 minutes, incremental, within 15 minutes")
        self.assertIsNone(expanded["next_run_at"])
        self.assertEqual(expanded["next_run_label"], "within 15 minutes")
        self.assertEqual(expanded["planned"], [])
        self.assertFalse(expanded["minute_known"])
        self.assertEqual(expanded["runs_per_day"], 96.0)

    def test_last_day_of_month(self):
        expanded = rules.expand_schedule(
            {"interval": "MONTHLY", "time_of_day": "06:00", "timezone": "America/Sao_Paulo", "day_of_month": "LAST_DAY_OF_MONTH", "refresh_type": "FULL_REFRESH"},
            _at("2026-02-01T00:00:00Z"),
            None,
        )
        self.assertEqual(expanded["definition"], "Monthly, last day, 06:00 America/Sao_Paulo, full")
        self.assertEqual(expanded["next_run_at"], "2026-02-28T09:00:00Z")

    def test_daylight_saving_gap_and_ambiguous_hour(self):
        gap = rules.expand_schedule(
            {"interval": "DAILY", "time_of_day": "02:30", "timezone": "America/New_York", "refresh_type": "FULL_REFRESH"},
            _at("2026-03-07T12:00:00Z"),
            None,
        )
        self.assertEqual(gap["next_run_at"], "2026-03-08T07:00:00Z")

        ambiguous = rules.expand_schedule(
            {"interval": "DAILY", "time_of_day": "01:30", "timezone": "America/New_York", "refresh_type": "FULL_REFRESH"},
            _at("2026-10-31T12:00:00Z"),
            None,
        )
        self.assertEqual(ambiguous["next_run_at"], "2026-11-01T05:30:00Z")

    def test_start_after_in_the_future_skips_the_earlier_slot(self):
        expanded = rules.expand_schedule(
            {
                "interval": "DAILY",
                "time_of_day": "06:00",
                "timezone": "America/Sao_Paulo",
                "refresh_type": "FULL_REFRESH",
                "start_after": _at("2026-11-14T12:00:00Z"),
            },
            NOW,
            None,
        )
        self.assertEqual(expanded["next_run_at"], "2026-11-15T09:00:00Z")
        self.assertEqual(expanded["planned"], ["2026-11-15T09:00:00Z", "2026-11-16T09:00:00Z", "2026-11-17T09:00:00Z", "2026-11-18T09:00:00Z", "2026-11-19T09:00:00Z", "2026-11-20T09:00:00Z"])

    def test_unknown_time_zone(self):
        expanded = rules.expand_schedule(
            {"interval": "DAILY", "time_of_day": "06:00", "timezone": "Not/AZone", "refresh_type": "FULL_REFRESH"},
            NOW,
            None,
        )
        self.assertEqual(expanded["definition"], "Schedule zone Not/AZone is unknown, full")
        self.assertIsNone(expanded["next_run_at"])
        self.assertEqual(expanded["planned"], [])
        self.assertFalse(expanded["zone_known"])


class ViewAndDashboardTests(unittest.TestCase):
    def test_view_windows_and_year_ago_coverage(self):
        now = _at("2026-11-13T00:00:00Z")
        points = [
            (_at("2025-08-15T00:00:00Z"), 2),
            (_at("2025-08-17T00:00:00Z"), 3),
            (_at("2026-10-01T00:00:00Z"), 4),
            (_at("2026-11-01T00:00:00Z"), 5),
        ]
        summary = rules.summarize_views(points, now)
        self.assertEqual(summary["views30"], 5)
        self.assertEqual(summary["views60"], 9)
        self.assertEqual(summary["views90"], 9)
        self.assertEqual(summary["views_year_ago_90"], 5)
        self.assertTrue(summary["history_covers_year_ago"])
        self.assertEqual(rules.iso(summary["last"]), "2026-11-01T00:00:00Z")

    def test_year_ago_is_null_when_history_starts_too_late(self):
        now = _at("2026-11-13T00:00:00Z")
        summary = rules.summarize_views([(_at("2026-06-04T00:00:00Z"), 8)], now)
        self.assertIsNone(summary["views_year_ago_90"])
        self.assertFalse(summary["history_covers_year_ago"])
        self.assertEqual(summary["views90"], 0)

    def test_dashboard_record_and_tombstone(self):
        now = _at("2026-11-13T21:00:00Z")
        views = rules.summarize_views([
            (_at("2026-06-04T00:00:00Z"), 1),
            (_at("2026-11-01T00:00:00Z"), 4),
        ], now)
        record = rules.build_dashboard_record(
            now, "dash-1", "TICKIT",
            _at("2026-06-05T20:12:07Z"),
            _at("2026-06-05T20:12:07Z"),
            views,
            _at("2026-11-01T00:30:00Z"),
            _at("2026-08-15T21:00:00Z"),
            ["ds-1"], ["Sales"], "not available", WINDOW,
        )
        self.assertEqual(record["assetState"], "ACTIVE")
        self.assertEqual(record["lastViewAt"], "2026-11-01T00:30:00Z")
        self.assertEqual(record["lastViewSource"], "CloudTrail")
        self.assertEqual(record["daysSinceLastView"], 12)
        self.assertEqual(record["usageState"], "ACTIVE")
        self.assertEqual(record["views30"], 4)
        self.assertEqual(record["views90"], 4)
        self.assertIsNone(record["viewsYearAgo90"])
        self.assertEqual(record["historyCoversYearAgo"], "false")
        self.assertEqual(record["datasetNames"], "Sales")
        self.assertEqual(record["deletionCandidate"], "false")
        self.assertIsNone(record["lastViewLabel"])

        never = rules.build_dashboard_record(
            now, "dash-2", "Quiet",
            _at("2026-06-01T00:00:00Z"),
            _at("2026-06-02T00:00:00Z"),
            {"last": None, "history_start": _at("2026-06-04T00:00:00Z"), "views30": 0, "views60": 0, "views90": 0, "views_year_ago_90": None, "history_covers_year_ago": False},
            None,
            _at("2026-08-15T21:00:00Z"),
            [], [], "SINGLE_ACTIVE_OWNER", WINDOW,
        )
        self.assertEqual(never["usageState"], "IDLE")
        self.assertEqual(never["lastViewLabel"], "no view since 2026-06-04")
        self.assertEqual(never["sortDays"], 999999)
        self.assertEqual(never["deletionCandidate"], "false")
        self.assertEqual(never["ownerStatus"], "SINGLE_ACTIVE_OWNER")

        covered = rules.build_dashboard_record(
            now, "dash-3", "Old",
            _at("2024-01-01T00:00:00Z"),
            None,
            {"last": None, "history_start": _at("2025-01-01T00:00:00Z"), "views30": 0, "views60": 0, "views90": 0, "views_year_ago_90": 0, "history_covers_year_ago": True},
            None,
            _at("2025-01-01T00:00:00Z"),
            ["ds-9"], ["Finance"], "not available", WINDOW,
        )
        self.assertEqual(covered["usageState"], "IDLE")
        self.assertEqual(covered["deletionCandidate"], "true")
        self.assertEqual(covered["viewsYearAgo90"], 0)

        self.assertEqual(
            rules.dashboard_tombstone(now, "dash-1", "TICKIT"),
            {"ts": "2026-11-13T21:00:00Z", "dashboardId": "dash-1", "dashboardName": "TICKIT", "assetState": "DELETED"},
        )


class FailedReadTests(unittest.TestCase):
    def test_a_failed_read_is_not_a_zero(self):
        self.assertIsNone(rules.observed_count(False, 0))
        self.assertEqual(rules.observed_count(True, 0), 0)
        self.assertEqual(rules.observed_count(True, 4), 4)


class StoredRefreshHitTests(unittest.TestCase):
    def test_a_stored_refresh_hit_is_dropped_and_an_older_hit_stays(self):
        floor = _at("2026-08-15T00:00:00Z")
        scan = _at("2026-11-13T12:00:00Z")
        hits = {
            "ds-refresh": {"at": "2026-11-13T10:02:00Z", "kind": None, "assetId": None},
            "ds-real": {"at": "2026-11-01T00:00:00Z", "kind": "dashboard", "assetId": "dash-1"},
            "ds-old": {"at": "2026-07-01T00:00:00Z", "kind": None, "assetId": None},
        }
        windows = {
            "ds-refresh": [{"created": _at("2026-11-13T10:00:00Z"), "duration": 300}],
            "ds-old": [{"created": _at("2026-07-01T00:00:00Z"), "duration": 300}],
        }
        rules.forget_refresh_hits(hits, windows, floor, scan)
        self.assertNotIn("ds-refresh", hits)
        self.assertEqual(hits["ds-real"]["at"], "2026-11-01T00:00:00Z")
        self.assertEqual(hits["ds-old"]["at"], "2026-07-01T00:00:00Z")


class ScheduleEmissionTests(unittest.TestCase):
    def test_unknown_zone_keeps_the_schedule_and_drops_planned_runs(self):
        expanded = rules.expand_schedule(
            {"interval": "DAILY", "time_of_day": "06:00", "timezone": "Not/AZone", "refresh_type": "FULL_REFRESH"},
            NOW,
            None,
        )
        keep_label = expanded["definition"]
        self.assertIn("unknown", keep_label)
        self.assertEqual(rules.planned_runs(expanded), [])

    def test_known_zone_keeps_its_planned_runs(self):
        expanded = rules.expand_schedule(
            {"interval": "DAILY", "time_of_day": "06:00", "timezone": "UTC", "refresh_type": "FULL_REFRESH"},
            _at("2026-11-13T00:00:00Z"),
            None,
        )
        self.assertEqual(rules.planned_runs(expanded), expanded["planned"])
        self.assertGreater(len(expanded["planned"]), 0)


class WindowMergeTests(unittest.TestCase):
    def test_merge_drops_old_rows_and_keeps_the_newest_duration(self):
        floor = _at("2026-08-15T00:00:00Z")
        merged = rules.merge_windows(
            [{"created": "2026-08-01T00:00:00Z", "duration": 10, "source": "MANUAL"},
             {"created": "2026-11-01T00:00:00Z", "duration": 20, "source": "SCHEDULED"}],
            [{"created": "2026-11-01T00:00:00Z", "duration": 25, "source": "SCHEDULED"},
             {"created": "2026-11-13T00:00:00Z", "duration": None, "source": "MANUAL"}],
            floor,
        )
        self.assertEqual(merged, [
            {"created": "2026-11-01T00:00:00Z", "duration": 25, "source": "SCHEDULED"},
            {"created": "2026-11-13T00:00:00Z", "duration": None, "source": "MANUAL"},
        ])


if __name__ == "__main__":
    unittest.main()
