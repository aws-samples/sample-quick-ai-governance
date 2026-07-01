# Amazon Quick — Agent Hours Usage Monitor

Track **Amazon Quick agent-hours usage** — total, per user, per feature, and license-covered vs.
chargeable — on a CloudWatch dashboard, and optionally land the same feed in **Amazon S3** for
downstream analysis (Athena, Grafana, Datadog).

Agent hours are **not** a CloudWatch metric, and the built-in Quick analytics dashboard only
aggregates them by subscription tier (no per-user / per-feature breakdown). The granular source is
the `AGENT_HOURS_LOGS` **vended-log feed**, which this module delivers to CloudWatch Logs (and S3)
so you can slice it however you need. Everything is provisioned by one CloudFormation stack, wrapped
by `apply` / `remove` helper scripts.

## Module layout

```
governance-agent-hours-monitor/
├── README.md
├── cloudformation/
│   └── governance-agent-hours-monitor.yaml       # the stack (source of truth)
└── scripts/
    ├── apply-governance-agent-hours-monitor.sh    # wraps: aws cloudformation deploy
    └── remove-governance-agent-hours-monitor.sh   # wraps: aws cloudformation delete-stack
```

## Architecture

```
                      +------------------------------------------+
                      |               Amazon Quick               |
                      |        Research / Flows / Automate        |
                      |           emits AGENT_HOURS_LOGS          |
                      +-------------------+----------------------+
                                          | vended logs
                                          v
                      +------------------------------------------+
                      |      CloudWatch Logs DELIVERY SOURCE      |
                      |      name:    <prefix>-source             |
                      |      logType: AGENT_HOURS_LOGS            |
                      +-------------------+----------------------+
                                          |
                                          |  one source -> two deliveries
           CreateDelivery #1              |              CreateDelivery #2
        +---------------------------------+---------------------------------+
        |                                                                   |
        v                                                                   v
+-------------------------------+                   +-------------------------------+
| DESTINATION (CWL)             |                   | DESTINATION (S3)  [optional]  |
| log group:                    |                   | bucket:                       |
|   /aws/vendedlogs/            |                   |   <prefix>-<account-id>       |
|   quick/agent-hours           |                   |                               |
+---------------+---------------+                   +---------------+---------------+
                |                                                   |
                v                                                   v
+-------------------------------+                   +-------------------------------+
| CloudWatch DASHBOARD          |                   | DOWNSTREAM (not built here)   |
| "<prefix>-dashboard"          |                   | you own this layer:           |
|  - Total agent hours (KPI)    |                   |   - Athena (query S3)         |
|  - Top consumers              |                   |   - Grafana / Datadog         |
|  - Spend by feature (trend)   |                   |                               |
|  - Included vs Extra (trend)  |                   |                               |
|  - Extra by feature/user/     |                   |                               |
|    automation                 |                   |                               |
+-------------------------------+                   +-------------------------------+
```

A `AWS::Logs::MetricFilter` also turns each event's `usage_hours` into the account metric
`QuickGovernance/AgentHours`, which backs the single-number KPI tile.

## What this stack creates

| Resource | Purpose |
|---|---|
| `AWS::Logs::LogGroup` | Receives `AGENT_HOURS_LOGS`; queried by the dashboard. |
| `AWS::Logs::MetricFilter` | Publishes account-total agent hours as metric `QuickGovernance/AgentHours` for the KPI tile (and optional alarms). |
| `AWS::Logs::DeliverySource` | One source pointing at the Quick account, fanning out below. |
| `AWS::Logs::DeliveryDestination` (CWL) + `AWS::Logs::Delivery` | Delivery #1 to the log group. |
| `AWS::CloudWatch::Dashboard` | The CIO dashboard (Logs Insights widgets + the metric KPI tile). |
| `AWS::S3::Bucket` + `AWS::S3::BucketPolicy` *(optional)* | Private, encrypted bucket for the feed. |
| `AWS::Logs::DeliveryDestination` (S3) + `AWS::Logs::Delivery` *(optional)* | Delivery #2 to S3. |

## Dashboard views (CIO)

A single dashboard time picker drives every widget: set the analysis timeframe with the picker and
every widget updates. The two trend charts use daily buckets (`bin(1d)`); all other widgets report
totals for the selected range. Defaults to a 3-month window.

| View | Widget |
|---|---|
| Total usage | KPI tile (metric), selected range |
| Who uses the most | Ranked table, selected range |
| Spend by feature | Trend chart (daily buckets) by service |
| License: Included vs Extra | Stacked trend chart (daily buckets) |
| Extra (chargeable) by feature | Table, selected range |
| Extra by user (people over entitlement) | Table, selected range |
| Extra by automation (scheduled/deployed) | Table, selected range |

The KPI tile reads the `AgentHours` metric, which accrues from the metric filter's creation onward
(no backfill); the log-based widgets reflect full history within the log group's retention. Top
consumers attributes each event to its `user_arn`, falling back to `service_resource_arn` for
scheduled/deployed Automation runs (which carry no `user_arn`), so no chargeable hours are dropped.

## Prerequisites

