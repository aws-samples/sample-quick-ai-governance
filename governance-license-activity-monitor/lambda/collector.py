"""
AWS Quick — License and Activity Monitor collector.

One daily pass. It reads users, CloudTrail, and the optional Agent Hours,
ownership, limit and index inputs. It never calls UpdateUser, DeleteUser,
or a permissions API.

Last observed activity is the later of the newest attributed CloudTrail event
and the newest Agent Hours stamp. It is not the console's Last active. A
sign-in fills last sign-in only.
"""

from __future__ import annotations

import gzip
import json
import os
import time
import datetime as dt
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

import license_rules as rules

ACCOUNT_ID = os.environ["QS_ACCOUNT_ID"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
STATE_BUCKET = os.environ["STATE_BUCKET"]
STATE_PREFIX = os.environ.get("STATE_PREFIX", "license-activity/").strip("/")
SNAPSHOT_BUCKET = os.environ.get("SNAPSHOT_BUCKET") or STATE_BUCKET
SNAPSHOT_PREFIX = os.environ.get("SNAPSHOT_PREFIX", "license-activity/").strip("/")
IDENTITY_REGION = os.environ.get("IDENTITY_REGION", "")
ACTIVITY_REGIONS = [
    item.strip() for item in os.environ.get("ACTIVITY_REGIONS", "").split(",") if item.strip()
]
UNUSED_THRESHOLD_DAYS = int(os.environ.get("UNUSED_THRESHOLD_DAYS", "60"))
CT_LOOKBACK_DAYS = int(os.environ.get("CT_LOOKBACK_DAYS", "90"))
LOG_RETENTION_DAYS = int(os.environ.get("LOG_RETENTION_DAYS", "90"))
AGENT_HOURS_PREFIX = os.environ.get("AGENT_HOURS_PREFIX", "agent-hours/").strip("/") + "/"
OWNERSHIP_KEY = os.environ.get("OWNERSHIP_KEY", "orphaned-assets/latest-ownership.jsonl")

QS_MIN_INTERVAL_SECONDS = 0.22
CT_MIN_INTERVAL_SECONDS = 0.55
MAX_CT_PAGES = 500
MAX_HOUR_OBJECTS = 200
IDENTITY_REGION_RE_TEXT = "identity region is "

_RETRY = Config(retries={"max_attempts": 8, "mode": "adaptive"})
quicksight = boto3.client("quicksight", region_name=REGION, config=_RETRY)
cloudtrail = boto3.client("cloudtrail", region_name=REGION, config=_RETRY)
s3 = boto3.client("s3", region_name=REGION, config=_RETRY)
cloudwatch = boto3.client("cloudwatch", region_name=REGION, config=_RETRY)


class _Pacer:
    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._last = 0.0

    def wait(self) -> None:
        delta = self._interval - (time.monotonic() - self._last)
        if delta > 0:
            time.sleep(delta)
        self._last = time.monotonic()


qs_pacer = _Pacer(QS_MIN_INTERVAL_SECONDS)
ct_pacer = _Pacer(CT_MIN_INTERVAL_SECONDS)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None) -> str | None:
    return rules.iso(value)


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _state_key(key: str) -> str:
    return f"{STATE_PREFIX}/{key}" if STATE_PREFIX else key


def load_state(key: str) -> dict:
    try:
        response = s3.get_object(Bucket=STATE_BUCKET, Key=_state_key(key))
        return json.loads(response["Body"].read())
    except ClientError as error:
        if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return {}
        raise


def save_state(key: str, obj: dict) -> None:
    s3.put_object(
        Bucket=STATE_BUCKET, Key=_state_key(key),
        Body=json.dumps(obj, default=str).encode("utf-8"),
        ContentType="application/json",
    )


def write_snapshot(kind: str, records: list[dict]) -> None:
    now = _utcnow()
    body = "\n".join(json.dumps(row, default=str) for row in records).encode("utf-8")
    dated = f"{SNAPSHOT_PREFIX}/{kind}/dt={now:%Y-%m-%d}/run-{now:%H%M%S}.jsonl"
    for key in (dated, f"{SNAPSHOT_PREFIX}/latest-{kind}.jsonl"):
        s3.put_object(
            Bucket=SNAPSHOT_BUCKET, Key=key, Body=body,
            ContentType="application/x-ndjson",
        )


