# Amazon Quick — Orphaned Assets Monitor

> [!IMPORTANT]
> **Sample code — review and adapt before production use.** Not an AWS service and not supported by AWS; see the [repository disclaimer](../README.md#disclaimer).

Fleet-wide ownership assurance for every Amazon Quick asset — **who owns it, whether those
owners still exist, and which assets are one departure away from being orphaned** — on a native
Amazon Quick dashboard, with SNS alerts and alarms for new HIGH findings, over S3 JSONL snapshots that
any downstream tool can also read. Strictly **audit-only**: the monitor never transfers ownership and
never deletes anything.

Amazon Quick does not automatically transfer a user's assets when that user leaves:

| Removal path | What happens to the user's assets |
|---|---|
| Console deletion by an administrator | The administrator chooses transfer-or-delete during deletion. |
| `DeleteUser` API | **No cleanup runs.** Assets stay in the account without an owner until an administrator transfers them. |
| Removal from IAM Identity Center / Active Directory | No automatic cleanup. The user parks in the **Inactive users** list pending review; assets stay untouched. |

(See [User lifecycle and data handling in Amazon Quick](https://docs.aws.amazon.com/quick/latest/userguide/user-lifecycle-data-handling.html).)
The asset management console supports manual discovery and transfer; this module adds the
continuous, account-wide detection layer in front of it.

## Module layout

```
governance-orphaned-assets-monitor/
├── README.md
├── cloudformation/
│   └── governance-orphaned-assets-monitor.yaml    # SAM template (source of truth)
├── lambda/
│   ├── collector.py                               # daily ownership reconciliation
│   └── requirements.txt                           # pinned boto3 (Space/Agent/Topic-V2 APIs)
└── scripts/
    ├── apply-governance-orphaned-assets-monitor.sh   # wraps: sam build + sam deploy
    └── remove-governance-orphaned-assets-monitor.sh  # teardown (S3 bucket retained)
```

## Architecture

```
 EventBridge schedule — daily cron(45 3 * * ? *)
        |
        v
 Collector Lambda (read-only, ReservedConcurrency=1)
  |-- discover identity region (differs from the asset region; auto-parsed
  |     from the API's own error hint, cached in S3)
  |-- ListUsers + ListGroups(+memberships) per namespace
  |-- per asset type: List* inventory + Describe*Permissions per asset
  |     (paced <=4.5 TPS under the 5 TPS/user quota)
  |-- classify every owner grant against the identity inventory
  |-- evaluate finding rules; track finding lifecycle in S3 state
        |
        +--> JSONL snapshot per scan   ->  shared analytics bucket, orphaned-assets/ownership/dt=.../
        +--> finding + identity state  ->  shared analytics bucket, orphaned-assets/state/
        +--> 11 fleet KPIs             ->  CloudWatch metrics QuickGovernance/OrphanedAssets (alarms)
        +--> alert on NEW HIGH finding ->  SNS (optional)
        |
        v  Glue table orphaned_assets_snapshots · Athena (Direct Query)
 AMAZON QUICK DASHBOARD (companion stack)
  - Overview: open findings / HIGH / single-owner / healthy, coverage by type, HIGH action list
  - Findings · Ownership · History
```

Asset types scanned (all verified live, configurable via `AssetTypes`): **datasets, dashboards,
analyses, data sources, shared folders, spaces, agents, topics**. Soft-deleted analyses
(`Status: DELETED`, 30-day restore window) and the service-managed SYSTEM default agent are
excluded by design. Storage is S3 only — the shared analytics bucket holds the snapshots the
dashboard queries and the collector's working state; no database, no module-owned buckets.

## Ownership semantics the module applies

**Owner rule (verified against live permission documents of all eight types).** A grant is an
*owner* grant if and only if its actions include `quicksight:Update<Type>Permissions` — owner
grants also carry `Delete<Type>`, viewer grants carry neither.

**Principal classification.** Every owner principal is resolved against the current inventory:
`ACTIVE_USER` · `PENDING_USER` (exists, never activated) · `USER_MISSING` ·
`GROUP_WITH_MEMBERS` · `GROUP_EMPTY` · `GROUP_MISSING` · `NAMESPACE_GRANT` (account-wide) ·
`UNKNOWN_PRINCIPAL`. A missing principal is **probable** deletion, not confirmed — identity may
have moved or the inventory may be incomplete; every finding carries `confidence`
(`QUICK_INVENTORY` / `PROBABLE`).

**Finding rules** (evaluated per asset over the complete owner set):

| Finding | Severity | Meaning |
|---|---|---|
| `NO_OWNER_GRANTS` | High | The permission document has no owner grant at all — the steady state of an orphan (see below). |
| `OWNER_PRINCIPAL_MISSING` | High | Every owner ARN is absent from the current inventory. |
| `NO_ACTIVE_OWNER` | High | Owners exist but none resolves to an active identity. |
| `ONLY_INACTIVE_POLICY_OWNERS` | Medium | All owners exist but none ever activated. |
| `SINGLE_ACTIVE_OWNER` | Medium | Exactly one active owner — the bus-factor-1 list to review before offboarding. |
| `MIXED_OWNER_STATUS` | Low | At least one active owner, plus missing/inactive co-owners. |
| `OWNER_STATUS_UNKNOWN` | Low | Classification failed (API error); never treated as orphaned. |
| `RECOVERED` | Info | A HIGH finding cleared (ownership restored or asset removed), only after a COMPLETE scan. |

Group-owned assets with members and namespace grants count as actively owned. Findings keep
`firstObservedAt` across scans; **partial scans never close findings**.

**Verified deletion behavior (live experiment).** After `DeleteUser`, the asset survives and
its grant remains **verbatim for a transition window**, then Quick purges it asynchronously —
the lasting orphan signature is an **empty permission document** (`NO_OWNER_GRANTS`), with
`OWNER_PRINCIPAL_MISSING` catching the window. The deleted user disappears from `ListUsers`
immediately, and owner-search APIs reject deleted ARNs — which is why detection requires this
permissions sweep, not a search. End-to-end detection, alerting, and recovery were verified
against a real orphaned asset.

## Dashboard tour

See [Amazon Quick dashboard](#amazon-quick-dashboard) below for the four tabs. The constant across
them: the **finding ladder** (HIGH orphaned or missing-owner → MEDIUM at-risk → LOW mixed or unknown),
the confidence label on every finding, and the reminder that remediation is a manual, human decision.

## Alerts

- **New HIGH finding** (SNS email, optional): fires once per asset when a finding first becomes
  HIGH, with the finding type, asset, confidence, owner grants, and a link to the transfer
  procedure. Re-fires only if the finding type changes.
- **`HighSeverityFindings ≥ 1`** (CloudWatch alarm on the collector's KPI metric): open HIGH findings
  exist (daily granularity).
- **Collector errors** (alarm): the scan itself failed; findings may be stale.

Alerting is operational and lives in CloudWatch alarms and SNS; visualization lives in Amazon Quick.

## Where the data lives

Everything the collector writes goes to the shared analytics bucket created by the
[Analytics Foundation](../governance-analytics-foundation/README.md) — the module creates **no S3
buckets of its own**:

| Prefix | Content | Read by |
|---|---|---|
| `orphaned-assets/ownership/dt=<date>/run-<time>.jsonl` | One JSONL snapshot per scan, one record per asset | the Quick dashboard's Glue table; Athena, Grafana, Datadog |
| `orphaned-assets/latest-ownership.jsonl` | Rolling copy of the last scan | convenience for external tools |
| `orphaned-assets/state/` | `findings.json` (finding lifecycle), `identity.json` (identity cache) | the collector only |

The state prefix sits outside the Glue table location, the foundation's lifecycle rules never expire it
(snapshots expire after the foundation's `AnalyticsExpirationDays`), and the foundation's grant to
Quick's service role carries an explicit Deny on `*/state/*`, so no Quick data source can read it.

**Self-exclusion.** The Quick assets created by the governance stacks themselves (IDs `quick-governance-*`)
are left out of the scan by default (`ExcludeAssetIdPrefixes`), so the monitor does not report on its own
dashboards and datasets. Exclusion hides the finding; to fix the underlying bus factor, own the governance assets with a Quick **group** rather than a single user (`QuickPrincipalArn` on the foundation stack).

**Upgrading from a release with the CloudWatch dashboard or a module-owned bucket.** Re-applying removes
the CloudWatch dashboard and the per-asset data log group (the log group is retained — delete it with
`aws logs delete-log-group` when no longer needed); alarms, KPI metrics and SNS stay. A stack still
writing to its own bucket must be migrated in this order: (1) copy the state —
`aws s3 cp --recursive s3://<module bucket>/state/ s3://<shared bucket>/orphaned-assets/state/` — *before*
the switch, otherwise the first scan starts with no memory and re-fires every open finding as NEW;
(2) re-apply (the state and access-log buckets leave the stack, retained); (3) run `RunScanNowCommand`
and check `"recovered": 0` / `"newHigh": 0`; (4) copy the history with
`aws s3 sync s3://<module bucket>/snapshots/ownership/ s3://<shared bucket>/orphaned-assets/ownership/history/`
(a sub-prefix the live collector never writes to). The two module buckets can then be deleted (they are
versioned — delete all versions).

## Amazon Quick dashboard

`cloudformation/governance-orphaned-assets-quick-dashboard.yaml` builds a native Amazon Quick dashboard over the S3 snapshots: Glue
table(s) over the partitioned snapshot prefix (`orphaned_assets_snapshots`, without the `ownerPrincipals` column), Direct Query datasets whose SQL keeps the
latest scan per asset (`row_number() over (partition by … order by ts desc)`) plus a history dataset,
and a four-tab administrator dashboard. The module's apply script deploys it after the module stack;
on its own:

```bash
cd scripts
./apply-governance-orphaned-assets-quick-dashboard.sh --region us-east-1
./remove-governance-orphaned-assets-quick-dashboard.sh --region us-east-1   # snapshots in S3 are untouched
```

| Tab | What it shows |
|---|---|
| Overview | Assets scanned, orphaned (HIGH), HIGH / MEDIUM counts, single-owner (bus factor 1), healthy, asset types covered; coverage by asset type; findings by type; the HIGH action list oldest-first; a how-to note on the finding ladder |
| Findings | The full HIGH, MEDIUM and LOW lists from the latest scan with days open, owner / viewer counts and first-observed dates |
| Ownership | Coverage by asset type, assets by type × finding, and the single-owner list to co-own before the owner leaves |
| History | Findings per scan by severity, assets scanned per day by type, recovered findings, scans (status, counts) |

Controls (date range on the history tab, asset type, severity, finding) live in the collapsible control bar. The datasets
are Direct Query: every load reads the latest snapshots through Athena, so the dashboard is as fresh as
the last scan with no SPICE and no refresh schedule. Owner principal names are deliberately not declared in the Glue table — asset names and counts are enough to act on, and remediation stays manual in the Quick asset management console.

## Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `ResourcePrefix` | `quick-governance-orphaned-assets` | Names every resource (lowercase). |
| `AnalyticsBucketName` | *(required)* | The foundation's shared analytics bucket: snapshots under `orphaned-assets/ownership/`, state under `orphaned-assets/state/`. The apply script resolves it from the foundation stack. |
| `ScheduleExpression` | `cron(45 3 * * ? *)` | Daily reconciliation cadence. |
| `AlertEmail` | *(empty)* | Set to enable SNS alerts + alarm notifications. |
| `IdentityRegion` | *(empty = auto-discover)* | Region hosting the Quick user/group APIs when it differs from the asset Region. |
| `Namespaces` | `default` | Comma-separated namespaces to inventory. |
| `AssetTypes` | all eight | Comma-separated asset types to scan. |
| `LogRetentionDays` | `90` | Retention of the collector's own Lambda log group. |
| `FunctionTimeoutSeconds` / `FunctionMemoryMb` | `900` / `512` | Collector sizing. |
| `ExcludeAssetIdPrefixes` | `quick-governance-` | Comma-separated asset-ID prefixes left out of the scan (the governance stacks' own Quick assets). Empty scans everything. |

## Deploy

Prerequisites: the [Analytics Foundation](../governance-analytics-foundation/README.md) stack in
the same account and Region (the script resolves its bucket; `--foundation-stack` if renamed), AWS SAM
CLI (the build step needs a local Python 3.14 or Docker for `sam build --use-container`), AWS CLI v2
authenticated against the account/Region that holds the Quick assets. `sam build` resolves `lambda/requirements.txt`, which pins a boto3 recent
enough for the Space/Agent/Topic-V2 permission APIs.

```bash
cd governance-orphaned-assets-monitor/scripts

# minimal: module stack + Quick dashboard
./apply-governance-orphaned-assets-monitor.sh --region us-east-1

# module only (snapshots for your own tools)
./apply-governance-orphaned-assets-monitor.sh --region us-east-1 --skip-dashboard

# with alerts
./apply-governance-orphaned-assets-monitor.sh \
    --region us-east-1 --profile my-profile \
    --alert-email bi-admins@example.com
```

Then trigger a first scan immediately (also printed as a stack output):

```bash
aws lambda invoke --function-name quick-governance-orphaned-assets-fn \
  --payload '{}' --cli-binary-format raw-in-base64-out --region us-east-1 /dev/stdout
```

Open the dashboard via the `DashboardUrl` output of the dashboard stack (printed last). If you set
`AlertEmail`, confirm the SNS subscription from your inbox.

**Acting on findings** stays manual by design, in the
[Quick asset management console](https://docs.aws.amazon.com/quick/latest/userguide/manage-qs-assets.html)
(**Manage Quick → Manage assets**):

- **HIGH (orphaned)** — use **Transfer** to move the asset to a new owner ("when the original
  owner is no longer present", per the console's own use cases).
- **MEDIUM (`SINGLE_ACTIVE_OWNER`)** — remediate *proactively*: select the assets and use
  **Actions → Share** to add an administrator or a recovery group as **co-owner**
  ([walkthrough](https://aws.amazon.com/blogs/big-data/govern-and-manage-permissions-of-amazon-quicksight-assets-with-the-new-centralized-asset-management-console/)).
  This is the cheapest insurance in the module: Amazon Quick
  [never deletes an asset that has more than one owner](https://docs.aws.amazon.com/quick/latest/userguide/user-lifecycle-data-handling.html)
  when a user is removed — the departed user is simply dropped from the permissions and the
  asset stays available to its co-owners, whatever the removal path.

The next scan records the improvement automatically (`RECOVERED` for cleared HIGH findings;
single-owner findings close once a second active owner exists).

## IAM and security

- Collector permissions are read-only towards Quick: the `List*` inventory operations, the
  `Describe*Permissions` operation per asset type (resource-scoped to this Region's assets),
  and `ListUsers` / `ListGroups` / `ListGroupMemberships` (region-wildcarded, because the
  identity region can differ from the asset region), plus scoped writes to its own Lambda log
  group, the `orphaned-assets/` prefix of the shared bucket, and its metrics namespace, and optional
  `sns:Publish`. **No mutating Quick action is
  granted** — ownership transfer requires a human in the asset management console.
- Ownership findings identify people (owner principal names) — that is their purpose. They stay
  inside your account (shared bucket, SNS topic); the Glue table the dashboard reads omits the
  owner principal names, and the state prefix is denied to Quick's service role. No personal data is
  placed in metric dimensions or alarm names.

## Cost

Roughly **US$3–4/month** on default settings, dominated by the 11 KPI metrics (~$3.30), plus
**US$1/month for the alert-topic KMS key when `AlertEmail` is set** (SNS encryption at rest); the
rest — one short Lambda run per day, two alarms, S3 snapshots and state, Athena per dashboard load —
is pennies. Quick and identity API calls made by the collector are free.

## Limitations

- **Identity evidence is Quick-inventory only** (`QUICK_ONLY`): a missing principal is probable
  deletion, not authoritative — reconciliation with IAM Identity Center or an HR roster is
  future work. Correspondingly, there is no activity-based "inactive owner" policy yet:
  `PENDING_USER` means never-activated, not dormant.
- **One asset Region per deployment.** Deploy the stack in each Region with Quick assets; the
  identity region is discovered automatically.
- **Scale**: the paced sweep (~4.5 TPS) handles roughly 3,000–4,000 assets within the Lambda
  15-minute limit. Beyond that, split by `AssetTypes` into separate stacks or reduce scope.
- **Transition-window semantics**: right after a user deletion an asset may briefly report
  `OWNER_PRINCIPAL_MISSING` before Quick purges the grant and it becomes `NO_OWNER_GRANTS`;
  both are HIGH, so alerting is unaffected.
- **The HIGH alarm evaluates the day's worst value** (stat Maximum over the daily bucket): after
  remediating mid-day, the alarm reflects the fix on the next day's bucket; the dashboard reflects
  it on the next scan.

## Non-goals

- **Auto-remediation**: the module never transfers ownership, never deletes users or assets,
  never edits permissions. An approval-based transfer workflow (separate mutating role, named
  approver, verified before/after state) is a possible future phase — deliberately not built.
- **Offboarding replacement**: this monitor complements — never replaces — IAM Identity Center,
  Active Directory, or HR offboarding processes.
- **Usage/health monitoring**: that is the
  [Dataset Lifecycle Monitor](../governance-dataset-lifecycle-monitor/README.md); join both
  modules' S3 snapshots to rank transfers by usage (an orphaned dataset serving active agents
  is urgent; an orphaned, unused one is an archive candidate).

## Teardown

```bash
cd governance-orphaned-assets-monitor/scripts
./remove-governance-orphaned-assets-monitor.sh --region us-east-1                   # dashboard stack, then module stack
./remove-governance-orphaned-assets-monitor.sh --region us-east-1 --keep-dashboard  # module stack only
```

Nothing in S3 is deleted: snapshots and state stay under `orphaned-assets/` in the foundation's
analytics bucket. Remove that data, or the foundation itself, through
`governance-analytics-foundation/scripts`.
