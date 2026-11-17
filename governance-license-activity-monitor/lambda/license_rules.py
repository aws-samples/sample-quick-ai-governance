"""Pure license and activity rules. No AWS clients.

The collector imports this module. Tests import it directly.
"""

from __future__ import annotations

import datetime as dt

DEFAULT_WINDOW_DAYS = 60
OVERAGE_USD_PER_HOUR = 3.0
ESSENTIALS_BONUS_UNTIL = dt.date(2027, 2, 1)
STALE_FEED_DAYS = 2
AI_HISTORY_DAYS = 90

# First match wins. A sign-in is not in this table and never counts as activity.
EVENT_CATEGORIES = {
    "authoring": {
        "CreateAnalysis", "UpdateAnalysis", "DeleteAnalysis",
        "CreateDashboard", "UpdateDashboard", "DeleteDashboard",
        "CreateDataSet", "UpdateDataSet", "DeleteDataSet",
        "CreateTopic", "UpdateTopic",
        "CreateFlow", "UpdateFlow",
        "CreateAutomation", "UpdateAutomation",
    },
    "AI": {
        "InvokeAgent", "InvokeAgentInternal", "StartResearch",
        "CreateChat", "SendMessage",
    },
    "administration": {
        "CreateUser", "UpdateUser", "DeleteUser", "RegisterUser",
        "CreateGroup", "UpdateGroup", "DeleteGroup",
        "CreateGroupMembership", "DeleteGroupMembership",
        "UpdateDashboardPermissions", "UpdateDataSetPermissions",
    },
    "viewing": set(),
}

TIERS = {
    "ESSENTIALS": {"price": 3.0, "hours": 1.0, "authoring": False},
    "PROFESSIONAL": {"price": 20.0, "hours": 8.0, "authoring": False},
    "ENTERPRISE": {"price": 40.0, "hours": 18.0, "authoring": True},
}
TIER_ORDER = ("ESSENTIALS", "PROFESSIONAL", "ENTERPRISE")

ROLE_TIER = {
    "READER": "ESSENTIALS",
    "READER_PRO": "PROFESSIONAL",
    "AUTHOR": "ENTERPRISE",
    "AUTHOR_PRO": "ENTERPRISE",
    "ADMIN": "ENTERPRISE",
    "ADMIN_PRO": "ENTERPRISE",
}

ROLE_PRICE_UNKNOWN = {"BI_AUTHOR"}

PATHS = {
    ("AUTHOR_PRO", "ESSENTIALS"): "Author Pro > Author > Restricted Reader > Reader Pro",
    ("AUTHOR_PRO", "PROFESSIONAL"): "Author Pro > Author > Restricted Reader > Reader Pro",
    ("READER_PRO", "ESSENTIALS"): "Reader Pro > Reader",
}


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_cursor(value: dt.datetime | None) -> str | None:
    """Cursor time with microseconds. Whole seconds would drop the rest of that second."""
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def classify_event(event_source: str | None, event_name: str | None) -> str | None:
    """One category, or None for a sign-in and for anything that is not Quick."""
    if event_source == "signin.amazonaws.com":
        return None
    if event_source != "quicksight.amazonaws.com" or not event_name:
        return None
    for category in ("authoring", "AI", "administration"):
        if event_name in EVENT_CATEGORIES[category]:
            return category
    return "viewing"


def _match_role_session(users: list[dict], role_name: str, session: str) -> str | None:
    """Match role and session exactly, or the one Quick user on that role.

    A session suffix is not a match: FinanceRole/bob does not own an
    AdminRole/bob event.
    """
    if not role_name or not session:
        return None
    iam_users = [
        user for user in users
        if user.get("identityType") == "IAM" and user.get("userArn")
    ]
    exact = [
        user for user in iam_users
        if str(user.get("userName") or "") == f"{role_name}/{session}"
    ]
    if len(exact) == 1:
        return exact[0]["userArn"]
    on_role = [
        user for user in iam_users
        if str(user.get("userName") or "").split("/", 1)[0] == role_name
    ]
    if len(on_role) == 1:
        return on_role[0]["userArn"]
    return None