def resolve_identity_client() -> tuple[Any, str]:
    region = IDENTITY_REGION
    state = load_state("state/identity.json")
    if not region:
        region = state.get("identityRegion", "")
    if not region:
        try:
            qs_pacer.wait()
            quicksight.list_users(AwsAccountId=ACCOUNT_ID, Namespace="default")
            region = REGION
        except ClientError as error:
            text = str(error)
            marker = text.find(IDENTITY_REGION_RE_TEXT)
            if marker < 0:
                raise
            region = text[marker + len(IDENTITY_REGION_RE_TEXT):].split()[0].strip(" .")
        state["identityRegion"] = region
        save_state("state/identity.json", state)
    if region == REGION:
        return quicksight, region
    return boto3.client("quicksight", region_name=region, config=_RETRY), region


def list_users(identity_client: Any) -> list[dict]:
    users = []
    token = None
    while True:
        qs_pacer.wait()
        kwargs: dict[str, Any] = {"AwsAccountId": ACCOUNT_ID, "Namespace": "default"}
        if token:
            kwargs["NextToken"] = token
        page = identity_client.list_users(**kwargs)
        for item in page.get("UserList", []):
            kind = item.get("IdentityType") or ""
            if kind == "IAM":
                identity_type = "IAM"
            elif kind in ("IAM_IDENTITY_CENTER", "IDENTITY_CENTER"):
                identity_type = "IDENTITY_CENTER"
            else:
                identity_type = "QUICK"
            users.append({
                "userArn": item.get("Arn"),
                "userName": item.get("UserName"),
                "role": item.get("Role"),
                "active": bool(item.get("Active")),
                "identityType": identity_type,
                "identityCenterId": item.get("ExternalLoginId"),
            })
        token = page.get("NextToken")
        if not token:
            break
    return users


def _apply_event(state: dict, users: list[dict], event_time: dt.datetime, detail: dict, region: str) -> None:
    identity = detail.get("userIdentity") or {}
    user_arn, note = rules.attribute_user(identity, users)
    if user_arn is None:
        return
    source = detail.get("eventSource")
    name = detail.get("eventName")
    category = rules.classify_event(source, name)
    at = _iso(event_time)
    if category is None and source == "signin.amazonaws.com":
        previous = state["signins"].get(user_arn)
        if at and (previous is None or at > previous):
            state["signins"][user_arn] = at
        return
    if category is None:
        return
    hit = state["hits"].get(user_arn)
    if at and (hit is None or at > hit.get("at", "")):
        state["hits"][user_arn] = {
            "at": at, "category": category, "region": region,
            "name": name, "note": note, "source": "CloudTrail",
        }
    if category and event_time is not None:
        rules.remember_category(
            state.setdefault("categories", []), user_arn, rules.iso_cursor(event_time) or "", category)
    if category == "authoring" and event_time is not None:
        rules.remember_authoring(
            state["authoring"], user_arn, rules.iso_cursor(event_time) or "", name or "")
    if category == "AI" and at:
        days = state.setdefault("aiByUser", {}).setdefault(user_arn, [])
        day = at[:10]
        if day not in days:
            days.append(day)


