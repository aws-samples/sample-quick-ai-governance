# Amazon Quick — Orphaned Assets Monitor

Fleet-wide ownership assurance for every Amazon Quick asset — **who owns it, whether those
owners still exist, and which assets are one departure away from being orphaned** — on a
CloudWatch dashboard, with SNS alerts for new HIGH findings and S3 JSONL snapshots for
downstream analysis. Strictly **audit-only**: the monitor never transfers ownership and never
deletes anything.

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
        +--> 1 JSON ownership event per asset  ->  CloudWatch Logs data log group
        +--> JSONL snapshot per scan           ->  S3 (dt=... partitions + latest)
        +--> 11 fleet KPIs                     ->  QuickGovernance/OrphanedAssets
        +--> alert on NEW HIGH finding         ->  SNS (optional)
        |
        v
 CloudWatch DASHBOARD
  - KPI tiles (open findings / HIGH / single-owner / assets+users / partial / recovered)
  - "How to read ownership findings" legend
  - HIGH: orphaned or missing-owner assets
  - MEDIUM: at-risk assets (bus factor 1)
  - Ownership coverage by asset type · LOW table · recovered feed
```

Asset types scanned (all verified live, configurable via `AssetTypes`): **datasets, dashboards,
analyses, data sources, shared folders, spaces, agents, topics**. Soft-deleted analyses
(`Status: DELETED`, 30-day restore window) and the platform-managed SYSTEM default agent are
excluded by design. Storage reuses the repository chassis — CloudWatch Logs as the queryable
system of record, S3 for state and snapshots, no database.

## Ownership semantics the module guarantees

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

- **KPI tiles** — open findings, HIGH severity, single-owner assets, assets|users scanned,
  partial scans, recovered findings. Tiles read the current day's bucket (`liveData`), so the
  morning scan is visible immediately; values are the day's maximum (worst state today).
- **How to read ownership findings** — always-visible legend: the owner rule, the finding
  ladder, confidence semantics, and the reminder that remediation is manual.
- **HIGH — orphaned or missing-owner assets** — the action list, oldest finding first, with
  owner counts and principals.
- **MEDIUM — at-risk assets** — single-owner and inactive-only-owner assets per type.
- **Ownership coverage by asset type** — assets vs healthy vs high-risk vs single-owner.
- **Recovered findings (feed)** and **LOW — mixed or unknown owner status**.

## Alerts

- **New HIGH finding** (SNS email, optional): fires once per asset when a finding first becomes
  HIGH, with the finding type, asset, confidence, owner grants, and a link to the transfer
  procedure. Re-fires only if the finding type changes.
- **`HighSeverityFindings ≥ 1`** (alarm): open HIGH findings exist (daily granularity).
- **Collector errors** (alarm): the scan itself failed; findings may be stale.

## Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `ResourcePrefix` | `quick-governance-orphaned-assets` | Names every resource (lowercase; also in the bucket name). |
| `DataLogGroupName` | `/quick-governance/orphaned-assets` | Data log group queried by the dashboard. |
| `ScheduleExpression` | `cron(45 3 * * ? *)` | Daily reconciliation cadence. |
| `AlertEmail` | *(empty)* | Set to enable SNS alerts + alarm notifications. |
| `IdentityRegion` | *(empty = auto-discover)* | Region hosting the Quick user/group APIs when it differs from the asset Region. |
| `Namespaces` | `default` | Comma-separated namespaces to inventory. |
| `AssetTypes` | all eight | Comma-separated asset types to scan. |
| `LogRetentionDays` | `90` | Data + Lambda log group retention. |
| `SnapshotExpirationDays` | `365` | S3 snapshot lifecycle expiry (finding history). |
| `FunctionTimeoutSeconds` / `FunctionMemoryMb` | `900` / `512` | Collector sizing. |

## Deploy

Prerequisites: AWS SAM CLI (the build step needs a local Python 3.14 or Docker for
`sam build --use-container`), AWS CLI v2 authenticated against the account/Region that holds
the Quick assets. `sam build` resolves `lambda/requirements.txt`, which pins a boto3 recent
enough for the Space/Agent/Topic-V2 permission APIs.

```bash
cd governance-orphaned-assets-monitor/scripts

# minimal
./apply-governance-orphaned-assets-monitor.sh --region us-east-1

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

Open the dashboard via the `DashboardUrl` stack output. If you set `AlertEmail`, confirm the
SNS subscription from your inbox.

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
  identity region can differ from the asset region), plus scoped writes to its own log group,
  bucket, and metrics namespace, and optional `sns:Publish`. **No mutating Quick action is
  granted** — ownership transfer requires a human in the asset management console.
- Ownership findings identify people (owner principal names) — that is their purpose. They stay
  inside your account (log group, bucket, SNS topic); grant dashboard/log access accordingly.
  No personal data is placed in metric dimensions or alarm names.
- The S3 bucket blocks public access, is SSE-encrypted, and is retained on stack delete.

## Cost

Roughly **US$3–4/month** on default settings, dominated by the 11 KPI metrics (~$3.30); the
rest — one short Lambda run per day, one log event per asset per scan, two alarms, S3 state —
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
- **KPI tiles show the day's worst value** (stat Maximum over the daily bucket): after
  remediating mid-day, tiles and the HIGH alarm reflect the fix on the next day's bucket even
  though the tables update on the next scan.

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
./remove-governance-orphaned-assets-monitor.sh --region us-east-1
```

The S3 bucket is retained (finding history). Remove it manually when no longer needed:

```bash
aws s3 rb "s3://quick-governance-orphaned-assets-<account-id>" --force --region us-east-1
```
