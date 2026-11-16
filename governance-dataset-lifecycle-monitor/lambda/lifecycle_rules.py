"""Pure dataset-lifecycle rules. No AWS clients.

The collector imports this module. Tests import it directly so they can assert
literal values without credentials or a network call.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_WINDOW_DAYS = 60
REFRESH_MARGIN_SECONDS = 120

RUNS_PER_DAY = {
    "MINUTE15": 96.0,
    "MINUTE30": 48.0,
    "HOURLY": 24.0,
    "DAILY": 1.0,
    "WEEKLY": 0.143,
    "MONTHLY": 0.033,
}

_WITHIN = {
    "MINUTE15": "within 15 minutes",
    "MINUTE30": "within 30 minutes",
    "HOURLY": "within 1 hour",
}

_WEEKDAYS = {
    "MONDAY": 0,
    "TUESDAY": 1,
    "WEDNESDAY": 2,
    "THURSDAY": 3,
    "FRIDAY": 4,
    "SATURDAY": 5,
    "SUNDAY": 6,
}

_WEEKDAY_LABEL = {
    "MONDAY": "Monday",
    "TUESDAY": "Tuesday",
    "WEDNESDAY": "Wednesday",
    "THURSDAY": "Thursday",
    "FRIDAY": "Friday",
    "SATURDAY": "Saturday",
    "SUNDAY": "Sunday",
}


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def judge(
    now: dt.datetime,
    last_evidence: dt.datetime | None,
    coverage_start: dt.datetime | None,
    created_at: dt.datetime | None,
    window_days: int,
) -> dict:
    """ACTIVE, IDLE, or UNKNOWN for one asset.

    ``days`` is floored ``(now - instant).days``. An event exactly ``window_days``
    ago is still inside the window. An asset created exactly ``window_days`` ago
    is not old enough to be IDLE.
    """
    coverage_days = (
        (now - coverage_start).days if coverage_start is not None else None
    )
    age_days = (now - created_at).days if created_at is not None else None
    days_since = (
        (now - last_evidence).days if last_evidence is not None else None
    )

    active = last_evidence is not None and days_since <= window_days
    old_enough = age_days is not None and age_days > window_days
    covered = coverage_days is not None and coverage_days >= window_days

    reason = None
    recheck = None
    if active:
        state = "ACTIVE"
    elif covered and old_enough:
        state = "IDLE"
    else:
        state = "UNKNOWN"
        if not old_enough:
            reason = (
                "created date is missing"
                if age_days is None
                else f"created {age_days} days ago"
            )
        else:
            shown = coverage_days if coverage_days is not None else 0
            reason = f"coverage is {shown} days"
        ends = []
        if created_at is not None:
            ends.append(created_at + dt.timedelta(days=window_days))
        if coverage_start is not None:
            ends.append(coverage_start + dt.timedelta(days=window_days))
        recheck = max(ends) if ends else None

    return {
        "usageState": state,
        "daysSince": days_since,
        "coverageDays": coverage_days if coverage_days is not None else 0,
        "unknownReason": reason,
        "recheckAfter": recheck.astimezone(dt.timezone.utc).date().isoformat()
        if recheck is not None
        else None,
    }


def refresh_bounds(
    created: dt.datetime,
    duration_seconds: int | None,
    scan_time: dt.datetime,
    margin_seconds: int = REFRESH_MARGIN_SECONDS,
) -> tuple[dt.datetime, dt.datetime]:
    """Inclusive window around one ingestion. A missing duration ends at the scan."""
    start = created - dt.timedelta(seconds=margin_seconds)
    if duration_seconds is None:
        end = scan_time
    else:
        end = created + dt.timedelta(seconds=duration_seconds + margin_seconds)
    return start, end


def query_counts_as_use(
    import_mode: str | None,
    event_time: dt.datetime,
    ingestions: list[dict],
    scan_time: dt.datetime,
    margin_seconds: int = REFRESH_MARGIN_SECONDS,
) -> bool:
    """False only for a SPICE query that falls inside a refresh of that dataset.

    Direct-query events always count. ``ingestions`` items are
    ``{"created": datetime, "duration": int | None}``.
    """
    if import_mode != "SPICE":
        return True
    for ingestion in ingestions:
        start, end = refresh_bounds(
            ingestion["created"],
            ingestion.get("duration"),
            scan_time,
            margin_seconds,
        )
        if start <= event_time <= end:
            return False
    return True


def named_asset(values: list[str]) -> tuple[str | None, str | None]:
    """First dashboard or analysis id carried by a CloudTrail event.

    ``values`` is every resource id or ARN string on the event. A dashboard
    wins over an analysis when both are present.
    """
    found: list[tuple[str, str]] = []
    for value in values:
        if not value:
            continue
        lower = value.lower()
        for needle, kind in (("dashboard/", "dashboard"), ("analysis/", "analysis")):
            if needle in lower:
                asset_id = value.split("/")[-1]
                if asset_id:
                    found.append((kind, asset_id))
                break
    for kind in ("dashboard", "analysis"):
        for item_kind, asset_id in found:
            if item_kind == kind:
                return kind, asset_id
    return None, None


def query_source_label(
    asset_kind: str | None,
    asset_id: str | None,
    dashboard_names: dict[str, str],
) -> str:
    if asset_kind == "dashboard" and asset_id:
        name = dashboard_names.get(asset_id) or asset_id
        return f"Query from dashboard {name}"
    if asset_kind == "analysis" and asset_id:
        return f"Query from analysis {asset_id}"
    return "Query (no asset named)"


_EVIDENCE_LABELS = {
    "AGENT_CITED_DATASET": "Agent citation",
    "AGENT_SELECTED_RESOURCE": "Selected in chat",
    "AGENT_CITED_TOPIC": "Agent cited a topic",
    "AGENT_USED_SPACE": "Agent used a space",
    "AGENT_CONVERSATION_SCOPE": "Agent conversation",
    "DASHBOARD_VIEW_METRIC": "Dashboard view",
    "DASHBOARD_VIEW_CLOUDTRAIL": "Dashboard opened (CloudTrail)",
    "AGENT_CITED_DASHBOARD": "Agent cited a dashboard",
    "DEPENDENCY_ONLY": "Referenced only",
}


def evidence_label(source: str | None, query_label: str | None = None) -> str | None:
    if source == "CLOUDTRAIL_QUERY":
        return query_label or "Query (no asset named)"
    if source is None:
        return None
    return _EVIDENCE_LABELS.get(source, source)


def merge_windows(
    existing: list[dict],
    fetched: list[dict],
    floor: dt.datetime,
) -> list[dict]:
    """Dedupe ingestions by created time and drop those older than ``floor``.

    Each item is ``{"created": iso, "duration": int | None, "source": str}``.
    """
    merged: dict[str, dict] = {}
    for item in list(existing) + list(fetched):
        created = item.get("created")
        if not created:
            continue
        parsed = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        if parsed < floor:
            continue
        merged[created] = {
            "created": created,
            "duration": item.get("duration"),
            "source": item.get("source"),
        }
    return [merged[key] for key in sorted(merged)]


def _zone(name: str | None):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def _wall_to_utc(
    year: int, month: int, day: int, hour: int, minute: int, zone,
) -> dt.datetime | None:
    """Local wall time to UTC. A DST gap moves to the next real minute.

    An ambiguous wall time uses the first occurrence (fold 0).
    """
    naive = dt.datetime(year, month, day, hour, minute)
    for extra in range(0, 180):
        trial = naive + dt.timedelta(minutes=extra)
        aware = trial.replace(tzinfo=zone)
        utc = aware.astimezone(dt.timezone.utc)
        back = utc.astimezone(zone).replace(tzinfo=None)
        if (back.year, back.month, back.day, back.hour, back.minute) == (
            trial.year, trial.month, trial.day, trial.hour, trial.minute,
        ):
            return utc
    return None


def _hhmm(value: str | None) -> tuple[int, int] | None:
    if not value or ":" not in value:
        return None
    hour_text, minute_text = value.split(":", 1)
    try:
        hour, minute = int(hour_text), int(minute_text)
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _refresh_word(refresh_type: str | None) -> str:
    if refresh_type == "FULL_REFRESH":
        return "full"
    if refresh_type == "INCREMENTAL_REFRESH":
        return "incremental"
    return (refresh_type or "unspecified").lower()


def _clock(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _minute_label(minute: int) -> str:
    return f":{minute:02d}"


def _phase_minutes(interval: str, minute: int) -> list[int]:
    if interval == "MINUTE15":
        phase = minute % 15
        return [phase, phase + 15, phase + 30, phase + 45]
    if interval == "MINUTE30":
        phase = minute % 30
        return [phase, phase + 30]
    if interval == "HOURLY":
        return [minute]
    return []


def _last_day(year: int, month: int) -> int:
    if month == 12:
        nxt = dt.date(year + 1, 1, 1)
    else:
        nxt = dt.date(year, month + 1, 1)
    return (nxt - dt.timedelta(days=1)).day


def expand_schedule(
    schedule: dict,
    now: dt.datetime,
    newest_scheduled: dt.datetime | None,
    horizon_days: int = 7,
) -> dict:
    """One schedule plus the concrete runs in the next ``horizon_days``.

    ``schedule`` keys: interval, time_of_day, timezone, day_of_week,
    day_of_month, refresh_type, start_after (datetime or None).
    Sub-daily intervals have no minute in the API. The minute comes from
    ``newest_scheduled`` in the schedule's zone. With none, the label says
    "within <interval>" and no concrete runs are returned.
    """
    interval = schedule.get("interval") or ""
    refresh = _refresh_word(schedule.get("refresh_type"))
    zone_name = schedule.get("timezone") or ""
    zone = _zone(zone_name)
    runs_per_day = RUNS_PER_DAY.get(interval, 0.0)
    start_after = schedule.get("start_after")
    result = {
        "definition": "",
        "next_run_at": None,
        "next_run_label": None,
        "runs_per_day": runs_per_day,
        "minute_known": False,
        "minute_source": "unknown",
        "zone_known": zone is not None,
        "planned": [],
    }
    if zone is None:
        shown = zone_name or "(none)"
        result["definition"] = f"Schedule zone {shown} is unknown, {refresh}"
        return result

    clock = _hhmm(schedule.get("time_of_day"))
    phase_minute = None
    if interval in ("MINUTE15", "MINUTE30", "HOURLY"):
        if newest_scheduled is not None:
            phase_minute = newest_scheduled.astimezone(zone).minute
            result["minute_known"] = True
            result["minute_source"] = "scheduled refresh"
        else:
            within = _WITHIN.get(interval, "within the interval")
            every = {
                "MINUTE15": "Every 15 minutes",
                "MINUTE30": "Every 30 minutes",
                "HOURLY": "Hourly",
            }[interval]
            result["definition"] = f"{every}, {refresh}, {within}"
            result["next_run_label"] = within
            return result

    if interval in ("MINUTE15", "MINUTE30") and phase_minute is not None:
        every = "Every 15 minutes" if interval == "MINUTE15" else "Every 30 minutes"
        labels = [_minute_label(m) for m in _phase_minutes(interval, phase_minute)]
        if len(labels) == 1:
            slot_text = labels[0]
        else:
            slot_text = ", ".join(labels[:-1]) + " and " + labels[-1]
        result["definition"] = f"{every}, {refresh}, at {slot_text}"
    elif interval == "HOURLY" and phase_minute is not None:
        result["definition"] = f"Hourly at {_minute_label(phase_minute)}, {refresh}"
    elif interval == "DAILY" and clock is not None:
        result["definition"] = (
            f"Daily {_clock(*clock)} {zone_name}, {refresh}"
        )
        result["minute_known"] = True
        result["minute_source"] = "schedule"
    elif interval == "WEEKLY" and clock is not None:
        day = _WEEKDAY_LABEL.get(schedule.get("day_of_week") or "", "unknown day")
        result["definition"] = f"Weekly {day} {_clock(*clock)} {zone_name}, {refresh}"
        result["minute_known"] = True
        result["minute_source"] = "schedule"
    elif interval == "MONTHLY" and clock is not None:
        day = schedule.get("day_of_month") or ""
        if day == "LAST_DAY_OF_MONTH":
            result["definition"] = (
                f"Monthly, last day, {_clock(*clock)} {zone_name}, {refresh}"
            )
        else:
            result["definition"] = (
                f"Monthly day {day}, {_clock(*clock)} {zone_name}, {refresh}"
            )
        result["minute_known"] = True
        result["minute_source"] = "schedule"
    else:
        result["definition"] = f"{interval or 'Unknown interval'}, {refresh}"
        return result

    horizon = now + dt.timedelta(days=horizon_days)
    search_end = now + dt.timedelta(days=400)
    planned: list[dt.datetime] = []
    next_run: dt.datetime | None = None
    local_now = now.astimezone(zone)
    day = local_now.date()
    last_day = search_end.astimezone(zone).date()
    while day <= last_day:
        instants = _instants_on(
            day, interval, clock, phase_minute, schedule, zone
        )
        for instant in instants:
            if instant <= now:
                continue
            if start_after is not None and instant < start_after:
                continue
            if next_run is None:
                next_run = instant
            if instant <= horizon:
                planned.append(instant)
        if next_run is not None and day > horizon.astimezone(zone).date():
            break
        day += dt.timedelta(days=1)

    result["next_run_at"] = iso(next_run)
    result["planned"] = [iso(item) for item in planned]
    return result


def _instants_on(day, interval, clock, phase_minute, schedule, zone) -> list[dt.datetime]:
    instants: list[dt.datetime] = []
    if interval in ("MINUTE15", "MINUTE30", "HOURLY") and phase_minute is not None:
        minutes = _phase_minutes(interval, phase_minute)
        for hour in range(24):
            for minute in minutes:
                if interval == "HOURLY" and minute != phase_minute:
                    continue
                instant = _wall_to_utc(day.year, day.month, day.day, hour, minute, zone)
                if instant is not None:
                    instants.append(instant)
        return instants
    if clock is None:
        return instants
    hour, minute = clock
    if interval == "WEEKLY":
        wanted = _WEEKDAYS.get(schedule.get("day_of_week") or "")
        if wanted is None or day.weekday() != wanted:
            return instants
    if interval == "MONTHLY":
        day_of_month = schedule.get("day_of_month") or ""
        if day_of_month == "LAST_DAY_OF_MONTH":
            if day.day != _last_day(day.year, day.month):
                return instants
        else:
            try:
                if day.day != int(day_of_month):
                    return instants
            except ValueError:
                return instants
    instant = _wall_to_utc(day.year, day.month, day.day, hour, minute, zone)
    if instant is not None:
        instants.append(instant)
    return instants


def summarize_views(points: list[tuple[dt.datetime, float]], now: dt.datetime) -> dict:
    """DashboardViewCount daily points to the view facts on a dashboard record.

    ``views_year_ago_90`` is None unless the earliest point reaches the start
    of the 90-day window that ended one year ago. Missing days inside a
    covered window count as zero.
    """
    empty = {
        "last": None,
        "history_start": None,
        "views30": 0,
        "views60": 0,
        "views90": 0,
        "views_year_ago_90": None,
        "history_covers_year_ago": False,
    }
    if not points:
        return empty
    history_start = min(ts for ts, _value in points)
    last = None
    for ts, value in points:
        if value > 0 and (last is None or ts > last):
            last = ts

    def covered(start: dt.datetime, end: dt.datetime) -> int:
        total = 0.0
        for ts, value in points:
            if start <= ts <= end:
                total += value
        return int(total)

    year_start = now - dt.timedelta(days=455)
    year_end = now - dt.timedelta(days=365)
    covers = history_start <= year_start
    return {
        "last": last,
        "history_start": history_start,
        "views30": covered(now - dt.timedelta(days=30), now),
        "views60": covered(now - dt.timedelta(days=60), now),
        "views90": covered(now - dt.timedelta(days=90), now),
        "views_year_ago_90": covered(year_start, year_end) if covers else None,
        "history_covers_year_ago": covers,
    }


def build_dashboard_record(
    now: dt.datetime,
    dashboard_id: str,
    name: str | None,
    created_at: dt.datetime | None,
    last_published_at: dt.datetime | None,
    views: dict,
    cloudtrail_at: dt.datetime | None,
    coverage_start: dt.datetime | None,
    dataset_ids: list[str],
    dataset_names: list[str],
    owner_status: str,
    window_days: int,
) -> dict:
    """One dashboard row for ``dataset-lifecycle/dashboards/``."""
    metric_last = views.get("last")
    if metric_last is None and cloudtrail_at is None:
        last_view = None
        last_source = None
    elif cloudtrail_at is None or (
        metric_last is not None and metric_last >= cloudtrail_at
    ):
        last_view = metric_last
        last_source = "view metric"
    else:
        last_view = cloudtrail_at
        last_source = "CloudTrail"

    judged = judge(now, last_view, coverage_start, created_at, window_days)
    history_start = views.get("history_start")
    year_ago = views.get("views_year_ago_90")
    covers = bool(views.get("history_covers_year_ago"))
    deletion = (
        judged["usageState"] == "IDLE" and covers and year_ago == 0
    )
    if metric_last is None and history_start is not None:
        label = f"no view since {history_start.date().isoformat()}"
    elif metric_last is None and history_start is None:
        label = "no view recorded"
    else:
        label = None
    sort_days = judged["daysSince"] if judged["daysSince"] is not None else 999999
    return {
        "ts": iso(now),
        "dashboardId": dashboard_id,
        "dashboardName": name,
        "assetState": "ACTIVE",
        "createdAt": iso(created_at),
        "lastPublishedAt": iso(last_published_at),
        "metricLastViewAt": iso(metric_last),
        "cloudTrailLastViewAt": iso(cloudtrail_at),
        "lastViewAt": iso(last_view),
        "lastViewSource": last_source,
        "lastViewLabel": label,
        "daysSinceLastView": judged["daysSince"],
        "sortDays": sort_days,
        "views30": views.get("views30", 0),
        "views60": views.get("views60", 0),
        "views90": views.get("views90", 0),
        "viewsYearAgo90": year_ago,
        "metricHistoryStart": iso(history_start),
        "historyCoversYearAgo": "true" if covers else "false",
        "datasetIds": ",".join(dataset_ids),
        "datasetNames": ",".join(dataset_names),
        "coverageStart": iso(coverage_start),
        "coverageDays": judged["coverageDays"],
        "usageState": judged["usageState"],
        "unknownReason": judged["unknownReason"],
        "recheckAfter": judged["recheckAfter"],
        "ownerStatus": owner_status,
        "deletionCandidate": "true" if deletion else "false",
    }


def dashboard_tombstone(now: dt.datetime, dashboard_id: str, name: str | None) -> dict:
    return {
        "ts": iso(now),
        "dashboardId": dashboard_id,
        "dashboardName": name,
        "assetState": "DELETED",
    }


def observed_count(read_ok: bool, count: int) -> int | None:
    """None when the read failed. Zero is a real empty result."""
    if not read_ok:
        return None
    return count


def forget_refresh_hits(
    hits: dict,
    windows_by_dataset: dict,
    floor: dt.datetime,
    scan_time: dt.datetime,
) -> None:
    """Drop SPICE query hits that fall inside a refresh. Hits older than floor stay.

    ``hits`` is ``{datasetId: {"at": iso, ...}}``. Window ``created`` is a
    datetime or an ISO string.
    """
    for dataset_id in list(hits):
        hit = hits.get(dataset_id)
        at_text = hit.get("at") if isinstance(hit, dict) else None
        if not at_text:
            continue
        try:
            at = dt.datetime.fromisoformat(str(at_text).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at < floor:
            continue
        windows = []
        for item in windows_by_dataset.get(dataset_id, []):
            created = item.get("created")
            if isinstance(created, str):
                try:
                    created = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
                except ValueError:
                    continue
            if isinstance(created, dt.datetime):
                windows.append({"created": created, "duration": item.get("duration")})
        if not query_counts_as_use("SPICE", at, windows, scan_time):
            hits.pop(dataset_id, None)


def planned_runs(expanded: dict) -> list:
    """Concrete runs to write. An unknown zone keeps the schedule row and writes none."""
    if not expanded.get("zone_known"):
        return []
    return list(expanded.get("planned") or [])