def _scan_window(
    region: str, start: dt.datetime, end: dt.datetime, users: list[dict], state: dict,
    event_source: str = "quicksight.amazonaws.com", page_cap: int = MAX_CT_PAGES,
) -> tuple[str, dt.datetime | None]:
    """Page one window, newest first. Returns ('floor' or 'capped', oldest event).

    This does not move a cursor. The sign-in scan and a capped scan must not
    be able to stamp the backfill as finished.
    """
    client = cloudtrail if region == REGION else boto3.client("cloudtrail", region_name=region, config=_RETRY)
    token = None
    pages = 0
    oldest: dt.datetime | None = None
    while pages < page_cap:
        ct_pacer.wait()
        kwargs: dict[str, Any] = {
            "LookupAttributes": [{"AttributeKey": "EventSource", "AttributeValue": event_source}],
            "StartTime": start, "EndTime": end, "MaxResults": 50,
        }
        if token:
            kwargs["NextToken"] = token
        page = client.lookup_events(**kwargs)
        pages += 1
        events = page.get("Events", [])
        if not events:
            return "floor", oldest
        for event in events:
            event_time = event.get("EventTime")
            if isinstance(event_time, dt.datetime):
                oldest = event_time if oldest is None or event_time < oldest else oldest
                try:
                    detail = json.loads(event.get("CloudTrailEvent") or "{}")
                except json.JSONDecodeError:
                    continue
                _apply_event(state, users, event_time, detail, region)
        token = page.get("NextToken")
        if not token:
            return "floor", oldest
    return "capped", oldest


def collect_cloudtrail(users: list[dict], regions: list[str], state: dict, now: dt.datetime) -> None:
    floor = now - dt.timedelta(days=CT_LOOKBACK_DAYS)
    page_cap = rules.cloudtrail_page_cap(len(regions), CT_MIN_INTERVAL_SECONDS)
    state.setdefault("incrementalEnd", {})
    for region in regions:
        high = _parse_iso(state["highWater"].get(region)) or floor
        pending = _parse_iso(state["incrementalEnd"].get(region))
        window_end = pending if pending is not None and pending > high else now
        outcome, oldest = _scan_window(
            region, high, window_end, users, state, page_cap=page_cap)
        new_high, new_pending = rules.next_incremental(high, window_end, outcome, oldest)
        state["highWater"][region] = rules.iso_cursor(new_high)
        if new_pending is None:
            state["incrementalEnd"].pop(region, None)
        else:
            state["incrementalEnd"][region] = rules.iso_cursor(new_pending)
        # Sign-in is a separate small scan. Its result does not move either cursor.
        _scan_window(region, high, now, users, state, "signin.amazonaws.com", page_cap=5)
        if not state["backfillDone"].get(region):
            cursor = _parse_iso(state["backfillCursor"].get(region)) or now
            outcome, oldest = _scan_window(
                region, floor, cursor, users, state, page_cap=page_cap)
            new_cursor, done = rules.next_backfill(cursor, outcome, oldest, floor)
            state["backfillCursor"][region] = rules.iso_cursor(new_cursor)
            state["backfillDone"][region] = done
        save_state("state/activity.json", state)
    if regions and all(state["backfillDone"].get(region) for region in regions):
        state["coverageStart"] = _iso(floor)


def _read_object(key: str) -> bytes:
    body = s3.get_object(Bucket=STATE_BUCKET, Key=key)["Body"].read()
    if key.endswith(".gz"):
        body = gzip.decompress(body)
    return body


