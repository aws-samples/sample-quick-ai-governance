"""
spreadsheet-file-rename — Lambda handler
========================================

Auto-renames Amazon Quick (QuickSight) datasets created from an uploaded
Microsoft Excel (.xlsx) file by prefixing the dataset name with ``xls-``.

Trigger
-------
Amazon EventBridge rule on:
    source       : aws.quicksight
    detail-type  : "QuickSight DataSet Created"
                   (and optionally "QuickSight DataSet Updated")
    detail       : {"dataSetId": "<id>"}

Why this calls the raw QuickSight REST API instead of boto3
-----------------------------------------------------------
Datasets created by the Amazon Quick "upload a file" flow store their physical
table as a ``FileSource``. As of botocore 1.42.x / aws-cli 2.33.x the SDK's
``PhysicalTable`` shape models only RelationalTable, CustomSql, S3Source (and
SaaSTable) -- it does NOT model ``FileSource``. So boto3:
  * drops FileSource when reading  -> DescribeDataSet returns an empty {} table, and
  * cannot send FileSource on write -> UpdateDataSet fails "Invalid PhysicalTableMap".
The QuickSight REST API itself accepts/returns FileSource -- it is exactly what
the console's "edit -> Save & publish" uses. So this handler signs raw SigV4
DescribeDataSet / UpdateDataSet requests and replays the physical table verbatim
with a new Name. When AWS adds FileSource to boto3, this can become a typed call.

Idempotency / loop-safety
-------------------------
* Skips datasets whose name already starts with ``PREFIX``.
* The rename emits "QuickSight DataSet Updated" (a different detail-type), so it
  does not retrigger this function when only "Created" is subscribed.

Required environment variables
------------------------------
    QS_ACCOUNT_ID    AWS account ID hosting the Amazon Quick subscription.
    PREFIX           Prefix to apply (default: "xls-").
    TARGET_FORMAT    Upload format to act on (default: "XLSX").

Required IAM
------------
    quicksight:DescribeDataSet
    quicksight:UpdateDataSet
    (CloudWatch Logs basic execution role)
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from typing import Any, Dict, Optional

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session
from botocore.session import Session

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

# --- configuration -----------------------------------------------------------

ACCOUNT_ID: str = os.environ["QS_ACCOUNT_ID"]
PREFIX: str = os.environ.get("PREFIX", "xls-")
TARGET_FORMAT: str = os.environ.get("TARGET_FORMAT", "XLSX").upper()
REGION: str = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "us-east-1"
)

_SERVICE = "quicksight"
_ENDPOINT = f"https://quicksight.{REGION}.amazonaws.com"

# botocore gives us SigV4 signing + an HTTP client with no extra dependencies.
_credentials = Session().get_credentials()
_http = URLLib3Session()

# Fields DescribeDataSet returns that UpdateDataSet also accepts. They are
# replayed unchanged so a rename never drops dataset configuration.
_PASSTHROUGH_FIELDS = (
    "LogicalTableMap",
    "ColumnGroups",
    "FieldFolders",
    "RowLevelPermissionDataSet",
    "RowLevelPermissionTagConfiguration",
    "ColumnLevelPermissionRules",
    "DataSetUsageConfiguration",
    "DatasetParameters",
    "PerformanceConfiguration",
    "DataPrepConfiguration",       # new Amazon Quick data-prep model
    "SemanticModelConfiguration",  # new Amazon Quick semantic model
)


# --- raw QuickSight REST helpers (bypass boto3's FileSource-less model) -------

# Retry transient failures (throttling / 5xx) with exponential backoff + jitter.
# This restores the resilience the boto3 client's adaptive retries would provide,
# since we sign raw requests here.
_MAX_ATTEMPTS = 5
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _signed_request(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{_ENDPOINT}{path}"
    data = json.dumps(body) if body is not None else None
    base_headers = {"Content-Type": "application/json"} if data is not None else {}

    last_error: Optional[Exception] = None
    for attempt in range(_MAX_ATTEMPTS):
        # Re-sign every attempt so the SigV4 timestamp stays fresh.
        request = AWSRequest(method=method, url=url, data=data, headers=dict(base_headers))
        SigV4Auth(_credentials, _SERVICE, REGION).add_auth(request)
        try:
            response = _http.send(request.prepare())
        except Exception as err:  # noqa: BLE001 - transport error, retry
            last_error = err
        else:
            text = response.text or ""
            if response.status_code < 300:
                return json.loads(text) if text else {}
            error = RuntimeError(f"{method} {path} -> HTTP {response.status_code}: {text}")
            if response.status_code not in _RETRYABLE_STATUS:
                raise error
            last_error = error
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(min(2 ** attempt, 10) + random.uniform(0, 0.3))
    raise last_error or RuntimeError(f"{method} {path} failed after {_MAX_ATTEMPTS} attempts")


def _describe_data_set(dataset_id: str) -> Dict[str, Any]:
    path = f"/accounts/{ACCOUNT_ID}/data-sets/{dataset_id}"
    return _signed_request("GET", path).get("DataSet", {})


def _update_data_set(dataset_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    path = f"/accounts/{ACCOUNT_ID}/data-sets/{dataset_id}"
    return _signed_request("PUT", path, body)


# --- helpers -----------------------------------------------------------------

def _is_target_format(dataset: Dict[str, Any]) -> bool:
    """True if any physical table is an uploaded file in TARGET_FORMAT.

    Handles the new Amazon Quick ``FileSource`` uploads and classic ``S3Source``
    uploads; both expose the format at ``UploadSettings.Format``.
    """
    for table in (dataset.get("PhysicalTableMap") or {}).values():
        for source_key in ("FileSource", "S3Source"):
            fmt = (
                table.get(source_key, {}).get("UploadSettings", {}).get("Format", "")
                or ""
            ).upper()
            if fmt == TARGET_FORMAT:
                return True
    return False


def _build_update_body(dataset: Dict[str, Any], new_name: str) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "Name": new_name,
        "PhysicalTableMap": dataset["PhysicalTableMap"],
        "ImportMode": dataset["ImportMode"],
    }
    for field in _PASSTHROUGH_FIELDS:
        if dataset.get(field) is not None:
            body[field] = dataset[field]
    return body


# --- handler -----------------------------------------------------------------

def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Entry point for EventBridge -> Lambda."""
    detail = event.get("detail") or {}
    # The live "QuickSight DataSet Created" event carries "dataSetId" (capital S).
    dataset_id = detail.get("dataSetId") or detail.get("datasetId")
    if not dataset_id:
        LOG.warning("No dataSetId in event; ignoring. event=%s", event)
        return {"skipped": "no_dataset_id"}

    try:
        dataset = _describe_data_set(dataset_id)
    except Exception as err:  # noqa: BLE001 - surface for the error alarm
        LOG.error("DescribeDataSet failed for %s: %s", dataset_id, err)
        raise

    if not dataset:
        LOG.warning("DescribeDataSet returned no DataSet for %s", dataset_id)
        return {"skipped": "no_dataset", "dataSetId": dataset_id}

    if not _is_target_format(dataset):
        LOG.info(
            "Skip %s: not a %s upload (name=%s)",
            dataset_id, TARGET_FORMAT, dataset.get("Name"),
        )
        return {"skipped": "not_target_format", "dataSetId": dataset_id}

    current_name = dataset.get("Name", "")
    if current_name.startswith(PREFIX):
        LOG.info("Skip %s: already prefixed (%s)", dataset_id, current_name)
        return {"skipped": "already_prefixed", "dataSetId": dataset_id}

    new_name = f"{PREFIX}{current_name}"
    try:
        _update_data_set(dataset_id, _build_update_body(dataset, new_name))
    except Exception as err:  # noqa: BLE001 - surface for the error alarm
        LOG.error("UpdateDataSet failed for %s: %s", dataset_id, err)
        raise

    LOG.info("Renamed dataset %s: '%s' -> '%s'", dataset_id, current_name, new_name)
    return {
        "renamed": True,
        "dataSetId": dataset_id,
        "previousName": current_name,
        "newName": new_name,
    }