def attribute_user(identity: dict | None, users: list[dict]) -> tuple[str | None, str]:
    """Match a CloudTrail userIdentity to a ListUsers ARN.

    Returns (userArn or None, note). The note is "untested" for an IAM Identity
    Center match, because this account has no such user. An identity with no
    user fields returns None.
    """
    if not identity:
        return None, ""
    kind = identity.get("type")
    user_name = identity.get("userName") or ""
    arn = identity.get("arn") or ""
    on_behalf = (identity.get("onBehalfOf") or {}).get("userId")

    if kind == "Directory" and user_name:
        for user in users:
            if user.get("identityType") == "QUICK" and user.get("userName") == user_name:
                return user["userArn"], ""
    if kind == "AssumedRole" and arn:
        session = arn.rsplit("/", 1)[-1]
        role_name = arn.split("/")[-2] if arn.count("/") >= 2 else ""
        matched = _match_role_session(users, role_name, session)
        if matched:
            return matched, ""
    if kind == "Directory" and "/iam/" in arn:
        tail = arn.split("/iam/", 1)[-1].replace(":", "/")
        role_name, _sep, session = tail.partition("/")
        matched = _match_role_session(users, role_name, session)
        if matched:
            return matched, ""
    if on_behalf:
        for user in users:
            if user.get("identityCenterId") == on_behalf:
                return user["userArn"], "untested"
    return None, ""


OWNERSHIP_STORED_CHARS = 80


def owns(principal: str, user_name: str) -> bool:
    """True when an ownership principal is this Quick user.

    The Orphaned Assets snapshot stores the last 80 characters of
    ``default/{userName}``. The part after the namespace has to be the whole
    user name, so ``bob`` does not match ``default/AdminRole/bob``.
    """
    if not principal or not user_name:
        return False
    if principal == user_name:
        return True
    namespaced = "default/" + user_name
    if principal == namespaced or principal == namespaced[-OWNERSHIP_STORED_CHARS:]:
        return True
    if ":user/" in principal:
        _namespace, sep, name = principal.split(":user/", 1)[1].partition("/")
        if sep and name == user_name:
            return True
    return False


def later_activity(cloudtrail_at: dt.datetime | None, hours_at: dt.datetime | None) -> dt.datetime | None:
    """The later of the two feeds. Either one alone still counts."""
    times = [value for value in (cloudtrail_at, hours_at) if value is not None]
    return max(times) if times else None


def judge_user(
    now: dt.datetime,
    last_activity: dt.datetime | None,
    coverage_start: dt.datetime | None,
    active: bool,
    window_days: int,
) -> dict:
    """ACTIVE, IDLE, UNKNOWN, or PENDING for one user.

    PENDING wins: ListUsers Active is false. A sign-in is not last_activity.
    """
    coverage_days = (now - coverage_start).days if coverage_start is not None else 0
    days_since = (now - last_activity).days if last_activity is not None else None
    if not active:
        state, reason, recheck = "PENDING", None, None
    elif last_activity is not None and days_since <= window_days:
        state, reason, recheck = "ACTIVE", None, None
    elif coverage_start is not None and coverage_days >= window_days:
        state, reason, recheck = "IDLE", None, None
    else:
        state = "UNKNOWN"
        reason = f"coverage is {coverage_days} days"
        recheck = (
            (coverage_start + dt.timedelta(days=window_days)).date().isoformat()
            if coverage_start is not None else None
        )
    return {
        "usageState": state,
        "daysSince": days_since,
        "coverageDays": coverage_days,
        "unknownReason": reason,
        "recheckAfter": recheck,
    }


def removal_readiness(
    role: str | None,
    state: str,
    assets_owned_alone: int | None,
    ownership_present: bool,
) -> str:
    if role in ("ADMIN", "ADMIN_PRO"):
        return "Administrator: review by hand"
    if state == "UNKNOWN":
        return "Not enough history"
    if not ownership_present:
        return "Ownership unknown"
    if assets_owned_alone and assets_owned_alone > 0:
        return "Transfer ownership first"
    if state in ("IDLE", "PENDING"):
        return "Ready for review"
    return "In use"


def included_hours(tier: str, now: dt.datetime) -> float:
    base = TIERS[tier]["hours"]
    if tier == "ESSENTIALS" and now.date() < ESSENTIALS_BONUS_UNTIL:
        return 3.0
    return base


def _month_start(day: dt.date) -> dt.date:
    return day.replace(day=1)


def peak_months(now: dt.datetime) -> list[dt.date]:
    """The last three full months, newest first. The current month is excluded."""
    cursor = _month_start(now.date())
    months: list[dt.date] = []
    for _ in range(3):
        cursor = (cursor - dt.timedelta(days=1)).replace(day=1)
        months.append(cursor)
    return months