def read_agent_hours(state: dict, now: dt.datetime) -> dict:
    """Incremental read of agent-hours objects. Missing prefix is not a failure."""
    seen = set(state.get("hourKeys") or [])
    keys: list[str] = []
    token = None
    try:
        while True:
            kwargs: dict[str, Any] = {"Bucket": STATE_BUCKET, "Prefix": AGENT_HOURS_PREFIX}
            if token:
                kwargs["ContinuationToken"] = token
            page = s3.list_objects_v2(**kwargs)
            keys.extend(item["Key"] for item in page.get("Contents", []))
            token = page.get("NextContinuationToken")
            if not token:
                break
    except ClientError as error:
        print(f"WARN agent hours list failed: {error}")
        return {"status": "MISSING", "newest": None, "days": set(), "by_user": {}, "complete": False}
    fresh = [key for key in keys if key not in seen and not key.endswith("/")]
    if not keys:
        return {"status": "NOT_DEPLOYED", "newest": None, "days": set(), "by_user": {}, "complete": False}
    serial: dict[str, list] = state.get("hourStamps") or {}
    newest = _parse_iso(state.get("hoursNewest"))
    days = set(state.get("hourDays") or [])
    read_errors = False
    for key in fresh[:MAX_HOUR_OBJECTS]:
        try:
            raw = _read_object(key)
        except ClientError as error:
            print(f"WARN agent hours object failed {key}: {error}")
            read_errors = True
            continue
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "user_arn" not in row and isinstance(row.get("message"), str):
                try:
                    row = json.loads(row["message"])
                except json.JSONDecodeError:
                    continue
            user_arn = row.get("user_arn")
            hours = row.get("usage_hours")
            stamp = row.get("event_timestamp")
            if not user_arn or hours is None or stamp is None:
                continue
            when = dt.datetime.fromtimestamp(
                float(stamp) / (1000 if float(stamp) > 10_000_000_000 else 1),
                tz=dt.timezone.utc,
            )
            serial.setdefault(user_arn, []).append([_iso(when), float(hours)])
            days.add(when.date().isoformat())
            if newest is None or when > newest:
                newest = when
        seen.add(key)
    for arn, rows in list(serial.items()):
        kept = rules.keep_hour_rows(rows, now)
        if kept:
            serial[arn] = kept
        else:
            serial.pop(arn, None)
    cutoff = (_utcnow().date() - dt.timedelta(days=CT_LOOKBACK_DAYS)).isoformat()
    state["hourKeys"] = sorted(seen)
    state["hourStamps"] = serial
    state["hourDays"] = sorted(day for day in days if day >= cutoff)
    state["hoursNewest"] = _iso(newest)
    parsed: dict[str, list[tuple[dt.datetime, float]]] = {}
    for arn, rows in serial.items():
        pairs = []
        for stamp, value in rows:
            when = _parse_iso(stamp)
            if when is not None:
                pairs.append((when, float(value)))
        parsed[arn] = pairs
    complete = not read_errors and len(fresh) <= MAX_HOUR_OBJECTS
    return {
        "status": "OK", "newest": newest, "days": set(state["hourDays"]),
        "by_user": parsed, "complete": complete,
    }


def read_ownership() -> dict[str, dict] | None:
    try:
        body = s3.get_object(Bucket=STATE_BUCKET, Key=OWNERSHIP_KEY)["Body"].read().decode("utf-8")
    except ClientError as error:
        print(f"WARN ownership snapshot unavailable: {error}")
        return None
    owned: dict[str, dict] = {}
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        principals = row.get("ownerPrincipals") or []
        alone = row.get("findingType") == "SINGLE_ACTIVE_OWNER"
        for item in principals:
            principal = item.get("principal") or ""
            if not principal:
                continue
            bucket = owned.setdefault(principal, {"owned": 0, "alone": 0})
            bucket["owned"] += 1
            if alone and len(principals) == 1:
                bucket["alone"] += 1
    return owned


def _ownership_for(user: dict, index: dict[str, dict] | None) -> tuple[int | None, int | None]:
    if index is None:
        return None, None
    name = user.get("userName") or ""
    matched = {"owned": 0, "alone": 0}
    for principal, counts in index.items():
        if rules.owns(principal, name):
            matched["owned"] += counts["owned"]
            matched["alone"] += counts["alone"]
    return matched["owned"], matched["alone"]


def read_limits(identity_client: Any, users: list[dict]) -> dict[str, bool]:
    """True when a limit profile lets that user exceed the included hours."""
    allowed: dict[str, bool] = {}
    if not hasattr(identity_client, "batch_describe_user_limits"):
        print("WARN BatchDescribeUserLimits is not in this boto3")
        return allowed
    for start in range(0, len(users), 100):
        chunk = users[start:start + 100]
        qs_pacer.wait()
        try:
            response = identity_client.batch_describe_user_limits(
                accountId=ACCOUNT_ID,
                users=[{"namespace": "default", "userName": user["userName"]} for user in chunk],
                resourceTypes=["AGENT_HOURS"],
            )
        except (ClientError, Exception) as error:
            print(f"WARN BatchDescribeUserLimits failed: {error}")
            return allowed
        for entry in response.get("UserLimits") or response.get("userLimits") or []:
            name = entry.get("UserName") or entry.get("userName")
            limits = entry.get("EffectiveLimits") or entry.get("effectiveLimits") or []
            allowed[name] = any(
                (item.get("ResourceType") or item.get("resourceType")) == "AGENT_HOURS"
                and (item.get("LimitValue") or item.get("limitValue") or 0) > 0
                for item in limits
            )
    return allowed


