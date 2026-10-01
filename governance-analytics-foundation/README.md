# Amazon Quick — Governance Analytics Foundation

Shared, once-per-Region prerequisites of the four monitoring modules (Agent Hours, Chat and
Feedback, Dataset Lifecycle, Orphaned Assets) and their **native Amazon Quick dashboards**: an AWS
Glue database, an Amazon Athena workgroup with its own query-results bucket, a shared analytics
bucket that modules deliver into under per-module prefixes, and one Amazon Quick data source
(Athena). Deploy it first; Block Sharing and Spreadsheet File Rename do not need it.

## Module layout

```
governance-analytics-foundation/
├── README.md
├── cloudformation/
│   └── governance-analytics-foundation.yaml         # the stack (source of truth)
└── scripts/
    ├── apply-governance-analytics-foundation.sh      # wraps: aws cloudformation deploy
    └── remove-governance-analytics-foundation.sh     # wraps: aws cloudformation delete-stack
```

## What this stack creates

| Resource | Purpose |
|---|---|
| `AWS::S3::Bucket` `<prefix>-<account-id>-<region>` | Shared analytics bucket. Modules deliver under `agent-hours/`, `chat-feedback/`, `dataset-lifecycle/`, `orphaned-assets/`; the collectors keep their working state under `orphaned-assets/state/` and `dataset-lifecycle/state/`. Age-based expiry applies to the data prefixes only — state and `chat-feedback/agent-names/` are kept until rewritten. Retained on delete. |
| `AWS::S3::BucketPolicy` | Lets `delivery.logs.amazonaws.com` (this account) write under the vended-log prefixes; TLS-only. |
| `AWS::S3::Bucket` `aws-athena-query-results-quick-gov-<account-id>-<region>` | Athena results. The name pattern is covered by the `AWSQuicksightAthenaAccess` policy already on Quick's service role, so no extra authorization. |
| `AWS::S3::Bucket` access-log bucket | Server access logs for the two buckets above. |
| `AWS::Glue::Database` | `quick_governance` — one table per module, created by the module dashboard stacks. |
| `AWS::Athena::WorkGroup` | `quick-governance` — enforced results location, engine v3, SSE-S3. |
| `AWS::QuickSight::DataSource` | Athena data source used by every module dataset; owned by `QuickPrincipalArn`. |
| `AWS::IAM::Policy` on Quick's service role | Scoped grant so Quick can read the analytics bucket, use the workgroup and results bucket, and read the Glue database — replaces the console's "AWS resources" step. Carries an explicit Deny on `*/state/*`, so collector working state is unreadable through any Quick data source. Skipped when `QuickServiceRoleName` is empty. |

Exports: `<stack>-GlueDatabase`, `<stack>-AthenaWorkGroup`, `<stack>-QuickDataSourceArn`,
`<stack>-AnalyticsBucket`. Module dashboard stacks import them, which also enforces the teardown
order (modules first, foundation last).

## Prerequisites

- An Amazon Quick subscription in this account and Region.
- A Quick **user or group ARN** to own the data source. It lives in the account's Quick *identity*
  Region, which can differ from the Region you deploy into — list it with
  `aws quicksight list-users --aws-account-id <account> --namespace default --region <identity-region>`
  (the error message of a wrong-Region call names the right one).
- Quick's service role, `aws-quicksight-service-role-v0` in most accounts (`aws iam get-role` confirms
  it). If your account uses a different or custom role, pass `--quick-service-role <name>`.

## Deploy

```bash
cd scripts
./apply-governance-analytics-foundation.sh --region us-east-1 \
    --quick-principal-arn arn:aws:quicksight:sa-east-1:123456789012:user/default/admin
```

The deploy needs `CAPABILITY_IAM` (the script passes it) because the stack attaches a policy to Quick's
service role. That policy is what the Quick console's **AWS resources** page would otherwise create by
hand — verified: the console step attaches nothing but `s3:ListBucket` and `s3:GetObject` on the ticked
buckets — so **no console step is needed** in the default configuration. Verified end to end: with the
analytics bucket un-ticked in the console, dashboards whose datasets read that bucket still load, and
the Athena workgroup history shows Quick's queries succeeding on the CloudFormation grant alone. Two
things to know:

- The console page only lists grants it created itself, so the analytics bucket will *not* appear
  ticked there. Access works regardless; do not "fix" it by ticking the bucket (harmless, just redundant).
- Accounts that govern S3 data through **AWS Lake Formation**, or that run Quick with a custom IAM
  role, need Lake Formation grants or their own policy instead: pass `--quick-service-role ""` to skip
  the grant and follow the `ManualStep` stack output (Manage Quick → Security & permissions → AWS
  resources → Manage → enable Amazon Athena and select the analytics bucket).
- The grant covers the shared analytics bucket and this stack's Athena results bucket only. A dashboard
  deployed with `--data-bucket` (reading a module-owned bucket instead of the shared one) still needs that
  bucket ticked on the console's **AWS resources** page — the one configuration where the console step
  remains. The console's own `AWSQuicksightAthenaAccess` policy is not required by this project either
  (the grant names the `quick-governance` workgroup, catalog and database explicitly); keep it if other
  Quick datasets in the account use Athena.

## Module dashboards

Each module ships its Quick dashboard as a separate template that imports this stack's exports,
for example
[`governance-agent-hours-monitor/cloudformation/governance-agent-hours-quick-dashboard.yaml`](../governance-agent-hours-monitor/cloudformation/governance-agent-hours-quick-dashboard.yaml).
Datasets are **Direct Query** over Athena: no SPICE capacity, no refresh schedules, and freshness
equals S3 delivery latency plus query time — seconds after a vended-log delivery or a collector run.
Operational signals (alarms, SNS) stay in CloudWatch, where the modules publish their KPI metrics.

## Remove

```bash
cd scripts
./remove-governance-analytics-foundation.sh --region us-east-1
```

Fails while any module dashboard stack still imports the exports — remove those first. The three
buckets are retained; delete them manually with `aws s3 rb --force` when no longer needed.

## Cost

Glue database and Athena workgroup: free. Athena bills per TB scanned per query (governance data
is megabytes; Direct Query issues one query per visual per dashboard load). S3 storage for
analytics data and results is cents. Quick pricing is per user/capacity and independent of this
stack.

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| `ResourcePrefix` | `quick-governance-analytics` | Names resources and buckets; lowercase/DNS-safe. |
| `GlueDatabaseName` | `quick_governance` | Glue database for the module tables. |
| `AthenaWorkGroupName` | `quick-governance` | Athena workgroup used by the Quick data source. |
| `QuickPrincipalArn` | *(required)* | Quick user or group ARN (identity Region) owning the data source. |
| `QuickServiceRoleName` | `aws-quicksight-service-role-v0` | Quick's service role to grant access to; empty skips the grant (console/Lake Formation fallback). |
| `QueryResultsExpirationDays` | `7` | Lifecycle expiry of Athena results. |
| `AnalyticsExpirationDays` | `365` | Lifecycle expiry of delivered data in the shared analytics bucket (the six data prefixes; collector state and the agent-name lookup are never expired by age). Noncurrent versions expire after 30 days bucket-wide. |
