"""
Amazon Quick — Orphaned Assets Monitor collector.

One scheduled loop (daily by default): reconcile every asset's owner grants
against the current Quick user/group inventory and flag ownership risk.

Verified ground rules this implementation encodes (see module README,
"Phase 0 findings"):

  * Owner grant ⇔ the grant's actions contain quicksight:Update<Type>Permissions.
  * Deleting a user via the DeleteUser API leaves the asset in place. The
    dangling grant survives verbatim for a transition window (verified) and
    is then purged asynchronously by Quick (verified ~minutes later), so the
    steady-state orphan signature is an EMPTY permission document
    (NO_OWNER_GRANTS); OWNER_PRINCIPAL_MISSING catches the window.
  * User/group APIs live in the account's IDENTITY region, which can differ
    from the asset region. The AccessDeniedException message names the
    correct region; discovery parses it.
  * Search-by-owner (DIRECT_QUICKSIGHT_SOLE_OWNER) rejects deleted ARNs, so
    orphan detection requires the full describe-permissions sweep.
  * ListAnalyses returns soft-DELETED analyses (30-day restore window);
    they are skipped.
  * New-experience topics require the V2 APIs; PREVIEW agents resolve via
    DescribeAgent even though ListAgents omits them (permissions are only
    checked for listed agents).

Storage follows the repository chassis (same as the Dataset Lifecycle
Monitor): CloudWatch Logs data log group is the system of record (one JSON
event per asset per scan — findingType "NONE" when healthy so Logs Insights
latest() never surfaces stale findings), S3 keeps state + JSONL snapshots,
and a handful of low-cardinality KPIs go to CloudWatch metrics. The draft's
DynamoDB design remains an option for very large estates; it is not needed
at this scale and would break the repo's self-contained pattern.

Strictly read-only towards Quick: AUDIT_ONLY. No transfer, no deletion.
"""

from __future__ import annotations

