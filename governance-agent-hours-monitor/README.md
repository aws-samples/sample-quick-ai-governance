# Amazon Quick — Agent Hours Usage Monitor

Track **Amazon Quick agent-hours usage** — total, per user, per feature, and license-covered vs.
chargeable — on a native **Amazon Quick dashboard**. The module delivers the `AGENT_HOURS_LOGS`
vended-log feed into the shared analytics bucket of the
[Analytics Foundation](../governance-analytics-foundation/README.md) and ships the dashboard that
reads it through Athena; the same S3 data serves any downstream tool (Athena, Grafana, Datadog,
chargeback joins).

Agent hours are **not** a CloudWatch metric, and the built-in Quick analytics dashboard only
aggregates them by subscription tier (no per-user / per-feature breakdown). The granular source is
the `AGENT_HOURS_LOGS` **vended-log feed**, which this module delivers to S3 so you can slice it
however you need. Everything is provisioned by two CloudFormation stacks (the module and its Quick
dashboard), wrapped by `apply` / `remove` helper scripts.

## Module layout

```
governance-agent-hours-monitor/
├── README.md
├── cloudformation/
│   ├── governance-agent-hours-monitor.yaml             # the module stack (source of truth)
│   └── governance-agent-hours-quick-dashboard.yaml     # the Amazon Quick dashboard stack
└── scripts/
    ├── apply-governance-agent-hours-monitor.sh          # module stack + Quick dashboard
    ├── remove-governance-agent-hours-monitor.sh         # tears both down (data in S3 preserved)
    ├── apply-governance-agent-hours-quick-dashboard.sh  # the dashboard stack alone
    └── remove-governance-agent-hours-quick-dashboard.sh
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
                                | one delivery (S3)
                                v
            +------------------------------------------+
            | SHARED ANALYTICS BUCKET (foundation)      |
            |   s3://<analytics bucket>/agent-hours/    |
            |   JSON, Hive-style partitions             |
            +-------------------+----------------------+
                                | Glue table agent_hours_logs
                                | Athena (Direct Query)
                                v
            +------------------------------------------+
            | AMAZON QUICK DASHBOARD (companion stack)  |
            |   Overview · Users · Features and         |
            |   automations · Events                    |
            +------------------------------------------+
                     also readable by Athena, Grafana, Datadog...
```

## What this stack creates

| Resource | Purpose |
|---|---|
| `AWS::Logs::DeliverySource` | One source pointing at the Quick account (`AGENT_HOURS_LOGS`). |
| `AWS::Logs::DeliveryDestination` (S3) + `AWS::Logs::Delivery` | Delivery of the feed under `agent-hours/` in the shared analytics bucket. |

The module creates no buckets: the shared bucket, its policy (which already lets
`delivery.logs.amazonaws.com` write `agent-hours/`), the Glue database, the Athena workgroup and the
Quick data source all come from the foundation stack. The dashboard stack adds the Glue table, the
Direct Query dataset and the dashboard.

## Prerequisites

- The [Analytics Foundation](../governance-analytics-foundation/README.md) stack deployed in the same
  account and Region (the apply script resolves its bucket; `--foundation-stack` if you renamed it).
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

That deploys the module stack and then the Quick dashboard stack. Module only — the feed in S3 for
your own tools, no Quick dashboard:

```bash
./apply-governance-agent-hours-monitor.sh --region us-east-1 --skip-dashboard
```

Keep the legacy `AWSLogs/<account-id>/...` object layout (an existing Athena table or pipeline reads
it):

```bash
./apply-governance-agent-hours-monitor.sh --region us-east-1 --hive-path false
```

Credentials: pass `--profile <name>` (or set the `AWS_PROFILE` environment variable) to choose a
named profile. The profile/credentials you deploy with determine the account that `AWS::AccountId`
resolves to, so use the profile for the account that hosts Quick.