def stamp_keep_from(now: dt.datetime) -> dt.date:
    """Oldest hour stamp that can change the peak or this user's confidence."""
    history = now.date() - dt.timedelta(days=AI_HISTORY_DAYS)
    return min(min(peak_months(now)), history)


def keep_hour_rows(rows: list, now: dt.datetime) -> list:
    cutoff = stamp_keep_from(now).isoformat()
    return [row for row in rows if str(row[0])[:10] >= cutoff]


CATEGORY_ORDER = ("authoring", "AI", "administration", "viewing")


def cloudtrail_status(coverage_started: bool, events_read: bool) -> str:
    """OK once the 90-day backfill is stamped. SHORT means events were read and coverage is still open."""
    if coverage_started:
        return "OK"
    if events_read:
        return "SHORT"
    return "MISSING"


def remember_category(entries: list[dict], arn: str, at: str, category: str) -> None:
    """Latest time this user was seen in this category. A later event does not erase an earlier one."""
    if not arn or not at or not category:
        return
    for entry in entries:
        if entry.get("arn") == arn and entry.get("category") == category:
            if at > entry.get("at", ""):
                entry["at"] = at
            return
    entries.append({"arn": arn, "at": at, "category": category})


def categories_in_window(entries: list[dict], arn: str, window_floor: str) -> list[str]:
    found = {
        entry.get("category")
        for entry in entries
        if entry.get("arn") == arn and entry.get("category") and entry.get("at", "") >= window_floor
    }
    return [name for name in CATEGORY_ORDER if name in found]


def ai_days_in_window(days: list[str], window_floor: str) -> list[str]:
    """Dates after the cutoff date. The cutoff date itself is outside the window."""
    cutoff = (window_floor or "")[:10]
    return sorted(day for day in days if day > cutoff)


def remember_authoring(entries: list[dict], arn: str, at: str, name: str) -> None:
    """One row per user, time, and event name. A re-scan of the same event does not add another."""
    entry = {"arn": arn, "at": at, "name": name}
    if entry not in entries:
        entries.append(entry)


def saving_from_month(now: dt.datetime) -> str:
    """The first month that starts after the change. Billing uses the highest tier held."""
    year, month = now.year, now.month + 1
    if month == 13:
        year, month = year + 1, 1
    return f"{year:04d}-{month:02d}"


def _empty_fit(current: str | None, role: str, hours_confidence: str, now: dt.datetime) -> dict:
    return {
        "currentTier": current,
        "listPriceUsd": None if role in ROLE_PRICE_UNKNOWN else (TIERS[current]["price"] if current else None),
        "candidateTier": None,
        "candidatePriceUsd": None,
        "savingUsd": None,
        "changePath": None,
        "savingFrom": saving_from_month(now),
        "confidence": hours_confidence,
        "currentCostUsd": None,
        "tierCosts": {tier: None for tier in TIER_ORDER},
    }


def license_fit(
    now: dt.datetime,
    role: str,
    identity_type: str,
    state: str,
    peak_hours: float | None,
    authoring_count: int,
    overage_allowed: bool,
    hours_confidence: str,
) -> dict:
    """Cheapest tier that covers the use, or no candidate.

    Essentials and Professional are proposed only when their included hours
    cover the peak, unless this is the user's current tier and a limit allows
    overage. Enterprise can exceed its allowance when a limit allows overage,
    and needs no limit to be the current tier of an Enterprise role. IDLE users
    and administrators are not candidates. Anything other than HIGH confidence
    returns no saving. tierCosts still prices every tier from this one call.
    """
    current = ROLE_TIER.get(role)
    result = _empty_fit(current, role, hours_confidence, now)
    if role in ROLE_PRICE_UNKNOWN or current is None:
        result["confidence"] = "LOW"
        return result
    if peak_hours is None:
        return result

    def cost_of(tier: str) -> float | None:
        included = included_hours(tier, now)
        if authoring_count > 0 and not TIERS[tier]["authoring"]:
            return None
        if peak_hours <= included:
            return TIERS[tier]["price"]
        allow = overage_allowed and (tier == current or tier == "ENTERPRISE")
        if not allow:
            return None
        return TIERS[tier]["price"] + (peak_hours - included) * OVERAGE_USD_PER_HOUR

    costs = {tier: cost_of(tier) for tier in TIER_ORDER}
    result["tierCosts"] = {
        tier: None if cost is None else round(cost, 2) for tier, cost in costs.items()
    }
    current_cost = costs[current]
    result["currentCostUsd"] = None if current_cost is None else round(current_cost, 2)
    if state != "ACTIVE" or role in ("ADMIN", "ADMIN_PRO") or hours_confidence != "HIGH":
        return result
    available = [(tier, cost) for tier, cost in costs.items() if cost is not None]
    if not available:
        return result
    tier, cost = min(available, key=lambda item: (item[1], TIER_ORDER.index(item[0])))
    saving = None if current_cost is None else round(current_cost - cost, 2)
    if identity_type == "IDENTITY_CENTER" and tier != current:
        path = "change the IAM Identity Center group"
    else:
        path = PATHS.get((role, tier), "")
    result.update({
        "listPriceUsd": TIERS[current]["price"],
        "candidateTier": tier,
        "candidatePriceUsd": round(cost, 2),
        "savingUsd": saving,
        "changePath": path,
    })
    return result


