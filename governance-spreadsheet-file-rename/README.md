# Amazon Quick — Spreadsheet File Rename

> [!IMPORTANT]
> **Sample code — review and adapt before production use.** Not an AWS service and not supported by AWS; see the [repository disclaimer](../README.md#disclaimer).

Automatically prefix the name of any **Amazon Quick (QuickSight) dataset created from an uploaded
spreadsheet** (Microsoft Excel `.xlsx` by default) so it becomes **`xls-<original name>`**. Zero
change to the upload experience — users upload as usual, and the dataset is renamed within seconds.

Mechanism: an **EventBridge → Lambda** automation. Amazon Quick emits a
`QuickSight DataSet Created` event in near real time; a Lambda reads the new dataset, detects the
upload format, and renames it. Because the AWS SDKs don't yet model the file-upload physical table,
the Lambda calls `DescribeDataSet`/`UpdateDataSet` as **raw, SigV4-signed REST requests** — see
[Why it uses the raw QuickSight API](#why-it-uses-the-raw-quicksight-api-boto3-limitation). The
handler is idempotent and loop-safe.

## Module layout

```
governance-spreadsheet-file-rename/
├── README.md                                          ← this file
├── lambda/
│   └── app.py                                         ← Lambda handler (source of the deployed code)
├── cloudformation/
│   └── governance-spreadsheet-file-rename.yaml        ← SAM template — the single source of truth
└── scripts/
    ├── apply-governance-spreadsheet-file-rename.sh    ← thin wrapper around `sam deploy`
    └── remove-governance-spreadsheet-file-rename.sh   ← thin wrapper around `sam delete`
```

The SAM template defines the whole stack; the scripts just drive `sam deploy` / `sam delete`.

## How it works

```
  User uploads file.xlsx in Amazon Quick  ──creates dataset──▶  EventBridge (default bus)
                                                                 source      : aws.quicksight
                                                                 detail-type : QuickSight DataSet
                                                                               Created
                                                                 detail      : {"dataSetId": "..."}
                                                                        │ rule match
                                                                        ▼
   Lambda: quick-governance-spreadsheet-file-rename-fn
     1. DescribeDataSet (raw REST)  → read PhysicalTableMap incl. FileSource
     2. FileSource/S3Source .UploadSettings.Format == TARGET_FORMAT (default XLSX)?
     3. Name already starts with PREFIX (default "xls-")?  → skip
     4. UpdateDataSet (raw REST)    → replay the physical table with Name = PREFIX + old name
```

Why it is safe:

- The rename emits `QuickSight DataSet Updated` (a different detail-type), so it does not
  re-trigger a rule that only listens for `Created`.
- Even if you opt in to `Updated` events, the handler short-circuits when the name already starts
  with the prefix (`already_prefixed`), breaking the loop after a single rename.

## Why it uses the raw QuickSight API (boto3 limitation)

Datasets created by the Amazon Quick "upload a file" experience store their physical table as a
**`FileSource`**. As of **botocore 1.42.x / aws-cli 2.33.x**, the AWS SDKs do **not** model
`FileSource` in the `PhysicalTable` type — they know only `RelationalTable`, `CustomSql`, `S3Source`,
and `SaaSTable`. As a result, boto3 silently **drops** the file source when reading
(`DescribeDataSet` returns an empty physical table `{}`) and **cannot send** it when writing
(`UpdateDataSet` fails with `Invalid PhysicalTableMap`). The underlying **QuickSight REST API does
support `FileSource`** — it is exactly what the console's "edit → Save & publish" uses — so this
module calls `DescribeDataSet` and `UpdateDataSet` as raw, SigV4-signed REST requests (using
botocore's request signer, so **no extra dependencies**) and replays the file source verbatim with
the new name.

**Customer impact:** the rename works today for uploaded-file datasets, but it relies on an API
capability that is *ahead of the published SDKs*. It is therefore slightly more fragile (a future
SDK/API change could require an update) and can't be expressed with the typed boto3 client yet. When
AWS adds `FileSource` to the SDK model, this handler can be simplified back to a standard boto3 call
with no behavior change.

## AWS resource naming convention

Every resource uses the prefix **`quick-governance-spreadsheet-file-rename-`**:

| Resource | Default name |
|---|---|
| Lambda function | `quick-governance-spreadsheet-file-rename-fn` |
| IAM role | `quick-governance-spreadsheet-file-rename-role` |
| IAM inline policy | `quick-governance-spreadsheet-file-rename-policy` |
| EventBridge rule | `quick-governance-spreadsheet-file-rename-rule` |
| CloudWatch log group | `/aws/lambda/quick-governance-spreadsheet-file-rename-fn` |
| CloudWatch error alarm | `quick-governance-spreadsheet-file-rename-errors` |
| Resource tag (`Purpose=`) | `quick-governance-spreadsheet-file-rename` |
| Recommended stack name | `quick-governance-spreadsheet-file-rename` |

Override any of these via script flags or the `ResourcePrefix` parameter.

## What this deploys

| Resource | Purpose |
|---|---|
| `AWS::Lambda::Function` (Python 3.14, arm64/Graviton) | Detects the upload format and renames the dataset via the raw QuickSight REST API. |
| `AWS::IAM::Role` + inline policy | Least-privilege: `quicksight:DescribeDataSet` / `UpdateDataSet` / `CreateIngestion` (SPICE re-ingest on rename) on `dataset/*`, `quicksight:PassDataSource` (reference the uploaded file's data source) on `datasource/*`, and CloudWatch Logs. |
| `AWS::Events::Rule` + invoke permission | Matches `QuickSight DataSet Created` (optionally `... Updated`). |
| `AWS::Logs::LogGroup` | The function's log group, with a configurable retention (default 90 days). |
| `AWS::CloudWatch::Alarm` | Fires on any Lambda error within a 5-minute window. |

The Lambda runs on the **`python3.14`** runtime on **arm64 (Graviton)** for better price-performance,
and has no third-party dependencies (only the runtime-provided `botocore`), so SAM packages
`lambda/app.py` as-is — no build step is required. Transient QuickSight throttling (429) and 5xx
responses are retried with exponential backoff and jitter.

## Prerequisites

- An Amazon Quick (QuickSight) subscription in the AWS account and Region you deploy into.
  EventBridge events are emitted in the Region where the *dataset resource* lives — deploy there.
  (Note: your Quick *identity* Region can differ from the Region where datasets are created; deploy
  where the datasets — and thus the events — are.)
- **AWS SAM CLI** installed —
  [install guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).
- AWS CLI v2 authenticated (the scripts read stack outputs with it).
- `--resolve-s3` lets SAM create/manage the deployment-artifact bucket automatically.

**Same-account:** the stack targets the account it is deployed into via the `AWS::AccountId`
pseudo-parameter (resolved from your credentials/profile), so there is no account ID to pass. The
Quick subscription must live in that same account. Cross-account is out of scope.

## Deploy

```bash
cd scripts
./apply-governance-spreadsheet-file-rename.sh --region us-east-1
```

Pass `--profile <name>` (or set `AWS_PROFILE`) to choose the account/credentials to deploy with.
The script is a thin wrapper around `sam deploy`: it packages `lambda/app.py`, deploys/updates the
stack idempotently, and prints the outputs. Re-run it any time to change configuration.

Common options:

```bash
# Named profile, also rename on re-uploads, custom dataset prefix
./apply-governance-spreadsheet-file-rename.sh \
    --region us-east-1 --profile my-profile \
    --also-on-update --prefix excel-

# Act on CSV uploads instead of XLSX
./apply-governance-spreadsheet-file-rename.sh \
    --region us-east-1 --target-format CSV --prefix csv-
```

Deploying directly with SAM (equivalent to the script):

```bash
sam deploy \
    --template-file cloudformation/governance-spreadsheet-file-rename.yaml \
    --stack-name quick-governance-spreadsheet-file-rename \
    --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
    --resolve-s3 --no-confirm-changeset \
    --region us-east-1
```

## Parameters

Set via script flags or `sam deploy --parameter-overrides`:

| Parameter | Script flag | Default | Purpose |
|---|---|---|---|
| `ResourcePrefix` | `--resource-prefix` | `quick-governance-spreadsheet-file-rename` | Prefix for every resource name. |
| `Prefix` | `--prefix` | `xls-` | Prefix prepended to dataset names. |
| `TargetFormat` | `--target-format` | `XLSX` | Upload format to act on: `CSV TSV CLF ELF XLSX JSON`. |
| `AlsoOnUpdate` | `--also-on-update` | `false` | Also match `QuickSight DataSet Updated` events. |
| `ReservedConcurrency` | `--reserved-concurrency` | `5` | Throttle protection (set `0` to disable the function). |
| `LogRetentionDays` | `--log-retention-days` | `90` | CloudWatch Logs retention for the function's log group. |

The account is not a parameter — it is resolved from your deploy credentials/profile via
`AWS::AccountId`.

## Verify

1. In Amazon Quick, create a dataset by uploading a small `.xlsx` (e.g. `sales.xlsx`).
2. The dataset first appears as `sales`, then within seconds becomes `xls-sales`.
3. Tail the logs to see the decision:

```bash
aws logs tail /aws/lambda/quick-governance-spreadsheet-file-rename-fn --since 10m --follow --region us-east-1
```

| Symptom | Likely cause |
|---|---|
| No invocation | Events fire in the Region where the dataset is created — deploy the stack there. |
| `skipped: not_target_format` | The dataset isn't an upload in `TARGET_FORMAT` (e.g. an Athena/RDS source) — expected. |
| `skipped: already_prefixed` | The name already starts with the prefix — expected. |
| `UpdateDataSet ... Invalid PhysicalTableMap` | Running on an SDK/path that stripped `FileSource` — the handler uses the raw REST API to avoid this. |

## Remove

```bash
cd scripts
./remove-governance-spreadsheet-file-rename.sh --region us-east-1
```

A thin wrapper around `sam delete`. Equivalent:

```bash
sam delete --stack-name quick-governance-spreadsheet-file-rename --region us-east-1 --no-prompts
```

## Cost

Effectively pay-per-use and negligible at typical dataset-creation volumes: a few short Lambda
invocations per upload, plus CloudWatch Logs storage. No always-on resources.

## References

- [Amazon Quick events integration (EventBridge)](https://docs.aws.amazon.com/quick/latest/userguide/events-integration.html)
- [EventBridge events reference — Amazon Quick / QuickSight](https://docs.aws.amazon.com/eventbridge/latest/ref/events-ref-quicksight.html)
- [Creating a dataset using a Microsoft Excel file](https://docs.aws.amazon.com/quick/latest/userguide/create-a-data-set-excel.html)
- [API_UpdateDataSet](https://docs.aws.amazon.com/quicksight/latest/APIReference/API_UpdateDataSet.html) · [API_DescribeDataSet](https://docs.aws.amazon.com/quicksight/latest/APIReference/API_DescribeDataSet.html)
- [AWS Lambda Python runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-python.html) · [Install the AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