The script wraps `aws cloudformation deploy` and prints the stack outputs (`S3DataLocation`, then the
dashboard's `DashboardUrl`). You can also deploy the templates directly — see the header of
`cloudformation/governance-agent-hours-monitor.yaml`.

**Object key layout.** With `S3HiveCompatiblePath=true` (default) date variables are rendered as
`key=value` partitions (`agent-hours/AWSLogs/aws-account-id=<account-id>/...`), which Athena partition
projection, Glue crawlers, Spark and DuckDB all read as partition columns without any crawler or
`MSCK REPAIR`; `false` keeps `agent-hours/AWSLogs/<account-id>/...`. The dashboard's Glue table reads
the whole `agent-hours/` prefix, so either layout works for it. The exact service-defined path segments
come from the log type's delivery configuration template
(`aws logs describe-configuration-templates --log-types AGENT_HOURS_LOGS`).

**Upgrading from a release with CloudWatch dashboards.** Re-applying removes the CloudWatch dashboard,
metric filter and CloudWatch Logs delivery; the live S3 delivery into the shared bucket is updated in
place. The old log group (`/aws/vendedlogs/quick/agent-hours`) is left behind, retained — delete it with
`aws logs delete-log-group` once you no longer need it. Stacks that still deliver into a module-owned
bucket must first migrate to the shared bucket (sync the history into `agent-hours/history/`, a
sub-prefix the live delivery never writes to).

## Amazon Quick dashboard

`cloudformation/governance-agent-hours-quick-dashboard.yaml` builds the dashboard: a Glue table
(`agent_hours_logs`) over the delivered objects, a Direct Query dataset on the foundation's Athena data
source, and a four-tab administrator dashboard. The module's apply script deploys it; on its own:

```bash
cd scripts
./apply-governance-agent-hours-quick-dashboard.sh --region us-east-1
./remove-governance-agent-hours-quick-dashboard.sh --region us-east-1     # data in S3 is untouched
```

The dashboard owner defaults to the Quick principal the foundation was deployed with (override with
`--quick-principal-arn`). The template exceeds CloudFormation's inline size limit, so the script uploads
it through the foundation's query-results bucket.

| Tab | What it shows |
|---|---|
| Overview | KPI row (agent hours, included, extra, chargeable share, active users, automations, features), daily hours by feature, daily Included vs Extra, top 10 consumers, how-to note |
| Users | Per-user table (hours, included, extra, features used, events, first / last activity), users × features pivot, users over entitlement |
| Features and automations | Per-feature table, Included vs Extra by feature, automation resources behind unattended consumption, automations over entitlement, and a **glossary of `reporting_service` codes** with documentation links (undocumented codes are flagged as such) |
| Events | Every metered event, newest first, exportable to CSV for chargeback joins |

Controls (date range, feature, coverage) live in the collapsible control bar and apply to every tab.
The dataset is Direct Query: each load reads S3 through Athena, so the dashboard is as fresh as the
delivery — no SPICE, no refresh schedule. Principals display as short labels (`default/<user>`,
`research/<id>`); the full ARNs remain on the Events tab. Note that `event_timestamp` arrives in epoch
seconds in delivered objects (the documentation example shows milliseconds); the dataset SQL handles both.
Top consumers attributes each event to its `user_arn`, falling back to `service_resource_arn` for
scheduled/deployed Automation runs (which carry no `user_arn`), so no chargeable hours are dropped.
Agent-hour entitlements reset monthly — set the date range to the current calendar month to view the
active period.

## Remove

```bash
cd scripts
./remove-governance-agent-hours-monitor.sh --region us-east-1                   # dashboard stack, then module stack
./remove-governance-agent-hours-monitor.sh --region us-east-1 --keep-dashboard  # module stack only
```

Nothing in S3 is deleted: the feed lives under `agent-hours/` in the foundation's analytics bucket.
Remove that data, or the foundation itself, through `governance-analytics-foundation/scripts`.

## Parameters

| Parameter | Default | Notes |
|---|---|---|
| `ResourcePrefix` | `quick-governance-agent-hours-monitor` | Names every resource; lowercase/DNS-safe. |
| `AnalyticsBucketName` | *(required)* | The foundation's shared analytics bucket (its `AnalyticsBucketName` output). The apply script resolves it from the foundation stack. |
| `S3HiveCompatiblePath` | `true` | Hive-style `key=value` partitions in S3 keys. `false` keeps the legacy `AWSLogs/<account-id>/...` layout. |

## Why CloudFormation (and the wrapper scripts)

`CreateDelivery` is **not idempotent** — re-running raw CLI calls would create duplicate deliveries
and double the per-GB delivery charge. CloudFormation handles create/update/delete idempotently,
orders the dependency chain (source before the delivery), and tears the whole chain down with one
stack delete. The `apply` / `remove` scripts are thin wrappers that give a one-command UX consistent
with the other `governance-*` modules in this repo — including deploying the Quick dashboard.

## Mapping to usage levels

- **Account-level** — fully supported: sum `usage_hours` across the feed (the dashboard's KPI row).
- **User-level** — supported via `user_arn`; scheduled/deployed automations have no user and are
  attributed to their `service_resource_arn` (see notes).
- **Group-level** — *not* available from this feed (no group field). It requires joining `user_arn`
  to group membership (Quick/QuickSight groups or IAM Identity Center) — e.g. in Athena against the
  S3 data, or as an extra Glue table joined in the Quick dataset. That join is intentionally left to
  the downstream layer.

## Notes and caveats

- **`user_arn` presence**: in practice it is present on interactive events (Research, desktop/Pages,
  interactive Flows/Automate) and absent on scheduled/deployed Automation runs. The dashboard handles
  this by attributing no-user events to `service_resource_arn`. If per-user attribution ever looks
  empty, confirm `user_arn` is delivered; if not, add `RecordFields` to the `AWS::Logs::Delivery`
  resource.
- **`reporting_service` values are dynamic** (e.g. `RESEARCH`, `PAGES_RUNTIME`, `AUTOMATION`). The
  feature visuals group on the field, so new values appear automatically; the glossary flags codes not
  yet described by AWS documentation.
- **Alerting**: this module publishes no metric. For an entitlement alert, use a Quick threshold alert
  on the Overview KPI, or an Athena-based check over `agent-hours/`.
- **Not retroactive**: delivery starts at deploy time; events before it cannot be recovered.

## Cost (US East / N. Virginia reference; varies by region)

- Vended-log **delivery**: tiered, $0.50/GB for the first 10 TB/month — negligible at agent-hours
  volumes (records are a few hundred bytes, so 1 GB is ~2M events).
- **Storage**: S3 Standard ~$0.023/GB-month, under the foundation's lifecycle expiry.
- **Athena**: per TB scanned, $5; a dashboard load scans kilobytes (the 10 MB per-query minimum applies).

## Sources

- Amazon Quick — Monitoring usage with CloudWatch Logs (`AGENT_HOURS_LOGS` schema and delivery setup)
- Amazon CloudWatch pricing — vended logs delivery
- CloudFormation — `AWS::Logs::DeliverySource` / `DeliveryDestination` / `Delivery`
- Amazon Athena — partition projection; Amazon Quick — CloudFormation `AWS::QuickSight::*` resources
