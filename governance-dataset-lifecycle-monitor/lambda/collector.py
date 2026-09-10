"""
Amazon Quick — Dataset Lifecycle Monitor collector.

One Lambda, two loops, selected by the EventBridge schedule payload:

  {"mode": "fast"}  — sync health (PFR 1 + PFR 3)
      ListDataSets + ListIngestions per SPICE dataset.
      Derives last refresh status, typed failure reason, last/avg durations
      (segmented by refresh type), emits one JSON log event per dataset,
      publishes fleet KPIs, and sends SNS alerts for NEW failures
      (deduplicated via an S3 state object).

  {"mode": "slow"}  — usage, lineage, capacity (PFR 2)
      ListDashboards + DescribeDashboard (delta) for dashboard lineage,
      ListTopics + DescribeTopic for topic lineage,
      ListSpaces + ListSpaceResources + ListAgents + DescribeAgent for the
      space and Chat Agent lineage (feature-detected; needs a botocore with
      the Space/Agent APIs),
      GetMetricData on DashboardViewCount for last-view evidence,
      optional CloudTrail LookupEvents (QueryDatabase / GetDashboard) for
      higher-confidence evidence, optional CHAT_LOGS scan (Logs Insights on
      the Chat and Feedback Monitor log group) for agent-citation evidence,
      best-effort DescribeDataSet for SPICE capacity and an
      ESTIMATED_PURCHASED monthly cost signal.

Design rules honored (see module README):
  * A FAILED refresh never overwrites LastSuccessfulRefreshAt.
  * Direct-query datasets never get SPICE refresh semantics.
  * "No observed use" is evidence-bounded, never presented as proof.
  * CloudTrail QueryDatabase counts as usage only for direct-query datasets:
    for SPICE it fires during refresh (per AWS docs), and refreshes are not
    consumption.
  * Estimated cost is labeled ESTIMATED_PURCHASED, never an invoice amount.
  * Deleted datasets emit tombstone records (assetState=DELETED) so every
    dashboard table drops them on the next scan, whatever the time window.
  * ListIngestions is paced under the 5 TPS/user quota; every call retries
    with adaptive backoff; a partial scan is flagged, never hidden.

Storage layout (cost-optimized, no database):
  * CloudWatch Logs (DATA_LOG_GROUP): one JSON event per dataset per run —
    system of record queried by the dashboard (Logs Insights).
  * CloudWatch metrics (QuickGovernance/DatasetLifecycle): low-cardinality
    fleet KPIs only.
  * S3 (STATE_BUCKET): alert-dedupe + lineage + usage state, and per-run
    JSONL snapshots for Athena/Grafana.
"""

from __future__ import annotations