import json
import os
import re
import time
import datetime as dt
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ACCOUNT_ID = os.environ["QS_ACCOUNT_ID"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
STATE_BUCKET = os.environ["STATE_BUCKET"]
# State key prefix. Empty (default) keeps the module-owned layout (state/*.json
# at the bucket root); the shared analytics bucket uses STATE_BUCKET=<analytics
# bucket> and STATE_PREFIX=orphaned-assets/, so state lives under
# orphaned-assets/state/ - outside the Glue table location and denied to Quick.
STATE_PREFIX = os.environ.get("STATE_PREFIX", "").strip("/")
# Snapshot sink. Defaults reproduce the module-owned layout
# (snapshots/ownership/... in the state bucket); the shared analytics bucket
# uses SNAPSHOT_BUCKET=<analytics bucket> and SNAPSHOT_PREFIX=orphaned-assets/.
SNAPSHOT_BUCKET = os.environ.get("SNAPSHOT_BUCKET") or STATE_BUCKET
SNAPSHOT_PREFIX = os.environ.get("SNAPSHOT_PREFIX", "snapshots/").strip("/")
# Assets whose ID starts with one of these prefixes are not scanned. Default:
# the Quick assets the governance stacks create themselves (their IDs are
# deterministic, quick-governance-*), so the monitor does not report on its
# own dashboards and datasets. Comma-separated; empty disables the filter.
EXCLUDE_ASSET_ID_PREFIXES = tuple(
    p.strip() for p in os.environ.get(
        "EXCLUDE_ASSET_ID_PREFIXES", "quick-governance-"
    ).split(",") if p.strip()
)


def is_excluded_asset(asset_id: str | None) -> bool:
    return bool(asset_id) and asset_id.startswith(EXCLUDE_ASSET_ID_PREFIXES)
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
IDENTITY_REGION = os.environ.get("IDENTITY_REGION", "")  # empty = discover
NAMESPACES = [
    ns.strip() for ns in os.environ.get("NAMESPACES", "default").split(",")
    if ns.strip()
]
ASSET_TYPES = [
    t.strip().upper()
    for t in os.environ.get(
        "ASSET_TYPES",
        "DATASET,DASHBOARD,ANALYSIS,DATA_SOURCE,FOLDER,SPACE,AGENT,TOPIC",
    ).split(",")
    if t.strip()
]

METRIC_NAMESPACE = "QuickGovernance/OrphanedAssets"
QS_MIN_INTERVAL_SECONDS = 0.22      # ~4.5 TPS, under the 5 TPS/user quota
MAX_PRINCIPALS_PER_EVENT = 20       # cap the per-asset principal list
OWNER_ACTION_RE = re.compile(r"quicksight:Update\w+Permissions$")
IDENTITY_REGION_RE = re.compile(r"identity region is ([a-z0-9-]+)")

_RETRY_CONFIG = Config(retries={"max_attempts": 8, "mode": "adaptive"})

quicksight = boto3.client("quicksight", config=_RETRY_CONFIG)
cloudwatch = boto3.client("cloudwatch", config=_RETRY_CONFIG)
s3 = boto3.client("s3", config=_RETRY_CONFIG)
sns = boto3.client("sns", config=_RETRY_CONFIG)


class Pacer:
    """Client-side throttle: guarantees a minimum interval between calls."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last_call = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delta = now - self._last_call
        if delta < self._min_interval:
            time.sleep(self._min_interval - delta)
        self._last_call = time.monotonic()


qs_pacer = Pacer(QS_MIN_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Small utilities (same chassis as the Dataset Lifecycle Monitor)
# ---------------------------------------------------------------------------

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        Bucket=STATE_BUCKET,
        Key=_state_key(key),
        Body=json.dumps(obj, default=str).encode("utf-8"),
        ContentType="application/json",
    )


def write_snapshot(records: list[dict]) -> None:
    now = _utcnow()
    body = "\n".join(json.dumps(r, default=str) for r in records).encode("utf-8")
    dated_key = f"{SNAPSHOT_PREFIX}/ownership/dt={now:%Y-%m-%d}/run-{now:%H%M%S}.jsonl"
    for key in (dated_key, f"{SNAPSHOT_PREFIX}/latest-ownership.jsonl"):
        s3.put_object(
            Bucket=SNAPSHOT_BUCKET, Key=key, Body=body,
            ContentType="application/x-ndjson",
        )


def put_kpis(metrics: dict[str, float]) -> None:
    metric_data = [
        {"MetricName": name, "Value": float(value), "Unit": "Count"}
        for name, value in metrics.items()
    ]
    for start in range(0, len(metric_data), 20):
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE, MetricData=metric_data[start:start + 20]
        )


def _paginate(fn: Callable, result_key: str, **kwargs) -> list[dict]:
    """Uniform manual NextToken pagination (several ops lack paginators)."""
    items: list[dict] = []
    next_token: str | None = None
    while True:
        qs_pacer.wait()
        call_kwargs = dict(kwargs)
        if next_token:
            call_kwargs["NextToken"] = next_token
        page = fn(**call_kwargs)
        items.extend(page.get(result_key, []))
        next_token = page.get("NextToken")
        if not next_token:
            break
    return items


# ---------------------------------------------------------------------------
# Identity inventory (users + groups live in the IDENTITY region)
# ---------------------------------------------------------------------------

def resolve_identity_client() -> Any:
    """
    Quick user/group APIs must be called against the account's identity
    region. If IDENTITY_REGION is not configured, discover it: call ListUsers
    in the asset region and parse the AccessDeniedException message
    ("... your identity region is sa-east-1 ..."). The discovery result is
    cached in S3.
    """
    region = IDENTITY_REGION
    state = load_state("state/identity.json")
    if not region:
        region = state.get("identityRegion", "")
    if not region:
        try:
            qs_pacer.wait()
            quicksight.list_users(
                AwsAccountId=ACCOUNT_ID, Namespace=NAMESPACES[0]
            )
            region = REGION
        except ClientError as error:
            match = IDENTITY_REGION_RE.search(str(error))
            if not match:
                raise
            region = match.group(1)
        state["identityRegion"] = region
        save_state("state/identity.json", state)
    if region == REGION:
        return quicksight
    return boto3.client("quicksight", region_name=region, config=_RETRY_CONFIG)


def load_identity_inventory(identity_client: Any) -> tuple[dict, dict, int]:
    """
    Returns (users_by_arn, groups_by_arn, api_errors).
      users_by_arn:  arn -> {"userName", "active", "role"}
      groups_by_arn: arn -> {"groupName", "memberCount"}
    """
    users: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    api_errors = 0
    for namespace in NAMESPACES:
        try:
            for user in _paginate(
                identity_client.list_users, "UserList",
                AwsAccountId=ACCOUNT_ID, Namespace=namespace,
            ):
                users[user["Arn"]] = {
                    "userName": user.get("UserName"),
                    "active": bool(user.get("Active")),
                    "role": user.get("Role"),
                    "namespace": namespace,
                }
        except ClientError as error:
            api_errors += 1
            print(f"WARN ListUsers failed for namespace {namespace}: {error}")
        try:
            for group in _paginate(
                identity_client.list_groups, "GroupList",
                AwsAccountId=ACCOUNT_ID, Namespace=namespace,
            ):
                member_count = None
                try:
                    members = _paginate(
                        identity_client.list_group_memberships,
                        "GroupMemberList",
                        AwsAccountId=ACCOUNT_ID, Namespace=namespace,
                        GroupName=group["GroupName"],
                    )
                    member_count = len(members)
                except ClientError as error:
                    api_errors += 1
                    print(
                        f"WARN ListGroupMemberships failed for "
                        f"{group.get('GroupName')}: {error}"
                    )
                groups[group["Arn"]] = {
                    "groupName": group.get("GroupName"),
                    "memberCount": member_count,
                    "namespace": namespace,
                }
        except ClientError as error:
            api_errors += 1
            print(f"WARN ListGroups failed for namespace {namespace}: {error}")
    return users, groups, api_errors


# ---------------------------------------------------------------------------
# Asset registry — inventory + permissions per type (all verified live)
# ---------------------------------------------------------------------------

def _list_datasets() -> list[dict]:
    return [
        {"id": s["DataSetId"], "name": s.get("Name"), "arn": s.get("Arn")}
        for s in _paginate(
            quicksight.list_data_sets, "DataSetSummaries",
            AwsAccountId=ACCOUNT_ID,
        )
    ]


def _list_dashboards() -> list[dict]:
    return [
        {"id": s["DashboardId"], "name": s.get("Name"), "arn": s.get("Arn")}
        for s in _paginate(
            quicksight.list_dashboards, "DashboardSummaryList",
            AwsAccountId=ACCOUNT_ID,
        )
    ]


def _list_analyses() -> list[dict]:
    # Soft-deleted analyses (Status DELETED, 30-day restore window) are
    # skipped: their owner is often legitimately gone.
    return [
        {"id": s["AnalysisId"], "name": s.get("Name"), "arn": s.get("Arn")}
        for s in _paginate(
            quicksight.list_analyses, "AnalysisSummaryList",
            AwsAccountId=ACCOUNT_ID,
        )
        if not str(s.get("Status", "")).startswith("DELETED")
    ]


def _list_data_sources() -> list[dict]:
    return [
        {"id": s["DataSourceId"], "name": s.get("Name"), "arn": s.get("Arn")}
        for s in _paginate(
            quicksight.list_data_sources, "DataSources",
            AwsAccountId=ACCOUNT_ID,
        )
    ]


def _list_folders() -> list[dict]:
    return [
        {"id": s["FolderId"], "name": s.get("Name"), "arn": s.get("Arn")}
        for s in _paginate(
            quicksight.list_folders, "FolderSummaryList",
            AwsAccountId=ACCOUNT_ID,
        )
    ]


def _list_spaces() -> list[dict]:
    return [
        {
            "id": s.get("spaceId") or s.get("SpaceId"),
            "name": s.get("name") or s.get("Name"),
            "arn": s.get("spaceArn") or s.get("SpaceArn"),
        }
        for s in _paginate(
            quicksight.list_spaces, "SpaceSummaries", AwsAccountId=ACCOUNT_ID
        )
    ]


def _list_agents() -> list[dict]:
    # The SYSTEM default agent is service-managed (Creator: Auto_Created)
    # and legitimately carries no owner grants — excluded (verified live).
    return [
        {
            "id": s.get("AgentId") or s.get("agentId"),
            "name": s.get("Name") or s.get("name"),
            "arn": s.get("Arn") or s.get("arn"),
        }
        for s in _paginate(
            quicksight.list_agents, "AgentSummaries", AwsAccountId=ACCOUNT_ID
        )
        if (s.get("AgentId") or s.get("agentId")) != "SYSTEM"
    ]


def _list_topics() -> list[dict]:
    if hasattr(quicksight, "list_topics_v2"):
        summaries = _paginate(
            quicksight.list_topics_v2, "TopicSummaryList",
            AwsAccountId=ACCOUNT_ID,
        )
    else:  # pragma: no cover - requires outdated SDK
        summaries = _paginate(
            quicksight.list_topics, "TopicsSummaries", AwsAccountId=ACCOUNT_ID
        )
    return [
        {"id": s["TopicId"], "name": s.get("Name"), "arn": s.get("Arn")}
        for s in summaries
    ]


def _topic_permissions(topic_id: str) -> dict:
    if hasattr(quicksight, "describe_topic_permissions_v2"):
        return quicksight.describe_topic_permissions_v2(
            AwsAccountId=ACCOUNT_ID, TopicId=topic_id
        )
    return quicksight.describe_topic_permissions(  # pragma: no cover
        AwsAccountId=ACCOUNT_ID, TopicId=topic_id
    )


ASSET_REGISTRY: dict[str, dict] = {
    "DATASET": {
        "list": _list_datasets,
        "permissions": lambda i: quicksight.describe_data_set_permissions(
            AwsAccountId=ACCOUNT_ID, DataSetId=i),
    },
    "DASHBOARD": {
        "list": _list_dashboards,
        "permissions": lambda i: quicksight.describe_dashboard_permissions(
            AwsAccountId=ACCOUNT_ID, DashboardId=i),
    },
    "ANALYSIS": {
        "list": _list_analyses,
        "permissions": lambda i: quicksight.describe_analysis_permissions(
            AwsAccountId=ACCOUNT_ID, AnalysisId=i),
    },
    "DATA_SOURCE": {
        "list": _list_data_sources,
        "permissions": lambda i: quicksight.describe_data_source_permissions(
            AwsAccountId=ACCOUNT_ID, DataSourceId=i),
    },
    "FOLDER": {
        "list": _list_folders,
        "permissions": lambda i: quicksight.describe_folder_permissions(
            AwsAccountId=ACCOUNT_ID, FolderId=i),
    },
    "SPACE": {
        "list": _list_spaces,
        "permissions": lambda i: quicksight.describe_space_permissions(
            AwsAccountId=ACCOUNT_ID, SpaceId=i),
    },
    "AGENT": {
        "list": _list_agents,
        "permissions": lambda i: quicksight.describe_agent_permissions(
            AwsAccountId=ACCOUNT_ID, AgentId=i),
    },
    "TOPIC": {
        "list": _list_topics,
        "permissions": lambda i: _topic_permissions(i),
    },
}


# ---------------------------------------------------------------------------
# Owner classification and finding rules
# ---------------------------------------------------------------------------

def is_owner_grant(actions: list[str]) -> bool:
    """Owner ⇔ grant contains Update<Type>Permissions (verified rule)."""
    return any(OWNER_ACTION_RE.search(a or "") for a in actions)


def classify_principal(principal: str, users: dict, groups: dict) -> str:
    """
    ACTIVE_USER / PENDING_USER / USER_MISSING /
    GROUP_WITH_MEMBERS / GROUP_EMPTY / GROUP_MISSING /
    NAMESPACE_GRANT / UNKNOWN_PRINCIPAL

    A missing principal is evidence, not confirmed deletion (QUICK_ONLY
    identity mode): the grant survives user deletion verbatim, and only
    absence from ListUsers reveals it.
    """
    if ":user/" in principal:
        user = users.get(principal)
        if user is None:
            return "USER_MISSING"
        return "ACTIVE_USER" if user.get("active") else "PENDING_USER"
    if ":group/" in principal:
        group = groups.get(principal)
        if group is None:
            return "GROUP_MISSING"
        count = group.get("memberCount")
        if count is None:
            return "GROUP_WITH_MEMBERS"  # membership unknown: assume live
        return "GROUP_WITH_MEMBERS" if count > 0 else "GROUP_EMPTY"
    if ":namespace/" in principal:
        return "NAMESPACE_GRANT"
    return "UNKNOWN_PRINCIPAL"


SEVERITY = {
    "NO_OWNER_GRANTS": "HIGH",
    "OWNER_PRINCIPAL_MISSING": "HIGH",
    "NO_ACTIVE_OWNER": "HIGH",
    "ONLY_INACTIVE_POLICY_OWNERS": "MEDIUM",
    "SINGLE_ACTIVE_OWNER": "MEDIUM",
    "MIXED_OWNER_STATUS": "LOW",
    "OWNER_STATUS_UNKNOWN": "LOW",
    "NONE": "NONE",
}


def derive_finding(owner_classes: list[str]) -> str:
    """
    Map the owner-classification multiset to a finding type. Active owners
    are ACTIVE_USER, GROUP_WITH_MEMBERS, or NAMESPACE_GRANT (a namespace
    grant makes every namespace member an owner).
    """
    if not owner_classes:
        return "NO_OWNER_GRANTS"
    active = [
        c for c in owner_classes
        if c in ("ACTIVE_USER", "GROUP_WITH_MEMBERS", "NAMESPACE_GRANT")
    ]
    unknown = [c for c in owner_classes if c == "UNKNOWN_PRINCIPAL"]
    missing = [c for c in owner_classes if c in ("USER_MISSING", "GROUP_MISSING")]
    if not active:
        if unknown:
            return "OWNER_STATUS_UNKNOWN"
        if missing and len(missing) == len(owner_classes):
            return "OWNER_PRINCIPAL_MISSING"
        if missing:
            return "NO_ACTIVE_OWNER"
        return "ONLY_INACTIVE_POLICY_OWNERS"  # only PENDING / GROUP_EMPTY
    if missing or unknown or len(active) < len(owner_classes):
        return "MIXED_OWNER_STATUS"
    if len(active) == 1 and active[0] == "ACTIVE_USER":
        return "SINGLE_ACTIVE_OWNER"
    return "NONE"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def scan_asset_type(
    asset_type: str, users: dict, groups: dict, now_iso: str, scan_id: str
) -> tuple[list[dict], int]:
    """Returns (records, api_errors). Raises only if the LIST call fails."""
    registry = ASSET_REGISTRY[asset_type]
    assets = registry["list"]()
    records: list[dict] = []
    api_errors = 0
    for asset in assets:
        asset_id = asset.get("id")
        if not asset_id or is_excluded_asset(asset_id):
            continue
        record: dict[str, Any] = {
            "ts": now_iso,
            "scanId": scan_id,
            "assetType": asset_type,
            "assetId": asset_id,
            "assetName": asset.get("name"),
            "assetArn": asset.get("arn"),
        }
        try:
            qs_pacer.wait()
            response = registry["permissions"](asset_id)
            grants = response.get("Permissions", []) or []
        except ClientError as error:
            api_errors += 1
            print(f"WARN permissions failed {asset_type}/{asset_id}: {error}")
            record.update({
                "findingType": "OWNER_STATUS_UNKNOWN",
                "severity": SEVERITY["OWNER_STATUS_UNKNOWN"],
                "confidence": "UNKNOWN",
                "scanNote": "PERMISSIONS_CALL_FAILED",
                "ownersTotal": None,
            })
            records.append(record)
            continue

        owners: list[tuple[str, str]] = []   # (principal, classification)
        viewers = 0
        for grant in grants:
            principal = grant.get("Principal", "")
            if is_owner_grant(grant.get("Actions", []) or []):
                owners.append(
                    (principal, classify_principal(principal, users, groups))
                )
            else:
                viewers += 1

        owner_classes = [c for _, c in owners]
        finding = derive_finding(owner_classes)
        counts = {
            "ownersTotal": len(owners),
            "ownersActive": owner_classes.count("ACTIVE_USER")
            + owner_classes.count("GROUP_WITH_MEMBERS")
            + owner_classes.count("NAMESPACE_GRANT"),
            "ownersMissing": owner_classes.count("USER_MISSING")
            + owner_classes.count("GROUP_MISSING"),
            "ownersPending": owner_classes.count("PENDING_USER")
            + owner_classes.count("GROUP_EMPTY"),
            "viewersTotal": viewers,
        }
        record.update(counts)
        record["findingType"] = finding
        record["severity"] = SEVERITY[finding]
        # QUICK_ONLY identity mode: a missing principal is probable, not
        # confirmed — identity may have moved namespaces or the inventory
        # may be incomplete. Never remediate on PROBABLE alone.
        record["confidence"] = (
            "PROBABLE" if counts["ownersMissing"] else "QUICK_INVENTORY"
        )
        record["ownerPrincipals"] = [
            {"principal": p.split("/", 1)[-1][-80:], "status": c}
            for p, c in owners[:MAX_PRINCIPALS_PER_EVENT]
        ]
        records.append(record)
    return records, api_errors


def send_finding_alert(record: dict) -> None:
    if not SNS_TOPIC_ARN:
        return
    name = record.get("assetName") or record.get("assetId")
    subject = f"Quick ownership finding: {record['findingType']} - {name}"[:100]
    principals = "\n".join(
        f"    {p['status']:20} {p['principal']}"
        for p in record.get("ownerPrincipals", [])
    ) or "    (no owner grants at all)"
    body = "\n".join([
        "Amazon Quick — Orphaned Assets Monitor",
        "",
        f"Finding:       {record['findingType']} ({record['severity']})",
        f"Asset:         {record.get('assetType')} / {name}",
        f"Asset ID:      {record.get('assetId')}",
        f"Confidence:    {record.get('confidence')}",
        "Owner grants:",
        principals,
        "",
        "This monitor is audit-only: nothing was changed. In Manage Quick ->",
        "Manage assets, Transfer the asset to a new owner (orphaned) or use",
        "Share -> owner to add an admin/recovery group as co-owner:",
        "https://docs.aws.amazon.com/quick/latest/userguide/manage-qs-assets.html",
    ])
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=body)
    except ClientError as error:
        print(f"WARN SNS publish failed: {error}")


def run_scan() -> dict:
    started = time.monotonic()
    now = _utcnow()
    now_iso = _iso(now)
    scan_id = f"{now:%Y%m%dT%H%M%SZ}"
    api_errors = 0
    partial = False

    identity_client = resolve_identity_client()
    users, groups, identity_errors = load_identity_inventory(identity_client)
    api_errors += identity_errors
    if not users:
        # An empty user inventory would classify every asset as orphaned.
        # Treat it as a failed scan instead of emitting false findings.
        raise RuntimeError("User inventory is empty; aborting scan")

    records: list[dict] = []
    for asset_type in ASSET_TYPES:
        if asset_type not in ASSET_REGISTRY:
            print(f"WARN unknown asset type {asset_type}; skipping")
            continue
        try:
            type_records, type_errors = scan_asset_type(
                asset_type, users, groups, now_iso, scan_id
            )
            records.extend(type_records)
            api_errors += type_errors
            if type_errors:
                partial = True
        except ClientError as error:
            api_errors += 1
            partial = True
            print(f"WARN listing failed for {asset_type}: {error}")

    scan_status = "PARTIAL" if partial else "COMPLETE"
    for record in records:
        record["scanStatus"] = scan_status

    # ---- finding lifecycle: NEW alerts + RECOVERED on complete scans -----
    previous = load_state("state/findings.json")
    current: dict[str, dict] = {}
    new_high = 0
    high_transitions: list[dict] = []
    for record in records:
        if record["findingType"] in ("NONE",):
            continue
        key = f"{record['assetType']}#{record['assetId']}"
        current[key] = {
            "findingType": record["findingType"],
            "severity": record["severity"],
            "assetName": record.get("assetName"),
            "firstObservedAt": (
                previous.get(key, {}).get("firstObservedAt")
                if previous.get(key, {}).get("findingType")
                == record["findingType"]
                else None
            ) or now_iso,
            "lastObservedAt": now_iso,
        }
        record["firstObservedAt"] = current[key]["firstObservedAt"]
        old = previous.get(key, {})
        is_new = old.get("findingType") != record["findingType"]
        if is_new and record["severity"] == "HIGH":
            new_high += 1
            send_finding_alert(record)
        # Ownership restored: a HIGH finding downgraded to a lower severity.
        if (
            is_new
            and old.get("severity") == "HIGH"
            and record["severity"] != "HIGH"
        ):
            high_transitions.append({
                "assetType": record["assetType"],
                "assetId": record["assetId"],
                "assetName": record.get("assetName"),
                "recoveredFrom": old.get("findingType"),
                "firstObservedAt": old.get("firstObservedAt"),
            })

    recovered = 0
    if scan_status == "COMPLETE":
        for item in high_transitions:
            recovered += 1
            records.append({
                "ts": now_iso,
                "scanId": scan_id,
                "scanStatus": scan_status,
                "assetType": item["assetType"],
                "assetId": item["assetId"],
                "assetName": item.get("assetName"),
                "findingType": "RECOVERED",
                "severity": "NONE",
                "confidence": "QUICK_INVENTORY",
                "recoveredFrom": item.get("recoveredFrom"),
                "firstObservedAt": item.get("firstObservedAt"),
            })
        for key, old in previous.items():
            if key in current:
                continue
            asset_type, asset_id = key.split("#", 1)
            if is_excluded_asset(asset_id):
                # Newly excluded, not recovered: drop it from state silently.
                continue
            recovered += 1
            records.append({
                "ts": now_iso,
                "scanId": scan_id,
                "scanStatus": scan_status,
                "assetType": asset_type,
                "assetId": asset_id,
                "assetName": old.get("assetName"),
                "findingType": "RECOVERED",
                "severity": "NONE",
                "confidence": "QUICK_INVENTORY",
                "recoveredFrom": old.get("findingType"),
                "firstObservedAt": old.get("firstObservedAt"),
            })
        save_state("state/findings.json", current)
    else:
        # Never close findings after a partial scan.
        merged = dict(previous)
        merged.update(current)
        save_state("state/findings.json", merged)
    write_snapshot(records)

    open_by_severity = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    by_type: dict[str, int] = {}
    for finding in current.values():
        open_by_severity[finding["severity"]] = (
            open_by_severity.get(finding["severity"], 0) + 1
        )
        by_type[finding["findingType"]] = (
            by_type.get(finding["findingType"], 0) + 1
        )

    scan_seconds = round(time.monotonic() - started)
    put_kpis({
        "AssetsScanned": len(
            [r for r in records if r.get("findingType") != "RECOVERED"]
        ),
        "UsersScanned": len(users),
        "GroupsScanned": len(groups),
        "OpenFindings": len(current),
        "HighSeverityFindings": open_by_severity["HIGH"],
        "SingleActiveOwnerFindings": by_type.get("SINGLE_ACTIVE_OWNER", 0),
        "OwnerPrincipalMissingFindings": by_type.get(
            "OWNER_PRINCIPAL_MISSING", 0
        ),
        "RecoveredFindings": recovered,
        "PartialScans": 1 if partial else 0,
        "ApiErrors": api_errors,
        "ScanDurationSeconds": scan_seconds,
    })
    return {
        "scanId": scan_id, "status": scan_status,
        "assets": len(records) - recovered, "users": len(users),
        "groups": len(groups), "openFindings": len(current),
        "high": open_by_severity["HIGH"], "newHigh": new_high,
        "recovered": recovered, "apiErrors": api_errors,
        "seconds": scan_seconds,
    }


def lambda_handler(event: dict, _context: Any) -> dict:
    print("Starting orphaned-assets scan")
    result = run_scan()
    print(f"Done: {json.dumps(result)}")
    return result
