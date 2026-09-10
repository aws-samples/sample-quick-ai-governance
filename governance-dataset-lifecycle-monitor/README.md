# Amazon Quick — Dataset Lifecycle Monitor

Fleet-wide visibility into every Amazon Quick dataset — **last sync status with the typed failure
reason**, **sync duration (last + average, segmented by refresh type)**, and **last observed use
with an explicit confidence label** — on a CloudWatch dashboard, with SNS alerts for new refresh
failures and S3 JSONL snapshots for downstream analysis (Athena, Grafana, Quick itself).

Amazon Quick's **Manage Assets** page shows *which* datasets exist, but not whether they are
healthy, slow, or still used. Dataset-level refresh history is only visible inside each dataset —
which requires resource-level access an enterprise admin usually does not have. This module closes
that gap **without co-owning a single dataset**: everything is collected through IAM-authorized
read-only APIs and native CloudWatch metrics, so administrators get operational visibility with
zero data-plane access (no ability to query or edit anyone's data).

One collector Lambda runs on two schedules:

| Loop | Default cadence | Collects |
|---|---|---|
| **Fast** — sync health | `rate(1 hour)` | Last refresh status, typed failure reason, last/avg durations, rows ingested/dropped, failure recurrence |
| **Slow** — usage, lineage, capacity | daily `cron(15 3 * * ? *)` | Dashboard→dataset lineage, last-observed-use evidence, SPICE capacity, estimated cost signal |

A native CloudWatch alarm on `AWS/QuickSight IngestionErrorCount` provides minutes-level failure
detection *between* collector runs — the collector then attaches the reason on its next pass.

## Module layout

```
governance-dataset-lifecycle-monitor/
├── README.md
├── cloudformation/
│   └── governance-dataset-lifecycle-monitor.yaml      # SAM template (source of truth)
├── lambda/
│   └── collector.py                                   # fast + slow loop collector
└── scripts/
    ├── apply-governance-dataset-lifecycle-monitor.sh   # wraps: sam build + sam deploy
    └── remove-governance-dataset-lifecycle-monitor.sh  # wraps: aws cloudformation delete-stack
```

## Architecture

```
 EventBridge schedule            EventBridge schedule
 "fast" rate(1 hour)             "slow" cron(15 3 * * ? *)
        |                               |
        +------------- one collector ---+
                       Lambda (ReservedConcurrency=1)
                            |
        +-------------------+----------------------------+
        | fast loop                                      | slow loop
        v                                                v
  ListDataSets                                     ListDashboards
  ListIngestions per SPICE dataset                 DescribeDashboard (delta only)
   (paced <=4.5 TPS; 5 TPS/user quota)             ListTopics + DescribeTopic
        |                                          ListSpaces + ListSpaceResources
        |                                          ListAgents + DescribeAgent
        |                                          GetMetricData DashboardViewCount
        |                                          CloudTrail LookupEvents (optional)
        |                                          CHAT_LOGS Insights scan (optional)
        |                                          DescribeDataSet (capacity, best effort)
        |                                                |
        +---------------------+--------------------------+
                              v
        +---------------------------------------------+
        |  outputs, every run                         |
        |   1 JSON log event per dataset  ->  CloudWatch Logs data log group
        |   tombstone events for deleted datasets     |
        |   fleet KPI metrics (15 names)  ->  QuickGovernance/DatasetLifecycle
        |   JSONL snapshot + state        ->  S3 bucket (retained on delete)
        |   alert on NEW failure          ->  SNS (optional, deduplicated)
        +---------------------------------------------+
                              |
                              v
                 CloudWatch DASHBOARD
                  - KPI tiles (total / SPICE / failed / unreferenced /
                    no-observed-use / SPICE GB)
                  - Currently FAILED datasets + reason
                  - Slowest datasets (avg vs last)
                  - Unreferenced SPICE datasets (cost vs evidence)
                  - "How to read usage evidence" legend
                  - Least recently used (WITH evidence, stalest first)
                  - No usage evidence (telemetry blind spot, verify first)
                  - Native ingestion latency/error trends
                  - Largest datasets + estimated cost signal
```

Storage is deliberately database-free and cost-optimized:

- **CloudWatch Logs is the system of record** — one JSON event per dataset per run; the dashboard
  tables are Logs Insights queries (`stats latest(...) by datasetId`).
- **No per-dataset custom metrics** (at 1,000 datasets that would cost hundreds of USD/month and
  cannot carry text like failure reasons). Per-dataset trends come from the **free native**
  `AWS/QuickSight` metrics; only a handful of account-level KPIs are published.
- **S3** holds tiny state objects (alert dedupe, lineage cache, usage evidence) and per-run JSONL
  snapshots for anything downstream.

## What this stack creates

| Resource | Purpose |
|---|---|
| `AWS::Serverless::Function` + role | The collector (Python 3.14, arm64), read-only Quick access, `ReservedConcurrentExecutions: 1` to serialize loops under API quotas. |
| 2 × EventBridge schedule | Fast loop (`{"mode":"fast"}`) and slow loop (`{"mode":"slow"}`). |
| `AWS::Logs::LogGroup` (data) | One JSON event per dataset per run; queried by the dashboard. |
| `AWS::Logs::LogGroup` (Lambda) | Collector runtime logs, same retention. |
| `AWS::S3::Bucket` | State + JSONL snapshots. **Retained on stack delete.** |
| `AWS::CloudWatch::Dashboard` | The fleet dashboard (see tour below). |
| Alarm on `IngestionErrorCount` | Native account-aggregate metric — minutes-level MTTD for any SPICE refresh failure. |
| Alarm on Lambda `Errors` | Collector health — scan data may be stale/partial when firing. |
| `AWS::SNS::Topic` + email subscription *(optional)* | New-failure alerts (with reason) and alarm notifications. Created only when `AlertEmail` is set; **confirm the subscription email**. |

## Data sources and collection intervals

| Signal | Source | Interval | Notes |
|---|---|---|---|
| Refresh status + failure reason | `ListIngestions` (`IngestionStatus`, `ErrorInfo.Type/Message`, `RequestType/Source`, `RowInfo`) | Fast loop | Paced ≤4.5 TPS under the 5 TPS/user, 25 TPS/account quota |
| Failure detection between runs | Native `IngestionErrorCount` alarm | Continuous | Push-based; no polling |
| Sync duration last/avg | `ListIngestions` (`IngestionTimeInSeconds`) + native `IngestionLatency` | Fast loop / native | Averages segmented by full vs incremental refresh |
| Dashboard→dataset lineage | `ListDashboards` + `DescribeDashboard` (`DataSetArns`) | Slow loop, delta-only | Re-described only when `LastUpdatedTime` changes |
| Topic→dataset lineage | `ListTopicsV2` (fallback `ListTopics`) + `DescribeTopicV2` (fallback `DescribeTopic`), plus topics referenced by spaces | Slow loop | New-experience topics are invisible to the legacy APIs; failures downgrade to warnings |
| Space→dataset and Space→Chat Agent lineage | `ListSpaces` + `ListSpaceResources` (`DATA_SET` / `TOPIC` members) + `ListAgents` + `DescribeAgent` (`Spaces`) | Slow loop | Agents reach datasets only through spaces; feature-detected — requires a botocore with the Space/Agent APIs, otherwise last known state is reused with a warning |
| Agent citations, chat selections, and conversations | Logs Insights on the `CHAT_LOGS` log group (Pillar 2 module) | Slow loop, incremental | `cited_resource` + `user_selected_resources` + last conversation per agent (joined with lineage into `AGENT_CONVERSATION_SCOPE`); ≤10,000 messages per scan; skipped if the log group is absent |
| Last dashboard view | `GetMetricData` on `DashboardViewCount` per `DashboardId` | Slow loop | 90-day pass, then 15-month pass for silent dashboards |
| Last queried / viewed (higher confidence) | CloudTrail `LookupEvents`: `QueryDatabase`, `GetDashboard`, `GetDashboardEmbedUrl` | Slow loop, incremental | Always-on 90-day Event history; no trail required; ≤2 TPS paced. `QueryDatabase` counts as usage for direct-query datasets only — for SPICE it fires during refresh |
| SPICE capacity | `DescribeDataSet` (`ConsumedSpiceCapacityInBytes`) | Slow loop | Best-effort; file-upload datasets are not API-describable |

## Semantics the module guarantees

**Refresh semantics.** A `FAILED` refresh never overwrites `LastSuccessfulRefreshAt` — Quick keeps
serving the previous successful SPICE snapshot, so both timestamps are reported. Direct-query
datasets never get SPICE refresh semantics (`lastStatus: NOT_APPLICABLE`).

**Last-used semantics.** Quick exposes no authoritative "dataset last used" attribute, so
`lastObservedUseAt` is **evidence, not proof**, and always carries its source and confidence:

| Evidence | Confidence | Source label |
|---|---|---|
| A Chat Agent answer cited the dataset (`CHAT_LOGS`) | High | `AGENT_CITED_DATASET` |
| CloudTrail `QueryDatabase` naming a **direct-query** dataset | High | `CLOUDTRAIL_QUERY` |
| CloudTrail `QueryDatabase` naming a **SPICE** dataset (interactive query, session prewarm, or refresh — indistinguishable in the event) | Medium | `CLOUDTRAIL_QUERY` |
| An agent cited a topic containing the dataset, used a space containing it, or a user selected the dataset in chat | Medium | `AGENT_CITED_TOPIC` / `AGENT_USED_SPACE` / `AGENT_SELECTED_RESOURCE` |
| A conversation ran on an agent that can reach the dataset — CHAT_LOGS × agent→space→dataset lineage; scope-level, not query proof | Medium (agent scope ≤ `AgentScopeMediumMax` datasets) · Low (broader scope) | `AGENT_CONVERSATION_SCOPE` |
| A dashboard that uses the dataset was viewed (metric, CloudTrail, or agent citation) | Medium | `DASHBOARD_VIEW_METRIC` / `DASHBOARD_VIEW_CLOUDTRAIL` / `AGENT_CITED_DASHBOARD` |
| Dataset is referenced by dashboards, topics, or spaces (and through spaces, by Chat Agents), no observed activity | Low | `DEPENDENCY_ONLY` |
| No references, no events | No evidence | — (`usageConfidence: NO_EVIDENCE`) |

Nuances verified empirically: CloudTrail `QueryDatabase` fires for direct-query datasets when
they are queried (High), and for SPICE datasets on refreshes *and* service-initiated interactive
queries (session prewarm) — indistinguishable in the event, hence Medium. Topics are listed with
the **V2 topic APIs** first: topics created in the new Quick experience are invisible to the
legacy `ListTopics` / `DescribeTopic` ("use new versions of Topic APIs"), and topics referenced
by spaces are described even when no listing returns them. Agent citations of documents and
knowledge bases are ignored (no dataset lineage). One verified platform gap: an agent answer
**grounded through a topic can cite nothing** — empty `cited_resource`,
`message_scope: no_resources`, no CloudTrail event, no metric — leaving no dataset-level trace in
any exportable telemetry. The compensating control is `AGENT_CONVERSATION_SCOPE`: every
conversation on a non-SYSTEM agent (including PREVIEW / draft versions, resolved with
`DescribeAgent` on demand and cached) stamps the datasets that agent can reach through its
spaces — deliberately labeled scope-level evidence ("an agent able to reach this dataset was
actively used"), Medium only when the agent's scope is tight (≤ `AgentScopeMediumMax`), and
never presented as proof that the specific dataset was queried. The default SYSTEM agent is
excluded (its scope is every resource the user can access). Agent evidence requires the
[Chat and Feedback Monitor](../governance-chat-feedback-monitor/README.md)
log group (see `ChatLogGroupName`).

`No evidence` is **not** `unused` — the dashboard says so on its own legend widget. The
`NO_OBSERVED_USE` finding only fires for datasets that *are* referenced by dashboards, topics,
or spaces. Every dataset event carries `dependentDashboardCount`, `dependentTopicCount`,
`dependentSpaceCount`, and `dependentAgentCount` (Chat Agents reachable through spaces that
contain the dataset directly or via a topic) — so "which agents can reach this dataset" is
answerable per dataset, deterministically.

**Deleted datasets.** When a dataset disappears from `ListDataSets`, the collector emits
tombstone events (`assetState: DELETED`) on every run for the retention window, so
`latest(...) by datasetId` resolves to DELETED and every dashboard table drops the row on the
next scan — a deleted dataset never lingers with stale state, whatever time range is selected.

**Cost labeling.** SPICE is billed as pooled account/Region capacity, so per-dataset cost is a
managerial allocation. The module publishes `spiceGB × SpiceRatePerGBMonth` labeled
`ESTIMATED_PURCHASED` — never an AWS invoice amount. CUR-based actual/net attribution is a
non-goal (see below).

**Findings** (rule-based flags on each dataset event, filterable in Logs Insights via the
`findings` array or the flat `findingsCsv` string):
`REFRESH_FAILED`, `REFRESH_NEVER_COMPLETED`, `REFRESH_FAILURE_RECURRING` (≥3 failures in the
lookback window), `SPICE_DATASET_NOT_REFERENCED` (in no dashboard, no topic, *and* no space),
`NO_OBSERVED_USE`, `SCAN_INCOMPLETE`.

## Dashboard tour

- **KPI tiles** — total and direct-query datasets, SPICE datasets, datasets with FAILED last
  refresh, unreferenced SPICE datasets, referenced datasets with no observed use, total consumed
  SPICE GB.
- **Currently FAILED datasets** — latest state per dataset with `errorType`/`errorMessage`
  (typed reasons like `DATA_SOURCE_CONNECTION_FAILED`, `QUERY_TIMEOUT`, `PERMISSION_DENIED`; see
  the [SPICE ingestion error codes](https://docs.aws.amazon.com/quick/latest/userguide/errors-spice-ingestion.html)).
- **Slowest SPICE datasets** — average vs last duration, full vs incremental — spot datasets whose
  sync time is drifting up before they breach refresh windows.
- **Unreferenced SPICE datasets** — in no dashboard, topic, or space, ranked by capacity: the
  cost-versus-evidence list a Head of Data reviews first.
- **How to read usage evidence** — an always-visible legend: the confidence ladder, the evidence
  windows, and the blind spots. The single most important widget for interpreting the two tables
  below it.
- **Least recently used (WITH evidence)** — only datasets with an observed-use timestamp, stalest
  first, with confidence, source, and dashboard/topic/space/agent reference counts.
- **No usage evidence** — referenced or not, nothing observed: the telemetry blind spot list,
  explicitly labeled "verify before acting", with the same reference counts (a dataset with
  agents ≥1 is reachable by Chat Agents even if nothing was observed yet).
- **Native trends** — account-wide `IngestionLatency` (avg/max) and ingestions vs failures,
  emitted by Quick itself; empty when no refresh ran in the selected time range.
- **Largest SPICE datasets** — GB, estimated monthly cost signal, cross-referenced with usage.
- **Recent refresh-failure events** — raw feed of failures as they were observed.

## Alerts

- **New refresh failure** (SNS email): fires once per distinct failed ingestion — deduplicated via
  S3 state, and re-armed when the dataset recovers. Carries dataset name, failure time, refresh
  type, typed error, message, and last-success timestamp.
- **`IngestionErrorCount ≥ 1` in 5 min** (native alarm): fastest possible detection signal.
- **Collector errors** (alarm): the scan itself is failing; data may be stale.

## Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `ResourcePrefix` | `quick-governance-dataset-lifecycle` | Names every resource (lowercase; also in the bucket name). |
| `FastLoopSchedule` | `rate(1 hour)` | Sync-health cadence. |
| `SlowLoopSchedule` | `cron(15 3 * * ? *)` | Usage/lineage/capacity cadence. |
| `AlertEmail` | *(empty)* | Set to enable SNS alerts + alarm notifications. |
| `EnableCloudTrailEvidence` | `true` | CloudTrail Event history evidence for last-used. |
| `ChatLogGroupName` | `/aws/vendedlogs/quick/chat-feedback` | `CHAT_LOGS` log group (Pillar 2 module) scanned for agent-citation evidence. Skipped with a warning if absent; empty disables. |
| `AgentScopeMediumMax` | `10` | `AGENT_CONVERSATION_SCOPE` is Medium when the conversing agent can reach at most this many datasets, Low above it. |
| `SpiceRatePerGBMonth` | `0.38` | USD rate for the estimated cost signal (example us-east-1 rate — adjust per Region/agreement). |
| `UnusedThresholdDays` | `30` | Days without evidence before `NO_OBSERVED_USE`. |
| `IngestionLookbackRuns` | `50` | Newest ingestions per dataset for averages. |
| `CloudTrailLookbackDays` | `90` | First-scan Event history lookback (max 90). |
| `LogRetentionDays` | `90` | Data + Lambda log group retention. |
| `SnapshotExpirationDays` | `180` | S3 snapshot lifecycle expiry. |
| `FunctionTimeoutSeconds` / `FunctionMemoryMb` | `900` / `512` | Collector sizing. |

## Deploy

Prerequisites: AWS SAM CLI, AWS CLI v2 authenticated against the account/Region that holds the
Quick subscription. The apply script runs `sam build` first (resolves `lambda/requirements.txt`,
which pins a boto3 recent enough for the Space/Agent/Topic-V2 lineage APIs) — that step needs a
local Python matching the function runtime (3.14) or Docker (`sam build --use-container`). The
deploying principal needs CloudFormation/IAM/Lambda/S3/SNS/CloudWatch permissions; the collector
itself is read-only towards Quick.

```bash
cd governance-dataset-lifecycle-monitor/scripts

# minimal
./apply-governance-dataset-lifecycle-monitor.sh --region us-east-1

# with alerts and a 60-day unused threshold
./apply-governance-dataset-lifecycle-monitor.sh \
    --region us-east-1 --profile my-profile \
    --alert-email bi-admins@example.com --unused-threshold-days 60
```

Then trigger a first scan immediately (also printed as stack outputs):

```bash
aws lambda invoke --function-name quick-governance-dataset-lifecycle-fn \
  --payload '{"mode": "fast"}' --cli-binary-format raw-in-base64-out /dev/stdout
aws lambda invoke --function-name quick-governance-dataset-lifecycle-fn \
  --payload '{"mode": "slow"}' --cli-binary-format raw-in-base64-out /dev/stdout
```

Open the dashboard via the `DashboardUrl` stack output. If you set `AlertEmail`, confirm the SNS
subscription from your inbox.

## IAM and security

- Collector permissions are read-only towards Quick (`ListDataSets`, `ListDashboards`,
  `ListTopics`, `ListTopicsV2`, `ListSpaces`, `ListSpaceResources`, `ListAgents`,
  `ListIngestions`, `DescribeDataSet`, `DescribeDashboard`, `DescribeTopic`, `DescribeTopicV2`,
  `DescribeAgent`) plus
  `cloudwatch:GetMetricData`, `cloudtrail:LookupEvents`, Logs Insights read on the `CHAT_LOGS`
  log group when `ChatLogGroupName` is set (`logs:StartQuery` scoped to that group), scoped
  writes to its own log group/bucket/metrics namespace, and optional `sns:Publish`. **No Quick
  resource permissions (ownership/sharing) are used or granted** — this is the least-privilege
  alternative to adding yourself as co-owner of every dataset.
- Failure messages can reveal infrastructure details (hosts, schemas); they are truncated to 400
  chars and stay inside your account (log group, bucket, SNS topic). Grant dashboard/log access
  accordingly, and keep dataset names/IDs out of anything public.
- The S3 bucket blocks public access, is SSE-encrypted, and is retained on stack delete.

## Cost

Roughly **US$2–3/month** at ~1,000 SPICE datasets / 500 dashboards on default cadences:

| Item | ~Cost/month |
|---|---|
| Log ingestion (1k events/hour ≈ 24 MB/day) | ~$0.40 |
| Custom KPI metrics (≤15 total) | ~$1.80–4.50 |
| 3 alarms | $0.30 |
| Lambda (≤5 min/hour, arm64 512 MB) | ~$0.15 |
| `GetMetricData`, CHAT_LOGS Insights scan, S3, SNS | pennies |

QuickSight/CloudTrail API calls used by the collector are free. Slower cadences reduce this
further (`--fast-schedule 'rate(6 hours)'`).

## Limitations

- **Scale**: at ≤4.5 TPS the fast loop fits Lambda's 15-minute limit up to roughly **4,000 SPICE
  datasets**. Beyond that, chunk the scan (Step Functions map / per-batch invocations) — not built
  here.
- **Ingestion ordering**: the ingestion window fetches up to 200 newest entries and re-sorts
  defensively; a dataset with >200 ingestions since the last run (extreme incremental cadence)
  could under-count window failures.
- **Space/agent lineage needs recent APIs**: `ListSpaces` / `ListSpaceResources` / `ListAgents` /
  `DescribeAgent` / `ListTopicsV2` / `DescribeTopicV2` are recent additions to the Quick API. The
  apply script bundles a pinned boto3 that has them (`lambda/requirements.txt`, resolved by
  `sam build`). The collector also feature-detects them, so a package built without the bundle
  (for example a direct `sam deploy` of the raw template) logs a warning, reuses the last stored
  lineage, and everything else keeps working.
- **Usage evidence gaps**: agent-side evidence covers what `CHAT_LOGS` records — citations,
  explicit selections, and per-agent conversation timestamps (capped at 10,000 chat messages per
  scan) — and requires the Pillar 2 log group. A conversation that used a dataset without citing
  it is covered only at scope level (`AGENT_CONVERSATION_SCOPE`, never query proof); ad-hoc
  analyses and consumption outside these feeds remain invisible. `NO_OBSERVED_USE` is
  evidence-bounded, never proof.
  CloudTrail Event history covers at most 90 days; dashboard-view metrics cover ~15 months.
- **File-upload datasets**: not describable through the API — no capacity/cost signal for them.
- **Loops serialize**: `ReservedConcurrentExecutions: 1` means a colliding fast/slow run is
  throttled and retried by EventBridge; runs can shift by minutes.

## Non-goals

- **CUR/billing attribution** (`ACTUAL_NET` / `ACTUAL_GROSS` / `OPPORTUNITY_COST`): requires Data
  Exports + Glue/Athena prerequisites — breaks the self-contained-module rule. The estimated
  signal covers the practical ranking need; never treat it as an invoice decomposition.
- **Auto-remediation**: the module never deletes datasets, never disables refresh schedules, never
  changes permissions. Audit-only by design.
- **Ownership remediation**: see `governance-orphaned-assets-monitor` (separate module).

## Teardown

```bash
cd governance-dataset-lifecycle-monitor/scripts
./remove-governance-dataset-lifecycle-monitor.sh --region us-east-1
```

The S3 bucket is retained (scan history). Remove it manually when no longer needed:

```bash
aws s3 rb "s3://quick-governance-dataset-lifecycle-<account-id>" --force --region us-east-1
```