def read_index(regions: list[str]) -> dict:
    for region in regions:
        client = quicksight if region == REGION else boto3.client("quicksight", region_name=region, config=_RETRY)
        if not hasattr(client, "list_users_index_capacity"):
            continue
        qs_pacer.wait()
        try:
            client.list_users_index_capacity(awsAccountId=ACCOUNT_ID, namespace="default")
        except (ClientError, Exception) as error:
            text = str(error)
            if "No active index" in text:
                continue
            print(f"WARN ListUsersIndexCapacity failed in {region}: {error}")
            return {"status": "MISSING", "region": region}
        return {"status": "OK", "region": region}
    return {"status": "MISSING", "region": None}


def run() -> dict:
    started = time.monotonic()
    api_errors = 0
    now = _utcnow()
    now_iso = _iso(now)
    identity_client, identity_region = resolve_identity_client()
    regions = ACTIVITY_REGIONS or list(dict.fromkeys([identity_region, REGION]))
    try:
        users = list_users(identity_client)
    except ClientError as error:
        print(f"WARN ListUsers failed: {error}")
        raise
    state = load_state("state/activity.json")
    state.setdefault("hits", {})
    state.setdefault("signins", {})
    state.setdefault("authoring", [])
    state.setdefault("aiByUser", {})
    state.setdefault("highWater", {})
    state.setdefault("backfillCursor", {})
    state.setdefault("backfillDone", {})
    state.setdefault("incrementalEnd", {})
    if state.get("attributionVersion") != 4:
        state["hits"] = {}
        state["signins"] = {}
        state["aiByUser"] = {}
        state["authoring"] = []
        state["highWater"] = {}
        state["backfillCursor"] = {}
        state["backfillDone"] = {}
        state["incrementalEnd"] = {}
        state["coverageStart"] = None
        state["hourKeys"] = []
        state["hourStamps"] = {}
        state["hourDays"] = []
        state["hoursNewest"] = None
        state.pop("aiDays", None)
        state["attributionVersion"] = 4
    try:
        collect_cloudtrail(users, regions, state, now)
    except ClientError as error:
        api_errors += 1
        print(f"WARN CloudTrail scan failed: {error}")
    window_floor = rules.iso_cursor(now - dt.timedelta(days=UNUSED_THRESHOLD_DAYS))
    state["authoring"] = [item for item in state["authoring"] if item.get("at", "") >= (window_floor or "")]
    ai_cutoff = (now - dt.timedelta(days=rules.AI_HISTORY_DAYS)).date().isoformat()
    state["aiByUser"] = {
        arn: sorted({day for day in days if day >= ai_cutoff})
        for arn, days in state.get("aiByUser", {}).items()
        if any(day >= ai_cutoff for day in days)
    }
    state.setdefault("categories", [])
    state["categories"] = [
        item for item in state["categories"] if item.get("at", "") >= (window_floor or "")
    ]
    for item in state["authoring"]:
        rules.remember_category(state["categories"], item.get("arn", ""), item.get("at", ""), "authoring")
    for arn, hit in state["hits"].items():
        at = hit.get("at") or ""
        if at >= (window_floor or "") and hit.get("category"):
            rules.remember_category(state["categories"], arn, at, hit["category"])
    for arn, days in state["aiByUser"].items():
        recent = rules.ai_days_in_window(days, window_floor or "")
        if recent:
            rules.remember_category(state["categories"], arn, max(recent) + "T23:59:59Z", "AI")
    hours = read_agent_hours(state, now)
    ownership = read_ownership()
    save_state("state/activity.json", state)
    limits = read_limits(identity_client, users)
    index = read_index(regions)

    coverage_start = _parse_iso(state.get("coverageStart"))
    authoring_counts: dict[str, int] = {}
    for item in state["authoring"]:
        authoring_counts[item["arn"]] = authoring_counts.get(item["arn"], 0) + 1
    recent_floor = (now - dt.timedelta(days=30)).date()
    account_ai = {
        dt.date.fromisoformat(day)
        for days in state["aiByUser"].values()
        for day in days
        if dt.date.fromisoformat(day) >= recent_floor
    }
    feed_days = {dt.date.fromisoformat(day) for day in hours["days"] if day >= recent_floor.isoformat()}
    gaps = rules.gap_days(account_ai, feed_days) if hours["status"] == "OK" else []
    hours_stale = (
        hours["status"] == "OK"
        and hours["newest"] is not None
        and (now - hours["newest"]).days >= rules.STALE_FEED_DAYS
        and any((now.date() - day).days <= rules.STALE_FEED_DAYS for day in account_ai)
    )

    records = []
    tier_rows = []
    idle = active = pending = unknown = candidates = 0
    saving_total = 0.0
    for user in users:
        hit = dict(state["hits"].get(user["userArn"]) or {})
        trail_at = _parse_iso(hit.get("at"))
        hour_stamps = hours["by_user"].get(user["userArn"], [])
        hour_last = max((stamp for stamp, _value in hour_stamps), default=None)
        last_activity = rules.later_activity(trail_at, hour_last)
        if hour_last is not None and last_activity == hour_last and (trail_at is None or hour_last > trail_at):
            hit["source"] = "Agent hours"
            hit["category"] = "AI"
        peak = rules.peak_for_read(hour_stamps, now, bool(hours.get("complete")))
        judged = rules.judge_user(
            now, last_activity, coverage_start, user["active"], UNUSED_THRESHOLD_DAYS)
        owned, alone = _ownership_for(user, ownership)
        readiness = rules.removal_readiness(
            user["role"], judged["usageState"], alone, ownership is not None)
        user_ai = {
            dt.date.fromisoformat(day)
            for day in state["aiByUser"].get(user["userArn"], [])
        }
        confidence = rules.hours_confidence(
            hours["status"], bool(hours.get("complete")), judged["coverageDays"],
            UNUSED_THRESHOLD_DAYS, user_ai, {stamp.date() for stamp, _value in hour_stamps},
            hours["newest"], now,
        )
        fit = rules.license_fit(
            now, user["role"] or "", user["identityType"], judged["usageState"],
            peak, authoring_counts.get(user["userArn"], 0),
            bool(limits.get(user["userName"])), confidence,
        )
        categories = rules.categories_in_window(state["categories"], user["userArn"], window_floor or "")
        if judged["usageState"] == "IDLE":
            idle += 1
        elif judged["usageState"] == "ACTIVE":
            active += 1
        elif judged["usageState"] == "PENDING":
            pending += 1
        else:
            unknown += 1
        if fit["savingUsd"]:
            candidates += 1
            saving_total += fit["savingUsd"]
        records.append({
            "ts": now_iso,
            "userArn": user["userArn"],
            "userName": user["userName"],
            "role": user["role"],
            "identityType": user["identityType"],
            "active": "true" if user["active"] else "false",
            "assetState": "ACTIVE",
            "lastActivityAt": _iso(last_activity),
            "lastActivitySource": hit.get("source"),
            "lastActivityCategory": hit.get("category"),
            "daysSinceActivity": judged["daysSince"],
            "lastSignInAt": state["signins"].get(user["userArn"]),
            "coverageStart": _iso(coverage_start),
            "coverageDays": judged["coverageDays"],
            "usageState": judged["usageState"],
            "unknownReason": judged["unknownReason"],
            "recheckAfter": judged["recheckAfter"],
            "assetsOwned": owned,
            "assetsOwnedAlone": alone,
            "removalReadiness": readiness,
            "peakHours": peak,
            "hoursConfidence": confidence,
            "currentTier": fit["currentTier"],
            "listPriceUsd": fit["listPriceUsd"],
            "candidateTier": fit["candidateTier"],
            "candidatePriceUsd": fit["candidatePriceUsd"],
            "savingUsd": fit["savingUsd"],
            "changePath": fit["changePath"],
            "savingFrom": fit["savingFrom"],
            "authoringCount": authoring_counts.get(user["userArn"], 0),
            "didAuthoring": 1 if "authoring" in categories else 0,
            "didAI": 1 if "AI" in categories else 0,
            "didViewing": 1 if "viewing" in categories else 0,
            "didAdministration": 1 if "administration" in categories else 0,
            "attributionNote": hit.get("note") or "",
        })
        costs = fit["tierCosts"]
        for tier in rules.TIER_ORDER:
            cost = costs[tier]
            tier_rows.append({
                "ts": now_iso,
                "userArn": user["userArn"],
                "userName": user["userName"],
                "tier": tier,
                "listPriceUsd": rules.TIERS[tier]["price"],
                "includedHours": rules.included_hours(tier, now),
                "coversUse": "true" if cost is not None else "false",
                "costUsd": cost,
            })
    records.extend(_tombstones(users, now))
    sources = [
        _source_row(now_iso, "CloudTrail event history", now_iso, _iso(coverage_start),
                    (now - coverage_start).days if coverage_start else 0, 0,
                    rules.cloudtrail_status(coverage_start is not None, bool(state["hits"]) or bool(state["highWater"]))),
        _source_row(now_iso, "Agent Hours feed", _iso(hours["newest"]), None,
                    0, len(gaps), "STALE" if hours_stale else hours["status"]),
        _source_row(now_iso, "Orphaned Assets snapshot", None, None, 0, 0,
                    "OK" if ownership is not None else "NOT_DEPLOYED"),
        _source_row(now_iso, "user limits", None, None, 0, 0, "OK" if limits else "MISSING"),
        _source_row(now_iso, "index capacity", None, None, 0, 0, index["status"]),
    ]
    write_snapshot("users", records)
    write_snapshot("sources", sources)
    write_snapshot("tiers", tier_rows)
    seconds = round(time.monotonic() - started)
    cloudwatch.put_metric_data(
        Namespace="QuickGovernance/LicenseActivity",
        MetricData=[
            {"MetricName": "IdleUsers", "Value": idle, "Unit": "Count"},
            {"MetricName": "DowngradeCandidates", "Value": candidates, "Unit": "Count"},
            {"MetricName": "ApiErrors", "Value": api_errors, "Unit": "Count"},
        ],
    )
    return {
        "mode": "daily", "users": len(users), "active": active, "idle": idle,
        "pending": pending, "unknown": unknown, "candidates": candidates,
        "savingUsd": round(saving_total, 2), "apiErrors": api_errors,
        "gapDays": gaps, "seconds": seconds, "identityRegion": identity_region,
    }


