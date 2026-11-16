# AWS Quick — Dataset Lifecycle Monitor

> [!IMPORTANT]
> **Sample code — review and adapt before production use.** Not an AWS service and not supported by AWS; see the [repository disclaimer](../README.md#disclaimer).

Fleet-wide visibility into every AWS Quick dataset — **last sync status with the typed failure
reason**, **sync duration (last + average, segmented by refresh type)**, and **last observed use
with an explicit confidence label** — on a native AWS Quick dashboard, with SNS alerts and alarms
for new refresh failures, over S3 JSONL snapshots that any downstream tool can also read (Athena,
Grafana, or your own tools).

AWS Quick's **Manage Assets** page shows *which* datasets exist, but not whether they are
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
        |   JSONL snapshot (1 record per dataset,     |
        |     tombstones for deleted datasets) ->  shared analytics bucket, dataset-lifecycle/fast|slow/dt=.../
        |   state (alert dedupe, lineage, usage) ->  shared analytics bucket, dataset-lifecycle/state/
        |   fleet KPI metrics (15 names)  ->  CloudWatch metrics QuickGovernance/DatasetLifecycle (alarms)
        |   alert on NEW failure          ->  SNS (optional, deduplicated)
        +---------------------------------------------+
                              |  Glue tables fast, slow, dashboards, schedules · Athena (Direct Query)
                              v
                 AWS QUICK DASHBOARD (companion stack)
                  - Overview · Refresh health · Refresh plan · Usage
                    · Dashboards · Capacity and cost · History
```

Storage is deliberately database-free and cost-optimized:

- **S3 snapshots are the system of record** — one JSONL record per dataset per run in the shared
  analytics bucket; the dashboard's datasets keep the latest scan per dataset with a window function
  (`row_number() over (partition by datasetId order by ts desc)`).
- **No per-dataset custom metrics** (at 1,000 datasets that would cost hundreds of USD/month and
  cannot carry text like failure reasons). Per-dataset trends come from the **free native**
  `AWS/QuickSight` metrics; only a handful of account-level KPIs are published.
- **S3 also holds the tiny state objects** (alert dedupe, lineage cache, usage evidence) under
  `dataset-lifecycle/state/`, a prefix no dashboard reads. The module creates no buckets of its own.

## What this stack creates

| Resource | Purpose |
|---|---|
| `AWS::Serverless::Function` + role | The collector (Python 3.14, arm64), read-only Quick access, `ReservedConcurrentExecutions: 1` to serialize loops under API quotas. |
| 2 × EventBridge schedule | Fast loop (`{"mode":"fast"}`) and slow loop (`{"mode":"slow"}`). |
| `AWS::Logs::LogGroup` (Lambda) | Collector runtime logs, same retention. |
| KPI metrics (`QuickGovernance/DatasetLifecycle`) | Up to 15 account-level metrics published by the collector; they drive the alarms. |
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
| Last dashboard view | `GetMetricData` on `DashboardViewCount` per `DashboardId` | Slow loop | One 455-day pass: last view, views in 30, 60 and 90 days, and the same 90 days a year earlier |
| Refresh schedules | `ListRefreshSchedules` per SPICE dataset | Slow loop | Paced like `ListIngestions`. One row per schedule and one row per planned run in the next 7 days |
| Refresh count, 30 days | `GetMetricData` on `IngestionInvocationCount` per `DatasetId` | Slow loop | Scheduled and manual together. The metric has no source dimension |
| Last queried / viewed (higher confidence) | CloudTrail `LookupEvents`: `QueryDatabase`, `GetDashboard`, `GetDashboardEmbedUrl` | Slow loop, incremental | Always-on 90-day Event history; no trail required; ≤2 TPS paced. A SPICE `QueryDatabase` event inside that dataset's refresh window does not count as use |
| SPICE capacity | `DescribeDataSet` (`ConsumedSpiceCapacityInBytes`) | Slow loop | Best-effort; file-upload datasets are not API-describable |

## Semantics the module implements

**Refresh semantics.** A `FAILED` refresh never overwrites `LastSuccessfulRefreshAt` — Quick keeps
serving the previous successful SPICE snapshot, so both timestamps are reported. Direct-query
datasets never get SPICE refresh semantics (`lastStatus: NOT_APPLICABLE`).

**Last-used semantics.** Quick exposes no authoritative "dataset last used" attribute, so
`lastObservedUseAt` is **evidence, not proof**, and always carries its source and confidence:

| Evidence | Confidence | Source label |
|---|---|---|
| A Chat Agent answer cited the dataset (`CHAT_LOGS`) | High | `AGENT_CITED_DATASET` |
| CloudTrail `QueryDatabase` naming a **direct-query** dataset | High | `CLOUDTRAIL_QUERY` |
| CloudTrail `QueryDatabase` naming a **SPICE** dataset outside that dataset's own refresh | Medium | `CLOUDTRAIL_QUERY` |
| An agent cited a topic containing the dataset, used a space containing it, or a user selected the dataset in chat | Medium | `AGENT_CITED_TOPIC` / `AGENT_USED_SPACE` / `AGENT_SELECTED_RESOURCE` |
| A conversation ran on an agent that can reach the dataset — CHAT_LOGS × agent→space→dataset lineage; scope-level, not query proof | Medium (agent scope ≤ `AgentScopeMediumMax` datasets) · Low (broader scope) | `AGENT_CONVERSATION_SCOPE` |
| A dashboard that uses the dataset was viewed (metric, CloudTrail, or agent citation) | Medium | `DASHBOARD_VIEW_METRIC` / `DASHBOARD_VIEW_CLOUDTRAIL` / `AGENT_CITED_DASHBOARD` |
| Dataset is referenced by dashboards, topics, or spaces (and through spaces, by Chat Agents), no observed activity | Low | `DEPENDENCY_ONLY` |
| No references, no events | No evidence | — (`usageConfidence: NO_EVIDENCE`) |

Nuances verified empirically: CloudTrail `QueryDatabase` fires for direct-query datasets when
they are queried (High). For SPICE it also fires during a refresh. An event whose time falls
inside that dataset's ingestion, from 120 seconds before `CreatedTime` until the duration plus
120 seconds (or until the scan, when the duration is missing), does not count as use. Outside
that window the event stays Medium, because a person and a refresh can still look alike.
The event's `resourceId` names the dashboard or analysis behind the query when it carries one.
Topics are listed with
the **V2 topic APIs** first: topics created in the new Quick experience are invisible to the
legacy `ListTopics` / `DescribeTopic` ("use new versions of Topic APIs"), and topics referenced
by spaces are described even when no listing returns them. Agent citations of documents and
knowledge bases are ignored (no dataset lineage). One verified telemetry gap: an agent answer
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

**Findings** (rule-based flags on each dataset record, filterable in Athena or the Quick dashboard via
the `findings` array or the flat `findingsCsv` string):
`REFRESH_FAILED`, `REFRESH_NEVER_COMPLETED`, `REFRESH_FAILURE_RECURRING` (≥3 failures in the
lookback window), `SPICE_DATASET_NOT_REFERENCED` (in no dashboard, no topic, *and* no space),
`NO_OBSERVED_USE`, `SCAN_INCOMPLETE`.

## States, schedules and dashboard records

**State.** A dataset or a dashboard is ACTIVE when its last evidence is inside the window
(`UnusedThresholdDays`, 60 by default). An event exactly that many days ago is still inside.
It is IDLE when there is no such evidence, the evidence covers the whole window, and the asset
is older than the window. An asset created exactly that many days ago is not old enough.
Otherwise it is UNKNOWN, and the record says whether the coverage is short or the asset is new,
plus the date to re-check. The dashboard control (30, 60, 90, 180) recalculates the current
tabs from the timestamps. The history line keeps the state stored at scan time.
`NO_OBSERVED_USE` means the same thing as IDLE at the module window.

**Schedules.** The slow loop reads `ListRefreshSchedules` for every SPICE dataset. The plain-words
definition, the next run and the runs in the next 7 days come from the interval, the time of day,
the time zone and the day. Assumptions:

- 15-minute, 30-minute and hourly schedules have no minute in the API. The minute comes from the
  newest SCHEDULED ingestion. With none, the label says "within" that interval and no concrete
  runs are written. The 24-hour and 7-day counts still use the runs per day, so the count does
  not drop to zero.
- Times are computed in the schedule's zone and stored in UTC. A local time that falls in a
  daylight-saving gap moves to the next real minute. An ambiguous time uses the first one.
- An unknown time zone does not fail the scan. That schedule has no next run and no planned rows,
  and the row stays, with a label that says the zone is unknown.
- A failed schedule read or a failed 30-day refresh-count read is left empty, not stored as zero.
  That dataset is left out of "with a schedule" and "without a schedule". The manual-refresh
  total for that scan is blank, not zero.
- No run is planned before `StartAfterDateTime`. `LAST_DAY_OF_MONTH` is the last day of that
  month in that zone. Manual and API refreshes are never planned.
- The refresh window kept in `dataset-lifecycle/state/ingestion-windows.json` is what the next
  run uses, so a run does not page a dataset's full ingestion history. The first pass pages back
  through the CloudTrail lookback (capped at 120 pages, enough for 90 days of a 15-minute schedule).
- "Manual refreshes in the last 30 days" is the ingestion-count metric for SPICE datasets that
  have no schedule. With a schedule, the metric cannot split manual from scheduled.

**Dashboards.** One record per dashboard per scan, including the governance dashboards. Views come
from `DashboardViewCount`. CloudTrail `GetDashboard` is the other last-view source. A dashboard
with no view in the metric history is labelled with the history start. Tombstones
(`assetState: DELETED`) are re-emitted for the retention window, the same way deleted datasets are.

## Dashboard tour

See [AWS Quick dashboard](#aws-quick-dashboard) below for the seven tabs. Two things hold on every
tab: the typed failure reasons come straight from the
[SPICE ingestion error codes](https://docs.aws.amazon.com/quick/latest/userguide/errors-spice-ingestion.html),
and usage evidence is a **signal with a confidence label, never a verdict** — the how-to note on the
Overview tab is the single most important element for interpreting the usage tables.

## Alerts

- **New refresh failure** (SNS email): fires once per distinct failed ingestion — deduplicated via
  S3 state, and re-armed when the dataset recovers. Carries dataset name, failure time, refresh
  type, typed error, message, and last-success timestamp.
- **`IngestionErrorCount ≥ 1` in 5 min** (native alarm): fastest possible detection signal.
- **Collector errors** (alarm): the scan itself is failing; data may be stale.

Alerting is operational and lives in CloudWatch alarms and SNS; visualization lives in AWS Quick.

## Where the data lives

Everything the collector writes goes to the shared analytics bucket created by the
[Analytics Foundation](../governance-analytics-foundation/README.md) — the module creates **no S3
buckets of its own**:

| Prefix | Content | Read by |
|---|---|---|
| `dataset-lifecycle/fast/dt=<date>/run-<time>.jsonl` | One record per dataset per fast run (sync health) | Glue table `dataset_lifecycle_fast`; Athena, Grafana, Datadog |
| `dataset-lifecycle/slow/dt=<date>/run-<time>.jsonl` | One record per dataset per slow run (usage, lineage, capacity, state) | Glue table `dataset_lifecycle_slow` |
| `dataset-lifecycle/dashboards/dt=<date>/run-<time>.jsonl` | One record per dashboard per slow run, plus tombstones for deleted dashboards | Glue table `dataset_lifecycle_dashboards` |
| `dataset-lifecycle/schedules/dt=<date>/run-<time>.jsonl` | One row per refresh schedule and one row per planned run in the next 7 days | Glue table `dataset_lifecycle_schedules` |
| `dataset-lifecycle/latest-fast.jsonl`, `latest-slow.jsonl`, `latest-dashboards.jsonl`, `latest-schedules.jsonl` | Rolling copies of the last runs | convenience for external tools |
| `dataset-lifecycle/state/` | alert dedupe, known datasets and dashboards, lineage caches, ingestion windows, agent scope, usage evidence | the collector only |

The state prefix sits outside the Glue table locations, the foundation's lifecycle rules never expire it
(snapshots expire after the foundation's `AnalyticsExpirationDays`), and the foundation's grant to
Quick's service role carries an explicit Deny on `*/state/*`, so no Quick data source can read it.

**Self-exclusion.** The Quick datasets created by the governance stacks themselves (IDs `quick-governance-*`)
are left out of the scan by default (`ExcludeAssetIdPrefixes`); they are Direct Query with no dependents,
so without the exclusion they would appear as unreferenced.

**Upgrading from a release with the CloudWatch dashboard or a module-owned bucket.** Re-applying removes
the CloudWatch dashboard and the per-dataset data log group (the log group is retained — delete it with
`aws logs delete-log-group` when no longer needed); alarms, KPI metrics and SNS stay. A stack still
writing to its own bucket must be migrated in this order: (1) copy the state —
`aws s3 cp --recursive s3://<module bucket>/state/ s3://<shared bucket>/dataset-lifecycle/state/` — *before*
the switch, otherwise the collector loses its alert de-duplication and rebuilds every lineage cache;
(2) re-apply (the state and access-log buckets leave the stack, retained); (3) run `RunFastLoopNowCommand`
and `RunSlowLoopNowCommand` and check that `datasets` matches the previous run; (4) copy the history with
`aws s3 sync s3://<module bucket>/snapshots/ s3://<shared bucket>/dataset-lifecycle/history/` (a sub-prefix
the live collector never writes to). The two module buckets can then be deleted (they are versioned —
delete all versions).

## AWS Quick dashboard

`cloudformation/governance-dataset-lifecycle-quick-dashboard.yaml` builds a native AWS Quick dashboard over the S3 snapshots: Glue
table(s) over the partitioned snapshot prefixes (`dataset_lifecycle_fast`, `dataset_lifecycle_slow`,
`dataset_lifecycle_dashboards`, `dataset_lifecycle_schedules`), Direct Query datasets whose SQL keeps the
latest scan per dataset (`row_number() over (partition by … order by ts desc)`) plus a refresh history
dataset and an idle-history dataset,
and a seven-tab administrator dashboard. The module's apply script deploys it after the module stack;
on its own:

```bash
cd scripts
./apply-governance-dataset-lifecycle-quick-dashboard.sh --region us-east-1
./remove-governance-dataset-lifecycle-quick-dashboard.sh --region us-east-1   # snapshots in S3 are untouched
```

| Tab | What it shows |
|---|---|
| Overview | Datasets, SPICE and direct query, FAILED last refresh, SPICE consumed and estimated cost, idle and unknown datasets, idle dashboards, refreshes in the next 24 hours, SPICE datasets without a schedule, and the last scan. The how-to note states the three states, the window and the retire order: dashboards first, then the datasets they leave unused |
| Refresh health | FAILED datasets with typed reason, slowest SPICE datasets (average vs last, full vs incremental, rows), average duration by dataset, refresh state of every dataset |
| Refresh plan | Which SPICE datasets have a schedule, the next run in plain words, planned refreshes by hour and by day, and the SPICE datasets refreshed only by hand or by API |
| Usage | Active, idle and unknown counts, every dataset stalest first with the evidence behind the state, idle datasets nothing depends on, and datasets that are not old enough or not covered for long enough yet |
| Dashboards | Every dashboard, least recently viewed first, and the idle dashboards whose same 90 days a year earlier also had no views |
| Capacity and cost | Largest SPICE datasets with their state, estimated cost by dataset, SPICE GB by confidence and reference state |
| History | Datasets in FAILED state per hourly scan, records per day by status, the failure feed, and idle datasets and idle dashboards per daily scan |

Controls (Idle after, date range on the history tab, import mode, dataset, and on Refresh plan the refresh type and data source) live in the collapsible control bar. The datasets
are Direct Query: every load reads the latest snapshots through Athena, so the dashboard is as fresh as
the last scan with no SPICE and no refresh schedule. Usage evidence is a signal with a confidence label, never a verdict: no dataset should be retired on this dashboard alone.

**Upgrading from the previous Dataset Lifecycle dashboard.** Re-apply the Analytics Foundation first,
so the new `dataset-lifecycle/dashboards/` and `dataset-lifecycle/schedules/` prefixes expire with the
other data prefixes. Then re-apply this module. `UnusedThresholdDays` now defaults to 60 (it was 30);
the apply script sends 60 unless you pass `--unused-threshold-days`. The Usage tab no longer has
"Unreferenced datasets", "Least recently used", "No usage evidence" or "Datasets by reference state
and confidence". Those rows are in "All datasets", "Retire candidates" and "Not enough history yet".
Overview no longer has the "Referenced, no observed use" KPI; it has "Idle datasets". Run the slow
loop once after the deploy so the new tables have rows.

## Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `ResourcePrefix` | `quick-governance-dataset-lifecycle` | Names every resource (lowercase). |
| `AnalyticsBucketName` | *(required)* | The foundation's shared analytics bucket: snapshots under `dataset-lifecycle/fast/` and `slow/`, state under `dataset-lifecycle/state/`. The apply script resolves it from the foundation stack. |
| `FastLoopSchedule` | `rate(1 hour)` | Sync-health cadence. |
| `SlowLoopSchedule` | `cron(15 3 * * ? *)` | Usage/lineage/capacity cadence. |
| `AlertEmail` | *(empty)* | Set to enable SNS alerts + alarm notifications. |
| `EnableCloudTrailEvidence` | `true` | CloudTrail Event history evidence for last-used. |
| `ChatLogGroupName` | `/aws/vendedlogs/quick/chat-feedback` | `CHAT_LOGS` log group (Pillar 2 module) scanned for agent-citation evidence. Skipped with a warning if absent; empty disables. |
| `AgentScopeMediumMax` | `10` | `AGENT_CONVERSATION_SCOPE` is Medium when the conversing agent can reach at most this many datasets, Low above it. |
| `SpiceRatePerGBMonth` | `0.38` | USD rate for the estimated cost signal (example us-east-1 rate — adjust per Region/agreement). |
| `UnusedThresholdDays` | `60` | Days without evidence before a dataset or dashboard is IDLE, when coverage is complete and the asset is older than the window. The dashboard control can also show 30, 90 or 180. |
| `IngestionLookbackRuns` | `50` | Newest ingestions per dataset for averages. |
| `CloudTrailLookbackDays` | `90` | First-scan Event history lookback (max 90). |
| `LogRetentionDays` | `90` | Retention of the collector's own Lambda log group. |
| `FunctionTimeoutSeconds` / `FunctionMemoryMb` | `900` / `512` | Collector sizing. |
| `ExcludeAssetIdPrefixes` | `quick-governance-` | Comma-separated dataset-ID prefixes left out of the scan (the governance stacks' own Quick datasets). Empty scans everything. |

## Deploy

Prerequisites: the [Analytics Foundation](../governance-analytics-foundation/README.md) stack in the
same account and Region (the script resolves its bucket; `--foundation-stack` if renamed), AWS SAM CLI,
AWS CLI v2 authenticated against the account/Region that holds the Quick subscription. The apply script runs `sam build` first (resolves `lambda/requirements.txt`,
which pins a boto3 recent enough for the Space/Agent/Topic-V2 lineage APIs) — that step needs a
local Python matching the function runtime (3.14) or Docker (`sam build --use-container`). The
deploying principal needs CloudFormation/IAM/Lambda/S3/SNS/CloudWatch permissions; the collector
itself is read-only towards Quick.

```bash
cd governance-dataset-lifecycle-monitor/scripts

# minimal: module stack + Quick dashboard
./apply-governance-dataset-lifecycle-monitor.sh --region us-east-1

# module only (snapshots for your own tools)
./apply-governance-dataset-lifecycle-monitor.sh --region us-east-1 --skip-dashboard

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

Open the dashboard via the `DashboardUrl` output of the dashboard stack (printed last). If you set
`AlertEmail`, confirm the SNS subscription from your inbox.

## IAM and security

- Collector permissions are read-only towards Quick (`ListDataSets`, `ListDashboards`,
  `ListTopics`, `ListTopicsV2`, `ListSpaces`, `ListSpaceResources`, `ListAgents`,
  `ListIngestions`, `ListRefreshSchedules`, `DescribeDataSet`, `DescribeDashboard`, `DescribeTopic`, `DescribeTopicV2`,
  `DescribeAgent`) plus
  `cloudwatch:GetMetricData`, `cloudtrail:LookupEvents`, Logs Insights read on the `CHAT_LOGS`
  log group when `ChatLogGroupName` is set (`logs:StartQuery` scoped to that group), scoped
  writes to its own Lambda log group, the `dataset-lifecycle/` prefix of the shared bucket and its
  metrics namespace, and optional `sns:Publish`. **No Quick
  resource permissions (ownership/sharing) are used or granted** — this is the least-privilege
  alternative to adding yourself as co-owner of every dataset.
- Failure messages can reveal infrastructure details (hosts, schemas); they are truncated to 400
  chars and stay inside your account (shared bucket, SNS topic). Grant dashboard access accordingly,
  and keep dataset names/IDs out of anything public.

## Cost

Roughly **US$2–3/month** at ~1,000 SPICE datasets / 500 dashboards on default cadences:

| Item | ~Cost/month |
|---|---|
| S3 snapshots (1k records/hour ≈ 24 MB/day) + Athena per dashboard load | ~$0.10 |
| Custom KPI metrics (≤15 total) | ~$1.80–4.50 |
| 2 alarms | $0.20 |
| Alert-topic KMS key (created only when `AlertEmail` is set) | $1.00 |
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
./remove-governance-dataset-lifecycle-monitor.sh --region us-east-1                   # dashboard stack, then module stack
./remove-governance-dataset-lifecycle-monitor.sh --region us-east-1 --keep-dashboard  # module stack only
```

Nothing in S3 is deleted: snapshots and state stay under `dataset-lifecycle/` in the foundation's
analytics bucket. Remove that data, or the foundation itself, through
`governance-analytics-foundation/scripts`.