def peak_of_full_months(stamps: list[tuple[dt.datetime, float]], now: dt.datetime) -> float | None:
    """Peak monthly hours across the last 3 full months. The current month is excluded."""
    if not stamps:
        return None
    totals = {month: 0.0 for month in peak_months(now)}
    for stamp, hours in stamps:
        month = _month_start(stamp.date())
        if month in totals:
            totals[month] += hours
    return max(totals.values())


def gap_days(ai_days: set[dt.date], feed_days: set[dt.date]) -> list[str]:
    """Days with an AI event in CloudTrail and no Agent Hours record."""
    return [day.isoformat() for day in sorted(ai_days - feed_days)]


def peak_for_read(
    stamps: list[tuple[dt.datetime, float]], now: dt.datetime, read_complete: bool,
) -> float | None:
    """Peak hours, or None until this user's objects have all been read.

    A finished read with no rows is a real zero. A partial read is not.
    """
    if not read_complete:
        return None
    if not stamps:
        return 0.0
    return peak_of_full_months(stamps, now)


def hours_confidence(
    feed_status: str,
    read_complete: bool,
    coverage_days: int,
    window_days: int,
    user_ai_days: set[dt.date],
    user_hour_days: set[dt.date],
    feed_newest: dt.datetime | None,
    now: dt.datetime,
) -> str:
    """HIGH only when this user's own hours were fully read.

    Another user's missing day does not change this result. The account-wide
    gap list is a separate evidence row.
    """
    if feed_status != "OK":
        return "not available"
    if not read_complete or coverage_days < window_days:
        return "LOW"
    if user_ai_days - user_hour_days:
        return "LOW"
    if feed_newest is not None and (now - feed_newest).days >= STALE_FEED_DAYS:
        if any((now.date() - day).days <= STALE_FEED_DAYS for day in user_ai_days):
            return "LOW"
    return "HIGH"


def next_incremental(
    high: dt.datetime, end: dt.datetime, outcome: str, oldest: dt.datetime | None,
) -> tuple[dt.datetime, dt.datetime | None]:
    """Where the next newest-first scan starts.

    A finished window moves the high water to its end. A capped window has
    read (oldest, end] and missed [high, oldest), so the high water stays and
    the next end is that oldest event. It does not jump to now.
    """
    if outcome == "floor":
        return end, None
    if oldest is None or oldest <= high or oldest >= end:
        return high, end
    return high, oldest


def next_backfill(
    cursor_end: dt.datetime, outcome: str, oldest: dt.datetime | None, floor: dt.datetime,
) -> tuple[dt.datetime, bool]:
    """Move the backfill end backward. A capped scan must not leave it at now."""
    if outcome == "floor":
        return floor, True
    if oldest is None or oldest >= cursor_end:
        return cursor_end, False
    if oldest <= floor:
        return floor, True
    return oldest, False


def cloudtrail_page_cap(region_count: int, interval_seconds: float, budget_seconds: float = 700) -> int:
    """Pages per scan so two scans in every Region stay inside the time budget.

    The budget is 700s, under the 900s Lambda timeout, leaving the Quick and
    Agent Hours calls room to finish.
    """
    scans = max(region_count, 1) * 2
    return max(10, int(budget_seconds / (interval_seconds * scans)))