- Amazon Quick AI features enabled (Enterprise or Professional subscription).
- The deploying principal must hold `quicksight:AllowVendedLogDeliveryForResource` on the Quick account.
- Same-account: the Quick subscription must be in the same AWS account where you deploy. The template
  targets it automatically via the `AWS::AccountId` pseudo-parameter (resolved from your deploy
  credentials), so there is no account ID to pass. Cross-account delivery needs an extra
  delivery-destination policy and is out of scope.

## Deploy

```bash
cd scripts
./apply-governance-agent-hours-monitor.sh --region us-east-1
```

Dashboard-only (no S3):

```bash
./apply-governance-agent-hours-monitor.sh --region us-east-1 --enable-s3 false
```

Credentials: pass `--profile <name>` (or set the `AWS_PROFILE` environment variable) to choose a
named profile. The profile/credentials you deploy with determine the account that `AWS::AccountId`
resolves to, so use the profile for the account that hosts Quick.

The script wraps `aws cloudformation deploy` and prints the stack outputs (including a direct
`DashboardUrl`). You can also deploy the template directly — see the header of
`cloudformation/governance-agent-hours-monitor.yaml`.

## Remove

```bash
cd scripts
./remove-governance-agent-hours-monitor.sh --region us-east-1                  # keeps the S3 bucket
./remove-governance-agent-hours-monitor.sh --region us-east-1 --delete-bucket  # also removes log history
```

The S3 bucket has `DeletionPolicy: Retain`, so a stack delete preserves your log history unless you
pass `--delete-bucket`.

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| `ResourcePrefix` | `quick-governance-agent-hours-monitor` | Names every resource; lowercase/DNS-safe (used in the bucket name). |
| `LogGroupName` | `/aws/vendedlogs/quick/agent-hours` | Log group for the feed. |
| `LogRetentionDays` | `90` | CloudWatch Logs retention. |
| `EnableS3Delivery` | `true` | Set `false` for the dashboard path only. |
| `S3ExpirationDays` | `365` | Lifecycle expiration for objects in the S3 bucket. |

## Why CloudFormation (and the wrapper scripts)

`CreateDelivery` is **not idempotent** — re-running raw CLI calls would create duplicate deliveries
and double the per-GB delivery charge. CloudFormation handles create/update/delete idempotently,
orders the dependency chain (bucket policy before the S3 delivery; source before either delivery),
and tears the whole chain down with one stack delete. The `apply` / `remove` scripts are thin
wrappers that give a one-command UX consistent with the other `governance-*` modules in this repo.

## Mapping to usage levels

- **Account-level** — fully supported: sum `usage_hours` across the feed (and the `AgentHours` metric).
- **User-level** — supported via `user_arn`; scheduled/deployed automations have no user and are
  attributed to their `service_resource_arn` (see notes).
- **Group-level** — *not* available from this feed (no group field). It requires joining `user_arn`
  to group membership (Quick/QuickSight groups or IAM Identity Center) outside CloudWatch — e.g. in
  Athena against the S3 copy. That join is intentionally left to the downstream layer.

## Notes and caveats

- **`user_arn` presence**: in practice it is present on interactive events (Research, desktop/Pages,
  interactive Flows/Automate) and absent on scheduled/deployed Automation runs. The dashboard handles
  this by attributing no-user events to `service_resource_arn`. If per-user attribution ever looks
  empty, confirm `user_arn` is delivered; if not, add `RecordFields` to the `AWS::Logs::Delivery`
  resource.
- **`reporting_service` values are dynamic** (e.g. `RESEARCH`, `PAGES_RUNTIME`, `AUTOMATION`). The
  feature widgets group on the field, so new values appear automatically.
- **Billing period**: agent-hour entitlements reset monthly — set the time picker to the current
  calendar month to view the active period.
- **Alarms**: the account-total `AgentHours` metric can back a CloudWatch alarm (e.g. notify when the
  monthly total nears your pooled entitlement). Per-user / per-automation alarms aren't practical
  (high cardinality) — read those from the tables.
- **Metric backfill**: the metric filter only processes events ingested after its creation, so the
  KPI tile counts from deploy time forward; the log-based widgets always reflect full log history.

## Cost (US East / N. Virginia reference; varies by region)

- Vended-log **delivery**: tiered, $0.50/GB for the first 10 TB/month (per destination). Running both
  CWL and S3 deliveries pays this twice — negligible at agent-hours volumes (records are a few hundred
  bytes, so 1 GB is ~2M events).
- **Storage**: CloudWatch Logs $0.03/GB-month; S3 Standard ~$0.023/GB-month.
- **Logs Insights** queries on the dashboard bill per GB scanned (low for this feed).
- **Custom metric**: one `AgentHours` metric (~$0.30/month).

## Sources

- Amazon Quick — Monitoring usage with CloudWatch Logs (`AGENT_HOURS_LOGS` schema and delivery setup)
- Amazon CloudWatch pricing — vended logs delivery and storage
- CloudWatch Logs Insights — `stats` / `bin` and datetime functions
- CloudFormation — `AWS::Logs::DeliverySource` / `DeliveryDestination` / `Delivery`, `AWS::Logs::MetricFilter`