def _source_row(ts, source, newest, coverage, days, gap_count, status) -> dict:
    return {
        "ts": ts, "source": source, "newestRecordAt": newest,
        "coverageStart": coverage, "coverageDays": days,
        "gapDays30": gap_count, "status": status,
    }


def _tombstones(users: list[dict], now: dt.datetime) -> list[dict]:
    known = load_state("state/known-users.json")
    now_iso = _iso(now)
    current = {user["userArn"] for user in users}
    for user in users:
        known[user["userArn"]] = {"name": user["userName"], "lastSeen": now_iso}
    rows = []
    for arn in list(known):
        if arn in current:
            known[arn].pop("deletedAt", None)
            continue
        deleted = _parse_iso(known[arn].get("deletedAt")) or now
        known[arn]["deletedAt"] = known[arn].get("deletedAt") or now_iso
        if (now - deleted).days > LOG_RETENTION_DAYS:
            known.pop(arn)
            continue
        rows.append({
            "ts": now_iso, "userArn": arn, "userName": known[arn].get("name"),
            "assetState": "DELETED",
        })
    save_state("state/known-users.json", known)
    return rows


def lambda_handler(_event: dict, _context: Any) -> dict:
    print("Starting license-activity collector")
    result = run()
    print(f"Done: {json.dumps(result)}")
    return result