import json
import os
import re
import time
import datetime as dt
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ACCOUNT_ID = os.environ["QS_ACCOUNT_ID"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
DATA_LOG_GROUP = os.environ["DATA_LOG_GROUP"]
STATE_BUCKET = os.environ["STATE_BUCKET"]
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
ENABLE_CLOUDTRAIL = os.environ.get("ENABLE_CLOUDTRAIL", "true").lower() == "true"
SPICE_RATE_PER_GB_MONTH = float(os.environ.get("SPICE_RATE_PER_GB_MONTH", "0.38"))
UNUSED_THRESHOLD_DAYS = int(os.environ.get("UNUSED_THRESHOLD_DAYS", "30"))
INGESTION_LOOKBACK_RUNS = int(os.environ.get("INGESTION_LOOKBACK_RUNS", "50"))
CT_LOOKBACK_DAYS = int(os.environ.get("CT_LOOKBACK_DAYS", "90"))
CHAT_LOG_GROUP = os.environ.get("CHAT_LOG_GROUP", "")
LOG_RETENTION_DAYS = int(os.environ.get("LOG_RETENTION_DAYS", "90"))

METRIC_NAMESPACE = "QuickGovernance/DatasetLifecycle"
VIEW_LOOKBACK_DAYS_PRIMARY = 90     # first GetMetricData pass
VIEW_LOOKBACK_DAYS_EXTENDED = 455   # CloudWatch 1-hour/1-day retention bound
QS_MIN_INTERVAL_SECONDS = 0.22      # ~4.5 TPS, under the 5 TPS/user quota
CT_MIN_INTERVAL_SECONDS = 0.55      # under the 2 TPS LookupEvents quota
MAX_CT_PAGES_PER_EVENT_NAME = 200   # safety cap (~10k events per run)
ERROR_MESSAGE_MAX_CHARS = 400
AGENT_LOOKBACK_DAYS = 30            # first CHAT_LOGS scan window
AGENT_QUERY_LIMIT = 10000           # Logs Insights hard cap per query
AGENT_QUERY_TIMEOUT_SECONDS = 120   # give up polling the Insights query
AGENT_SCOPE_MEDIUM_MAX = int(os.environ.get("AGENT_SCOPE_MEDIUM_MAX", "10"))
AGENT_SCOPE_CACHE_DAYS = 7          # re-resolve agent->spaces after this
CONFIDENCE_RANK = {"HIGH": 2, "MEDIUM": 1}  # tie-break for equal timestamps

_RETRY_CONFIG = Config(retries={"max_attempts": 8, "mode": "adaptive"})

quicksight = boto3.client("quicksight", config=_RETRY_CONFIG)
logs = boto3.client("logs", config=_RETRY_CONFIG)
cloudwatch = boto3.client("cloudwatch", config=_RETRY_CONFIG)
s3 = boto3.client("s3", config=_RETRY_CONFIG)
sns = boto3.client("sns", config=_RETRY_CONFIG)
cloudtrail = boto3.client("cloudtrail", config=_RETRY_CONFIG)


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
ct_pacer = Pacer(CT_MIN_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_qs_id(value: str, kind: str) -> str:
    """
    Normalize a QuickSight resource reference to its bare ID.

    CloudTrail events and chat citations carry ARNs (sometimes doubled, e.g.
    'arn:...:dataset/arn:...:dataset/<id>'), while lineage state and
    ListDataSets use bare IDs. Taking the substring after the LAST
    '<kind>/' collapses every observed shape; bare IDs pass through.
    """
    marker = f"{kind}/"
    index = value.rfind(marker)
    return value[index + len(marker):] if index >= 0 else value


def _normalize_hit_map(hits: dict[str, str], kind: str) -> dict[str, str]:
    """Re-key an {id-or-arn: iso-ts} map by bare ID, keeping the newest ts."""
    normalized: dict[str, str] = {}
    for key, ts in hits.items():
        bare = _normalize_qs_id(key, kind)
        previous = normalized.get(bare)
        if previous is None or (ts and ts > previous):
            normalized[bare] = ts
    return normalized


def reconcile_deleted_datasets(mode: str, current: list[dict]) -> list[dict]:
    """
    Track the known dataset population in S3 and emit tombstone records
    (assetState=DELETED) for datasets that disappeared from ListDataSets.

    Tombstones are re-emitted on every run until they age past the log
    retention window, so `latest(assetState) by datasetId` is DELETED in any
    dashboard time range and the tables drop the row. Tombstones carry loop
    and importMode so they pass the widgets' pre-stats filters.
    """
    known = load_state("state/known-datasets.json")
    now = _utcnow()
    now_iso = _iso(now)

    for summary in current:
        known[summary["DataSetId"]] = {
            "name": summary.get("Name"),
            "importMode": summary.get("ImportMode"),
            "lastSeen": now_iso,
        }

    current_ids = {s["DataSetId"] for s in current}
    tombstones: list[dict] = []
    for dataset_id in list(known):
        if dataset_id in current_ids:
            known[dataset_id].pop("deletedAt", None)
            continue
        entry = known[dataset_id]
        deleted_at = _parse_iso(entry.get("deletedAt"))
        if deleted_at is None:
            deleted_at = now
            entry["deletedAt"] = now_iso
        if (now - deleted_at).days > LOG_RETENTION_DAYS:
            known.pop(dataset_id)  # older than every log event; stop emitting
            continue
        tombstones.append({
            "ts": now_iso,
            "loop": mode,
            "datasetId": dataset_id,
            "datasetName": entry.get("name"),
            "importMode": entry.get("importMode"),
            "assetState": "DELETED",
            "lastStatus": "DELETED",
            "findings": [],
            "findingsCsv": "",
        })

    save_state("state/known-datasets.json", known)
    return tombstones


def load_state(key: str) -> dict:
    try:
        response = s3.get_object(Bucket=STATE_BUCKET, Key=key)
        return json.loads(response["Body"].read())
    except ClientError as error:
        if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return {}
        raise


def save_state(key: str, obj: dict) -> None:
    s3.put_object(
        Bucket=STATE_BUCKET,
        Key=key,
        Body=json.dumps(obj, default=str).encode("utf-8"),
        ContentType="application/json",
    )


def write_snapshot(mode: str, records: list[dict]) -> None:
    """Per-run JSONL snapshot (Athena/Grafana-friendly) + rolling latest."""
    now = _utcnow()
    body = "\n".join(json.dumps(r, default=str) for r in records).encode("utf-8")
    dated_key = (
        f"snapshots/{mode}/dt={now:%Y-%m-%d}/run-{now:%H%M%S}.jsonl"
    )
    for key in (dated_key, f"snapshots/latest-{mode}.jsonl"):
        s3.put_object(
            Bucket=STATE_BUCKET, Key=key, Body=body,
            ContentType="application/x-ndjson",
        )


def put_data_events(mode: str, records: list[dict]) -> None:
    """Write one JSON log event per dataset to the data log group."""
    if not records:
        return
    stream_name = f"{mode}-{_utcnow():%Y-%m-%d}"
    try:
        logs.create_log_stream(
            logGroupName=DATA_LOG_GROUP, logStreamName=stream_name
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    timestamp_ms = int(_utcnow().timestamp() * 1000)
    events = [
        {"timestamp": timestamp_ms, "message": json.dumps(r, default=str)}
        for r in records
    ]
    for start in range(0, len(events), 500):
        logs.put_log_events(
            logGroupName=DATA_LOG_GROUP,
            logStreamName=stream_name,
            logEvents=events[start:start + 500],
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


# ---------------------------------------------------------------------------
# Quick API collection
# ---------------------------------------------------------------------------

def list_datasets() -> list[dict]:
    datasets: list[dict] = []
    paginator = quicksight.get_paginator("list_data_sets")
    for page in paginator.paginate(AwsAccountId=ACCOUNT_ID):
        qs_pacer.wait()
        datasets.extend(page.get("DataSetSummaries", []))
    return datasets


def list_recent_ingestions(dataset_id: str) -> list[dict]:
    """
    Newest ingestions for one dataset, bounded to <=200 fetched entries
    (1-2 API calls). The API is observed to return newest-first, but we
    defensively re-sort by CreatedTime before deriving state.
    """
    ingestions: list[dict] = []
    paginator = quicksight.get_paginator("list_ingestions")
    page_iter = paginator.paginate(
        AwsAccountId=ACCOUNT_ID,
        DataSetId=dataset_id,
        PaginationConfig={"MaxItems": 200, "PageSize": 100},
    )
    for page in page_iter:
        qs_pacer.wait()
        ingestions.extend(page.get("Ingestions", []))
    ingestions.sort(
        key=lambda i: i.get(
            "CreatedTime", dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        ),
        reverse=True,
    )
    return ingestions[:INGESTION_LOOKBACK_RUNS]


def derive_refresh_state(ingestions: list[dict]) -> dict:
    """PFR 1 + PFR 3 fields from the newest-first ingestion window."""
    state: dict[str, Any] = {
        "lastStatus": None, "lastRequestType": None, "lastRequestSource": None,
        "lastAttemptAt": None, "lastSuccessAt": None, "lastFailedAt": None,
        "errorType": None, "errorMessage": None,
        "lastDurationSec": None, "avgDurationSec": None,
        "avgFullRefreshSec": None, "avgIncrementalSec": None,
        "rowsIngested": None, "rowsDropped": None,
        "windowRuns": len(ingestions), "windowFailed": 0,
    }
    if not ingestions:
        return state

    newest = ingestions[0]
    state["lastStatus"] = newest.get("IngestionStatus")
    state["lastRequestType"] = newest.get("RequestType")
    state["lastRequestSource"] = newest.get("RequestSource")
    state["lastAttemptAt"] = _iso(newest.get("CreatedTime"))

    durations_all: list[int] = []
    durations_full: list[int] = []
    durations_incremental: list[int] = []

    for ingestion in ingestions:
        status = ingestion.get("IngestionStatus")
        created = ingestion.get("CreatedTime")
        if status == "COMPLETED":
            # A FAILED run never overwrites the last-success timestamp.
            if state["lastSuccessAt"] is None:
                state["lastSuccessAt"] = _iso(created)
                state["lastDurationSec"] = ingestion.get("IngestionTimeInSeconds")
                row_info = ingestion.get("RowInfo", {})
                state["rowsIngested"] = row_info.get("RowsIngested")
                state["rowsDropped"] = row_info.get("RowsDropped")
            seconds = ingestion.get("IngestionTimeInSeconds")
            if seconds is not None:
                durations_all.append(seconds)
                if ingestion.get("RequestType") == "INCREMENTAL_REFRESH":
                    durations_incremental.append(seconds)
                elif ingestion.get("RequestType") == "FULL_REFRESH":
                    durations_full.append(seconds)
        elif status == "FAILED":
            state["windowFailed"] += 1
            if state["lastFailedAt"] is None:
                state["lastFailedAt"] = _iso(created)
                error_info = ingestion.get("ErrorInfo", {}) or {}
                state["errorType"] = error_info.get("Type")
                message = (error_info.get("Message") or "")
                state["errorMessage"] = message[:ERROR_MESSAGE_MAX_CHARS] or None

    def _avg(values: list[int]) -> int | None:
        return round(sum(values) / len(values)) if values else None

    state["avgDurationSec"] = _avg(durations_all)
    state["avgFullRefreshSec"] = _avg(durations_full)
    state["avgIncrementalSec"] = _avg(durations_incremental)
    return state


def derive_fast_findings(refresh_state: dict) -> list[str]:
    findings: list[str] = []
    if refresh_state["lastStatus"] == "FAILED":
        findings.append("REFRESH_FAILED")
    if refresh_state["windowRuns"] > 0 and refresh_state["lastSuccessAt"] is None:
        findings.append("REFRESH_NEVER_COMPLETED")
    if refresh_state["windowFailed"] >= 3:
        findings.append("REFRESH_FAILURE_RECURRING")
    return findings


def describe_spice_capacity(dataset_id: str) -> tuple[float | None, bool]:
    """
    Best-effort ConsumedSpiceCapacityInBytes -> GB (decimal).
    Returns (gb, supported). File-upload datasets are not describable
    through the API and are reported as unsupported, not as errors.
    """
    qs_pacer.wait()
    try:
        response = quicksight.describe_data_set(
            AwsAccountId=ACCOUNT_ID, DataSetId=dataset_id
        )
        consumed = response["DataSet"].get("ConsumedSpiceCapacityInBytes")
        if consumed is None:
            return None, True
        return round(consumed / 1_000_000_000, 3), True
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code in ("InvalidParameterValueException", "UnsupportedUserEditionException"):
            return None, False
        raise


# ---------------------------------------------------------------------------
# FAST loop — sync health (PFR 1 + PFR 3)
# ---------------------------------------------------------------------------

def run_fast_loop() -> dict:
    started = time.monotonic()
    api_errors = 0
    records: list[dict] = []
    failed_datasets = 0
    spice_count = 0
    direct_query_count = 0

    alert_state = load_state("state/alert-state.json")
    now_iso = _iso(_utcnow())

    datasets = list_datasets()
    for summary in datasets:
        dataset_id = summary["DataSetId"]
        record: dict[str, Any] = {
            "ts": now_iso,
            "loop": "fast",
            "datasetId": dataset_id,
            "datasetName": summary.get("Name"),
            "importMode": summary.get("ImportMode"),
            "assetState": "ACTIVE",
        }
        if summary.get("ImportMode") != "SPICE":
            direct_query_count += 1
            record["lastStatus"] = "NOT_APPLICABLE"
            record["findings"] = []
            record["findingsCsv"] = ""
            records.append(record)
            continue

        spice_count += 1
        try:
            ingestions = list_recent_ingestions(dataset_id)
        except ClientError as error:
            api_errors += 1
            print(f"WARN ListIngestions failed for {dataset_id}: {error}")
            record["lastStatus"] = "SCAN_ERROR"
            record["findings"] = ["SCAN_INCOMPLETE"]
            record["findingsCsv"] = "SCAN_INCOMPLETE"
            records.append(record)
            continue

        refresh_state = derive_refresh_state(ingestions)
        record.update(refresh_state)
        record["findings"] = derive_fast_findings(refresh_state)
        record["findingsCsv"] = ",".join(record["findings"])
        records.append(record)

        if refresh_state["lastStatus"] == "FAILED":
            failed_datasets += 1
            newest_failed_id = ingestions[0].get("IngestionId")
            if alert_state.get(dataset_id) != newest_failed_id:
                send_failure_alert(record)
                alert_state[dataset_id] = newest_failed_id
        else:
            # Recovered (or healthy): clear dedupe state so the next
            # distinct failure alerts again.
            alert_state.pop(dataset_id, None)

    save_state("state/alert-state.json", alert_state)
    records.extend(reconcile_deleted_datasets("fast", datasets))
    put_data_events("fast", records)
    write_snapshot("fast", records)

    scan_seconds = round(time.monotonic() - started)
    put_kpis({
        "DatasetsScanned": len(datasets),
        "SpiceDatasets": spice_count,
        "DirectQueryDatasets": direct_query_count,
        "FailedRefreshDatasets": failed_datasets,
        "ApiErrors": api_errors,
        "FastScanDurationSeconds": scan_seconds,
    })
    return {
        "mode": "fast", "datasets": len(datasets), "spice": spice_count,
        "failed": failed_datasets, "apiErrors": api_errors,
        "seconds": scan_seconds,
    }


def send_failure_alert(record: dict) -> None:
    if not SNS_TOPIC_ARN:
        return
    name = record.get("datasetName") or record["datasetId"]
    subject = f"Quick dataset refresh FAILED: {name}"[:100]
    body = "\n".join([
        "Amazon Quick — Dataset Lifecycle Monitor",
        "",
        f"Dataset:        {name}",
        f"Dataset ID:     {record['datasetId']}",
        f"Failed at:      {record.get('lastFailedAt')}",
        f"Refresh type:   {record.get('lastRequestType')} ({record.get('lastRequestSource')})",
        f"Error type:     {record.get('errorType')}",
        f"Error message:  {record.get('errorMessage')}",
        f"Last success:   {record.get('lastSuccessAt') or 'never (in lookback window)'}",
        "",
        "Consumers keep seeing the previous successful SPICE snapshot until "
        "a refresh completes. Error-code reference:",
        "https://docs.aws.amazon.com/quick/latest/userguide/errors-spice-ingestion.html",
    ])
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=body)
    except ClientError as error:
        print(f"WARN SNS publish failed: {error}")


# ---------------------------------------------------------------------------
# SLOW loop — lineage, usage evidence, capacity (PFR 2)
# ---------------------------------------------------------------------------

def refresh_dashboard_lineage() -> dict:
    """
    dashboardId -> {name, lastUpdated, dataSetArns}; DescribeDashboard is
    called only for new/changed dashboards (delta collection).
    """
    lineage_state = load_state("state/dashboard-lineage.json")
    dashboards: list[dict] = []
    paginator = quicksight.get_paginator("list_dashboards")
    for page in paginator.paginate(AwsAccountId=ACCOUNT_ID):
        qs_pacer.wait()
        dashboards.extend(page.get("DashboardSummaryList", []))

    current_ids = set()
    for summary in dashboards:
        dashboard_id = summary["DashboardId"]
        current_ids.add(dashboard_id)
        last_updated = _iso(summary.get("LastUpdatedTime"))
        cached = lineage_state.get(dashboard_id)
        if cached and cached.get("lastUpdated") == last_updated:
            continue
        qs_pacer.wait()
        try:
            response = quicksight.describe_dashboard(
                AwsAccountId=ACCOUNT_ID, DashboardId=dashboard_id
            )
            dataset_arns = response["Dashboard"]["Version"].get("DataSetArns", [])
        except ClientError as error:
            print(f"WARN DescribeDashboard failed for {dashboard_id}: {error}")
            continue
        lineage_state[dashboard_id] = {
            "name": summary.get("Name"),
            "lastUpdated": last_updated,
            "dataSetArns": dataset_arns,
        }

    # Drop deleted dashboards from state.
    for stale_id in set(lineage_state) - current_ids:
        lineage_state.pop(stale_id, None)

    save_state("state/dashboard-lineage.json", lineage_state)
    return lineage_state


def _extract_topic_datasets(response: dict) -> list[str]:
    """Dataset ARNs from a DescribeTopic/DescribeTopicV2 response.

    Prefers the structured Topic.DataSets[].DatasetArn list; falls back to a
    regex walk of the whole response so schema evolution cannot silently
    drop lineage.
    """
    topic = response.get("Topic", {}) or {}
    arns = [
        d.get("DatasetArn")
        for d in (topic.get("DataSets") or [])
        if isinstance(d, dict) and d.get("DatasetArn")
    ]
    if arns:
        return arns
    blob = json.dumps(response, default=str)
    return sorted(set(re.findall(r"arn:[^\"\s]*:dataset/[A-Za-z0-9-]+", blob)))


def _describe_topic_any(topic_id: str) -> dict:
    """DescribeTopicV2 when available, falling back to the legacy API.

    Topics created in the new Quick experience reject the legacy
    DescribeTopic ("use new versions of Topic APIs"), and only appear in
    ListTopicsV2 — verified empirically.
    """
    attempts = []
    if hasattr(quicksight, "describe_topic_v2"):
        attempts.append(quicksight.describe_topic_v2)
    attempts.append(quicksight.describe_topic)
    last_error: ClientError | None = None
    for describe in attempts:
        qs_pacer.wait()
        try:
            return describe(AwsAccountId=ACCOUNT_ID, TopicId=topic_id)
        except ClientError as error:
            last_error = error
    raise last_error  # type: ignore[misc]


def refresh_topic_lineage(extra_topic_ids: set[str] | None = None) -> dict:
    """
    topicId -> {name, dataSetArns}. Topics (the Q&A semantic layer) are the
    main path from Chat Agents to datasets. Accounts hold few topics, so all
    are described on every slow run (no delta cache needed).

    Listing prefers ListTopicsV2 (new-experience topics are invisible to the
    legacy ListTopics), and topics referenced by spaces (extra_topic_ids)
    are described even if no listing returns them. Failures downgrade to
    warnings: lineage is evidence enrichment, not core.
    """
    lineage: dict[str, dict] = {}
    try:
        # list_topics(_v2) has no botocore paginator; page via NextToken.
        summaries: list[dict] = []
        use_v2 = hasattr(quicksight, "list_topics_v2")
        next_token: str | None = None
        while True:
            qs_pacer.wait()
            kwargs: dict[str, Any] = {"AwsAccountId": ACCOUNT_ID}
            if next_token:
                kwargs["NextToken"] = next_token
            if use_v2:
                page = quicksight.list_topics_v2(**kwargs)
                summaries.extend(page.get("TopicSummaryList", []))
            else:
                page = quicksight.list_topics(**kwargs)
                summaries.extend(page.get("TopicsSummaries", []))
            next_token = page.get("NextToken")
            if not next_token:
                break

        listed_names = {
            s.get("TopicId"): s.get("Name") for s in summaries if s.get("TopicId")
        }
        topic_ids = set(listed_names) | (extra_topic_ids or set())
        for topic_id in sorted(topic_ids):
            try:
                response = _describe_topic_any(topic_id)
            except ClientError as error:
                print(f"WARN DescribeTopic(V2) failed for {topic_id}: {error}")
                continue
            topic = response.get("Topic", {}) or {}
            lineage[topic_id] = {
                "name": topic.get("Name") or listed_names.get(topic_id),
                "dataSetArns": _extract_topic_datasets(response),
            }
    except ClientError as error:
        print(f"WARN ListTopics unavailable, skipping topic lineage: {error}")
        return load_state("state/topic-lineage.json")  # last known good
    save_state("state/topic-lineage.json", lineage)
    return lineage


def refresh_space_lineage() -> tuple[dict, int]:
    """
    Returns (spaceId -> {name, arn, dataSetArns, topicIds, agentIds},
    total agent count).

    Spaces are the knowledge layer Chat Agents are linked to, and the only
    path from datasets to agents (agents reference spaces, spaces reference
    datasets directly or through topics). Built from ListSpaces +
    ListSpaceResources + ListAgents + DescribeAgent (all read-only, manual
    NextToken paging — none of these operations has a botocore paginator).

    Feature-detected: if the runtime's botocore predates the Space/Agent
    APIs, or the collector lacks permissions, the scan is skipped with a
    warning and the last known good state is reused.
    """
    if not hasattr(quicksight, "list_spaces") or not hasattr(quicksight, "list_agents"):
        print(
            "WARN Space/Agent APIs not available in this botocore version; "
            "skipping space lineage (bundle a newer boto3 to enable)"
        )
        return load_state("state/space-lineage.json"), 0

    lineage: dict[str, dict] = {}
    try:
        # --- spaces and their contents -------------------------------------
        spaces: list[dict] = []
        next_token: str | None = None
        while True:
            qs_pacer.wait()
            kwargs: dict[str, Any] = {"AwsAccountId": ACCOUNT_ID}
            if next_token:
                kwargs["NextToken"] = next_token
            page = quicksight.list_spaces(**kwargs)
            spaces.extend(page.get("SpaceSummaries", []))
            next_token = page.get("NextToken")
            if not next_token:
                break

        for summary in spaces:
            space_id = summary.get("spaceId") or summary.get("SpaceId")
            if not space_id:
                continue
            entry = {
                "name": summary.get("name") or summary.get("Name"),
                "arn": summary.get("spaceArn") or summary.get("SpaceArn"),
                "dataSetArns": [],
                "topicIds": [],
                "agentIds": [],
            }
            qs_pacer.wait()
            try:
                response = quicksight.list_space_resources(
                    AwsAccountId=ACCOUNT_ID, SpaceId=space_id
                )
                for resource in response.get("SpaceResources", []):
                    rtype = resource.get("ResourceType")
                    details = resource.get("ResourceDetails") or {}
                    arn = (
                        details.get("resourceArn")
                        or details.get("ResourceArn")
                        or ""
                    )
                    if not arn:
                        continue
                    if rtype == "DATA_SET":
                        entry["dataSetArns"].append(arn)
                    elif rtype == "TOPIC":
                        entry["topicIds"].append(_normalize_qs_id(arn, "topic"))
            except ClientError as error:
                print(f"WARN ListSpaceResources failed for {space_id}: {error}")
            lineage[space_id] = entry

        # --- agents -> spaces (reverse map) --------------------------------
        agents: list[dict] = []
        next_token = None
        while True:
            qs_pacer.wait()
            kwargs = {"AwsAccountId": ACCOUNT_ID}
            if next_token:
                kwargs["NextToken"] = next_token
            page = quicksight.list_agents(**kwargs)
            agents.extend(page.get("AgentSummaries", []))
            next_token = page.get("NextToken")
            if not next_token:
                break

        for summary in agents:
            agent_id = summary.get("AgentId") or summary.get("agentId")
            if not agent_id:
                continue
            qs_pacer.wait()
            try:
                response = quicksight.describe_agent(
                    AwsAccountId=ACCOUNT_ID, AgentId=agent_id
                )
                for space_arn in response.get("Agent", {}).get("Spaces", []) or []:
                    space_id = _normalize_qs_id(space_arn, "space")
                    if space_id in lineage:
                        lineage[space_id]["agentIds"].append(agent_id)
            except ClientError as error:
                print(f"WARN DescribeAgent failed for {agent_id}: {error}")
    except ClientError as error:
        print(f"WARN Space lineage unavailable, reusing last state: {error}")
        return load_state("state/space-lineage.json"), 0

    save_state("state/space-lineage.json", lineage)
    return lineage, len(agents)


def resolve_agent_scopes(
    agent_ids: set[str], space_effective: dict[str, set[str]]
) -> dict[str, set[str]]:
    """
    agent_id -> set of reachable dataset ARNs (through the agent's spaces,
    including topic-expanded membership).

    Chat traffic can carry agent IDs that ListAgents never returns (PREVIEW /
    draft versions tested in the builder), so unknown IDs are resolved with
    DescribeAgent on demand and cached in S3 for AGENT_SCOPE_CACHE_DAYS.
    Deleted agents are negative-cached the same way. Requires the Space/Agent
    APIs; without them the cache is used as-is.
    """
    cache = load_state("state/agent-scope.json")
    now = _utcnow()
    can_describe = hasattr(quicksight, "describe_agent")
    scopes: dict[str, set[str]] = {}

    for agent_id in sorted(agent_ids):
        entry = cache.get(agent_id)
        resolved_at = _parse_iso((entry or {}).get("resolvedAt"))
        stale = (
            resolved_at is None
            or (now - resolved_at).days >= AGENT_SCOPE_CACHE_DAYS
        )
        if stale and can_describe:
            spaces: list[str] = []
            qs_pacer.wait()
            try:
                response = quicksight.describe_agent(
                    AwsAccountId=ACCOUNT_ID, AgentId=agent_id
                )
                spaces = [
                    _normalize_qs_id(arn, "space")
                    for arn in response.get("Agent", {}).get("Spaces", []) or []
                ]
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code", "")
                if code != "ResourceNotFoundException":
                    print(f"WARN DescribeAgent failed for {agent_id}: {error}")
                # not found -> negative-cache with empty spaces
            entry = {"spaces": spaces, "resolvedAt": _iso(now)}
            cache[agent_id] = entry
        reachable: set[str] = set()
        for space_id in (entry or {}).get("spaces", []):
            reachable |= space_effective.get(space_id, set())
        scopes[agent_id] = reachable

    # Drop cache entries for agents with no recent conversations.
    for stale_id in set(cache) - set(agent_ids):
        cache.pop(stale_id, None)
    save_state("state/agent-scope.json", cache)
    return scopes


def _parse_chat_timestamp(event: dict, insights_ts: str | None) -> str | None:
    """Best timestamp for a chat event: its own field, else the log time."""
    raw = event.get("event_timestamp")
    if isinstance(raw, (int, float)):  # epoch millis or seconds
        seconds = raw / 1000 if raw > 10_000_000_000 else raw
        return _iso(dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc))
    if isinstance(raw, str):
        parsed = _parse_iso(raw)
        if parsed:
            return _iso(parsed)
    if insights_ts:
        try:
            parsed = dt.datetime.strptime(
                insights_ts, "%Y-%m-%d %H:%M:%S.%f"
            ).replace(tzinfo=dt.timezone.utc)
            return _iso(parsed)
        except ValueError:
            pass
    return None


def collect_agent_evidence(usage_state: dict) -> dict:
    """
    Scan the Chat and Feedback Monitor's CHAT_LOGS log group (if deployed)
    for resources that Chat Agent conversations cited or that users selected,
    and record last-seen timestamps per resource:

      agentDatasets           dataset cited in an answer        -> HIGH
      agentSelectedDatasets   dataset explicitly user-selected  -> MEDIUM
      agentTopics             topic cited/selected              -> MEDIUM (via lineage)
      agentDashboards         dashboard cited/selected          -> MEDIUM (via lineage)
      agentSpaces             space cited/selected              -> MEDIUM (via lineage)

    This is the agent-side usage evidence that dashboard telemetry cannot
    see. Non-Quick citations (documents, spaces) are ignored. The scan is
    incremental and fails soft: a missing log group or Insights error only
    logs a warning.

    Besides citations, the scan records the last conversation timestamp per
    non-SYSTEM agent (agentConversations) — joined with agent->space->dataset
    lineage in the slow loop, this yields AGENT_CONVERSATION_SCOPE evidence:
    "an agent able to reach this dataset was actively used at time T".
    """
    if not CHAT_LOG_GROUP:
        return usage_state

    now = _utcnow()
    last_run = _parse_iso(usage_state.get("agentLastRun"))
    if "agentConversations" not in usage_state:
        # Feature introduction: re-scan the full window once so existing
        # conversations (already consumed for citations) are attributed.
        last_run = None
    floor = now - dt.timedelta(days=AGENT_LOOKBACK_DAYS)
    start = max(last_run, floor) if last_run else floor

    query = (
        "fields @timestamp, @message"
        " | filter logType = 'CHAT_LOGS'"
        " | sort @timestamp desc"
        f" | limit {AGENT_QUERY_LIMIT}"
    )
    try:
        query_id = logs.start_query(
            logGroupName=CHAT_LOG_GROUP,
            startTime=int(start.timestamp()),
            endTime=int(now.timestamp()),
            queryString=query,
        )["queryId"]
        deadline = time.monotonic() + AGENT_QUERY_TIMEOUT_SECONDS
        while True:
            response = logs.get_query_results(queryId=query_id)
            status = response.get("status")
            if status == "Complete":
                break
            if status in ("Failed", "Cancelled", "Timeout"):
                print(f"WARN CHAT_LOGS Insights query ended as {status}")
                return usage_state
            if time.monotonic() > deadline:
                logs.stop_query(queryId=query_id)
                print("WARN CHAT_LOGS Insights query timed out; skipping")
                return usage_state
            time.sleep(2)
    except ClientError as error:
        print(f"WARN CHAT_LOGS scan unavailable ({CHAT_LOG_GROUP}): {error}")
        return usage_state

    if len(response.get("results", [])) >= AGENT_QUERY_LIMIT:
        usage_state["agentTelemetryTruncated"] = True

    cited_datasets = usage_state.setdefault("agentDatasets", {})
    selected_datasets = usage_state.setdefault("agentSelectedDatasets", {})
    topic_hits = usage_state.setdefault("agentTopics", {})
    dashboard_hits = usage_state.setdefault("agentDashboards", {})
    space_hits = usage_state.setdefault("agentSpaces", {})
    conversations = usage_state.setdefault("agentConversations", {})

    def _record(target: dict, kind: str, resource_id: str, ts: str) -> None:
        bare_id = _normalize_qs_id(resource_id, kind)
        previous = target.get(bare_id)
        if previous is None or ts > previous:
            target[bare_id] = ts

    def _route(resource_type: str, resource_id: str, cited: bool, ts: str) -> None:
        kind = resource_type.lower()
        if resource_id in ("", "ALL"):
            return
        if "dataset" in kind or "data_set" in kind:
            _record(
                cited_datasets if cited else selected_datasets,
                "dataset", resource_id, ts,
            )
        elif "topic" in kind:
            _record(topic_hits, "topic", resource_id, ts)
        elif "dashboard" in kind:
            _record(dashboard_hits, "dashboard", resource_id, ts)
        elif "space" in kind:
            _record(space_hits, "space", resource_id, ts)
        # documents, knowledge bases: no dataset lineage — ignored.

    for row in response.get("results", []):
        fields = {f["field"]: f["value"] for f in row}
        try:
            event = json.loads(fields.get("@message", "{}"))
        except json.JSONDecodeError:
            continue
        ts = _parse_chat_timestamp(event, fields.get("@timestamp"))
        if not ts:
            continue
        agent_id = event.get("agent_id")
        if agent_id and agent_id not in ("SYSTEM", "-"):
            previous = conversations.get(agent_id)
            if previous is None or ts > previous:
                conversations[agent_id] = ts
        cited = event.get("cited_resource")
        if isinstance(cited, list):
            for item in cited:
                if not isinstance(item, dict):
                    continue
                resource_id = item.get("citedResourceId")
                resource_type = item.get("citedResourceType") or ""
                if isinstance(resource_id, str) and resource_id:
                    _route(resource_type, resource_id, True, ts)
        selected = event.get("user_selected_resources")
        if isinstance(selected, list):
            for item in selected:
                if not isinstance(item, dict):
                    continue
                resource_id = item.get("resourceId")
                resource_type = item.get("resourceType") or ""
                if isinstance(resource_id, str) and resource_id:
                    _route(resource_type, resource_id, False, ts)

    usage_state["agentLastRun"] = _iso(now)
    return usage_state


def last_view_by_dashboard(dashboard_ids: list[str]) -> dict[str, dt.datetime]:
    """Latest non-zero DashboardViewCount datapoint per dashboard."""
    result: dict[str, dt.datetime] = {}
    remaining = list(dashboard_ids)

    def _query(ids: list[str], days: int, batch_size: int) -> None:
        end = _utcnow()
        start = end - dt.timedelta(days=days)
        for chunk_start in range(0, len(ids), batch_size):
            chunk = ids[chunk_start:chunk_start + batch_size]
            queries = [
                {
                    "Id": f"m{i}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/QuickSight",
                            "MetricName": "DashboardViewCount",
                            "Dimensions": [
                                {"Name": "DashboardId", "Value": dash_id}
                            ],
                        },
                        "Period": 86400,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                }
                for i, dash_id in enumerate(chunk)
            ]
            id_map = {f"m{i}": dash_id for i, dash_id in enumerate(chunk)}
            next_token: str | None = None
            while True:
                kwargs: dict[str, Any] = {
                    "MetricDataQueries": queries,
                    "StartTime": start,
                    "EndTime": end,
                    "ScanBy": "TimestampDescending",
                }
                if next_token:
                    kwargs["NextToken"] = next_token
                response = cloudwatch.get_metric_data(**kwargs)
                for series in response.get("MetricDataResults", []):
                    dash_id = id_map[series["Id"]]
                    if dash_id in result:
                        continue
                    for ts, value in zip(series["Timestamps"], series["Values"]):
                        if value > 0:
                            existing = result.get(dash_id)
                            if existing is None or ts > existing:
                                result[dash_id] = ts
                            break
                next_token = response.get("NextToken")
                if not next_token:
                    break

    # 90-day pass for everyone, then a 15-month pass for the silent ones.
    _query(remaining, VIEW_LOOKBACK_DAYS_PRIMARY, 500)
    silent = [d for d in remaining if d not in result]
    if silent:
        # 455 daily datapoints x 200 metrics stays under the 100,800
        # datapoints-per-call limit.
        _query(silent, VIEW_LOOKBACK_DAYS_EXTENDED, 200)
    return result


def _find_ids(node: Any, wanted_keys: set[str], found: set[str]) -> None:
    """Recursively collect string values of wanted keys in a JSON tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in wanted_keys and isinstance(value, str):
                found.add(value)
            else:
                _find_ids(value, wanted_keys, found)
    elif isinstance(node, list):
        for item in node:
            _find_ids(item, wanted_keys, found)


def collect_cloudtrail_evidence(usage_state: dict) -> dict:
    """
    Merge CloudTrail Event history evidence into usage state:
      datasets:   {datasetId: lastQueriedAt}   from QueryDatabase events
      dashboards: {dashboardId: lastViewedAt}  from GetDashboard events

    CloudTrail events reference resources by ARN (sometimes doubled), so
    every found value — and every key already in state from older collector
    versions — is normalized to the bare ID before storing/matching.
    """
    now = _utcnow()
    last_run = _parse_iso(usage_state.get("lastRun"))
    floor = now - dt.timedelta(days=CT_LOOKBACK_DAYS)
    start = max(last_run, floor) if last_run else floor

    # Migrate pre-fix state: keys may be ARNs; re-key by bare ID.
    usage_state["datasets"] = _normalize_hit_map(
        usage_state.get("datasets", {}), "dataset"
    )
    usage_state["dashboards"] = _normalize_hit_map(
        usage_state.get("dashboards", {}), "dashboard"
    )
    dataset_hits: dict[str, str] = usage_state["datasets"]
    dashboard_hits: dict[str, str] = usage_state["dashboards"]

    def _scan(
        event_name: str,
        wanted_keys: set[str],
        target: dict[str, str],
        kind: str,
    ) -> None:
        pages = 0
        paginator = cloudtrail.get_paginator("lookup_events")
        page_iter = paginator.paginate(
            LookupAttributes=[
                {"AttributeKey": "EventName", "AttributeValue": event_name}
            ],
            StartTime=start,
            EndTime=now,
        )
        for page in page_iter:
            ct_pacer.wait()
            pages += 1
            for event in page.get("Events", []):
                event_time = _iso(event.get("EventTime"))
                try:
                    detail = json.loads(event.get("CloudTrailEvent", "{}"))
                except json.JSONDecodeError:
                    continue
                found: set[str] = set()
                _find_ids(detail, wanted_keys, found)
                for resource_id in found:
                    bare_id = _normalize_qs_id(resource_id, kind)
                    previous = target.get(bare_id)
                    if event_time and (previous is None or event_time > previous):
                        target[bare_id] = event_time
            if pages >= MAX_CT_PAGES_PER_EVENT_NAME:
                print(f"WARN CloudTrail scan for {event_name} hit page cap")
                usage_state["telemetryTruncated"] = True
                break

    _scan(
        "QueryDatabase",
        {"dataSetId", "datasetId", "DataSetId"},
        dataset_hits,
        "dataset",
    )
    _scan(
        "GetDashboard",
        {"dashboardId", "DashboardId"},
        dashboard_hits,
        "dashboard",
    )
    _scan(
        "GetDashboardEmbedUrl",
        {"dashboardId", "DashboardId"},
        dashboard_hits,
        "dashboard",
    )

    usage_state["lastRun"] = _iso(now)
    return usage_state


def run_slow_loop() -> dict:
    started = time.monotonic()
    api_errors = 0
    now = _utcnow()
    now_iso = _iso(now)

    datasets = list_datasets()
    lineage_state = refresh_dashboard_lineage()
    space_lineage, agent_count = refresh_space_lineage()
    space_topic_ids = {
        topic_id
        for info in space_lineage.values()
        for topic_id in info.get("topicIds", [])
    }
    topic_lineage = refresh_topic_lineage(space_topic_ids)

    # datasetArn -> [dashboardId] / [topicId]
    arn_to_dashboards: dict[str, list[str]] = {}
    for dashboard_id, info in lineage_state.items():
        for dataset_arn in info.get("dataSetArns", []):
            arn_to_dashboards.setdefault(dataset_arn, []).append(dashboard_id)
    arn_to_topics: dict[str, list[str]] = {}
    for topic_id, info in topic_lineage.items():
        for dataset_arn in info.get("dataSetArns", []):
            arn_to_topics.setdefault(dataset_arn, []).append(topic_id)

    # spaceId -> effective dataset membership: datasets directly in the
    # space plus datasets of topics in the space. Agents reach datasets only
    # through spaces, so agent lineage derives from this map.
    space_effective: dict[str, set[str]] = {}
    for space_id, info in space_lineage.items():
        effective = set(info.get("dataSetArns", []))
        for topic_id in info.get("topicIds", []):
            effective.update(
                topic_lineage.get(topic_id, {}).get("dataSetArns", [])
            )
        space_effective[space_id] = effective
    arn_to_spaces: dict[str, list[str]] = {}
    for space_id, effective in space_effective.items():
        for dataset_arn in effective:
            arn_to_spaces.setdefault(dataset_arn, []).append(space_id)

    view_times = last_view_by_dashboard(list(lineage_state.keys()))

    usage_state = load_state("state/usage-state.json")
    if ENABLE_CLOUDTRAIL:
        try:
            usage_state = collect_cloudtrail_evidence(usage_state)
        except ClientError as error:
            api_errors += 1
            print(f"WARN CloudTrail evidence collection failed: {error}")
    usage_state = collect_agent_evidence(usage_state)
    save_state("state/usage-state.json", usage_state)
    ct_dataset_hits = usage_state.get("datasets", {})
    ct_dashboard_hits = usage_state.get("dashboards", {})
    agent_cited_datasets = usage_state.get("agentDatasets", {})
    agent_selected_datasets = usage_state.get("agentSelectedDatasets", {})
    agent_topic_hits = usage_state.get("agentTopics", {})
    agent_dashboard_hits = usage_state.get("agentDashboards", {})
    agent_space_hits = usage_state.get("agentSpaces", {})
    agent_conversations = usage_state.get("agentConversations", {})

    # AGENT_CONVERSATION_SCOPE: join conversations (CHAT_LOGS) with the
    # agent's reachable datasets (lineage). Scope-level evidence — "an agent
    # able to reach this dataset was actively used" — MEDIUM for tightly
    # scoped agents, LOW for broad ones. SYSTEM is never recorded.
    agent_scopes = resolve_agent_scopes(
        set(agent_conversations), space_effective
    )
    scope_hits: dict[str, tuple[dt.datetime, str]] = {}
    for agent_id, conv_ts_str in agent_conversations.items():
        conv_ts = _parse_iso(conv_ts_str)
        reachable = agent_scopes.get(agent_id, set())
        if conv_ts is None or not reachable:
            continue
        confidence = (
            "MEDIUM" if len(reachable) <= AGENT_SCOPE_MEDIUM_MAX else "LOW"
        )
        for dataset_arn in reachable:
            current = scope_hits.get(dataset_arn)
            candidate = (conv_ts, confidence)
            if current is None or (
                candidate[0],
                CONFIDENCE_RANK.get(candidate[1], 0),
            ) > (current[0], CONFIDENCE_RANK.get(current[1], 0)):
                scope_hits[dataset_arn] = candidate

    records: list[dict] = []
    no_observed_use = 0
    unreferenced = 0
    total_spice_gb = 0.0

    for summary in datasets:
        dataset_id = summary["DataSetId"]
        dataset_arn = summary["Arn"]
        import_mode = summary.get("ImportMode")
        dashboard_ids = arn_to_dashboards.get(dataset_arn, [])
        topic_ids = arn_to_topics.get(dataset_arn, [])
        space_ids = arn_to_spaces.get(dataset_arn, [])
        agent_ids = sorted({
            agent_id
            for space_id in space_ids
            for agent_id in space_lineage.get(space_id, {}).get("agentIds", [])
        })

        # --- usage evidence -------------------------------------------------
        # Candidates: (timestamp, confidence, source). Highest timestamp wins;
        # on a tie, the higher-confidence source wins.
        candidates: list[tuple[dt.datetime, str, str]] = []

        # Agent evidence (CHAT_LOGS): the consumption path dashboards miss.
        agent_cited_at = _parse_iso(agent_cited_datasets.get(dataset_id))
        if agent_cited_at:
            candidates.append((agent_cited_at, "HIGH", "AGENT_CITED_DATASET"))
        agent_selected_at = _parse_iso(agent_selected_datasets.get(dataset_id))
        if agent_selected_at:
            candidates.append(
                (agent_selected_at, "MEDIUM", "AGENT_SELECTED_RESOURCE")
            )
        for topic_id in topic_ids:
            topic_at = _parse_iso(agent_topic_hits.get(topic_id))
            if topic_at:
                candidates.append((topic_at, "MEDIUM", "AGENT_CITED_TOPIC"))
        for space_id in space_ids:
            space_at = _parse_iso(agent_space_hits.get(space_id))
            if space_at:
                candidates.append((space_at, "MEDIUM", "AGENT_USED_SPACE"))
        scope_hit = scope_hits.get(dataset_arn)
        if scope_hit:
            candidates.append(
                (scope_hit[0], scope_hit[1], "AGENT_CONVERSATION_SCOPE")
            )

        # CloudTrail QueryDatabase: HIGH for direct-query datasets (queries
        # only happen when something actually uses them). For SPICE the
        # signal is ambiguous — verified empirically: interactive sessions
        # emit service-initiated queries (e.g. "dataset-prewarm-count") AND
        # refreshes query the source too — so it counts as MEDIUM, never
        # discarded.
        queried_at = _parse_iso(ct_dataset_hits.get(dataset_id))
        if queried_at:
            confidence = "MEDIUM" if import_mode == "SPICE" else "HIGH"
            candidates.append((queried_at, confidence, "CLOUDTRAIL_QUERY"))

        # Dashboard-view evidence (native metric + CloudTrail + agent cite).
        for dashboard_id in dashboard_ids:
            metric_ts = view_times.get(dashboard_id)
            if metric_ts:
                candidates.append((metric_ts, "MEDIUM", "DASHBOARD_VIEW_METRIC"))
            trail_ts = _parse_iso(ct_dashboard_hits.get(dashboard_id))
            if trail_ts:
                candidates.append(
                    (trail_ts, "MEDIUM", "DASHBOARD_VIEW_CLOUDTRAIL")
                )
            agent_dash_ts = _parse_iso(agent_dashboard_hits.get(dashboard_id))
            if agent_dash_ts:
                candidates.append(
                    (agent_dash_ts, "MEDIUM", "AGENT_CITED_DASHBOARD")
                )

        is_referenced = bool(dashboard_ids or topic_ids or space_ids)
        last_used_at: dt.datetime | None = None
        usage_confidence = "NO_EVIDENCE"
        usage_source = None
        if candidates:
            candidates.sort(
                key=lambda c: (c[0], CONFIDENCE_RANK.get(c[1], 0)),
                reverse=True,
            )
            last_used_at, usage_confidence, usage_source = candidates[0]
        elif is_referenced:
            usage_confidence = "LOW"
            usage_source = "DEPENDENCY_ONLY"

        days_since_use = (
            (now - last_used_at).days if last_used_at is not None else None
        )

        # --- capacity + estimated cost (SPICE only, best effort) -----------
        spice_gb: float | None = None
        cost_type = "UNKNOWN"
        est_monthly_cost: float | None = None
        if import_mode == "SPICE":
            try:
                spice_gb, _supported = describe_spice_capacity(dataset_id)
            except ClientError as error:
                api_errors += 1
                print(f"WARN DescribeDataSet failed for {dataset_id}: {error}")
            if spice_gb is not None:
                total_spice_gb += spice_gb
                cost_type = "ESTIMATED_PURCHASED"
                est_monthly_cost = round(spice_gb * SPICE_RATE_PER_GB_MONTH, 2)

        # --- findings -------------------------------------------------------
        findings: list[str] = []
        if import_mode == "SPICE" and not is_referenced:
            findings.append("SPICE_DATASET_NOT_REFERENCED")
            unreferenced += 1
        evidence_window_covers_threshold = (
            VIEW_LOOKBACK_DAYS_EXTENDED >= UNUSED_THRESHOLD_DAYS
        )
        if (
            is_referenced
            and evidence_window_covers_threshold
            and (days_since_use is None or days_since_use > UNUSED_THRESHOLD_DAYS)
        ):
            findings.append("NO_OBSERVED_USE")
            no_observed_use += 1

        records.append({
            "ts": now_iso,
            "loop": "slow",
            "datasetId": dataset_id,
            "datasetName": summary.get("Name"),
            "importMode": import_mode,
            "assetState": "ACTIVE",
            "dependentDashboardCount": len(dashboard_ids),
            "dependentTopicCount": len(topic_ids),
            "dependentSpaceCount": len(space_ids),
            "dependentAgentCount": len(agent_ids),
            "lastObservedUseAt": _iso(last_used_at),
            "daysSinceObservedUse": days_since_use,
            "usageConfidence": usage_confidence,
            "usageSource": usage_source,
            "viewLookbackDays": VIEW_LOOKBACK_DAYS_EXTENDED,
            "queryLookbackDays": CT_LOOKBACK_DAYS if ENABLE_CLOUDTRAIL else 0,
            "agentLookbackDays": AGENT_LOOKBACK_DAYS if CHAT_LOG_GROUP else 0,
            "spiceGB": spice_gb,
            "estMonthlyCostUsd": est_monthly_cost,
            "costType": cost_type,
            "findings": findings,
            "findingsCsv": ",".join(findings),
        })

    records.extend(reconcile_deleted_datasets("slow", datasets))
    put_data_events("slow", records)
    write_snapshot("slow", records)

    scan_seconds = round(time.monotonic() - started)
    put_kpis({
        "NoObservedUseDatasets": no_observed_use,
        "UnreferencedDatasets": unreferenced,
        "ConsumedSpiceGB": round(total_spice_gb, 2),
        "DashboardsTracked": len(lineage_state),
        "TopicsTracked": len(topic_lineage),
        "SpacesTracked": len(space_lineage),
        "AgentsTracked": agent_count,
        "ApiErrors": api_errors,
        "SlowScanDurationSeconds": scan_seconds,
    })
    return {
        "mode": "slow", "datasets": len(datasets),
        "dashboards": len(lineage_state), "topics": len(topic_lineage),
        "spaces": len(space_lineage), "agents": agent_count,
        "noObservedUse": no_observed_use,
        "unreferenced": unreferenced, "apiErrors": api_errors,
        "seconds": scan_seconds,
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, _context: Any) -> dict:
    mode = (event or {}).get("mode", "fast")
    print(f"Starting dataset-lifecycle collector: mode={mode}")
    if mode == "slow":
        result = run_slow_loop()
    else:
        result = run_fast_loop()
    print(f"Done: {json.dumps(result)}")
    return result
