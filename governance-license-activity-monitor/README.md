> **Sample code — review and adapt before production use.** Not an AWS service and not supported by AWS; see the [repository disclaimer](../README.md#disclaimer).

# License and Activity Monitor

Last observed activity for every AWS Quick user, oldest first, and whether that user can be reviewed for removal. The license table shows who can move to another tier, the list-price saving, and the month the saving starts.

Last observed activity is the newest CloudTrail or Agent Hours event this module can attribute to the user. It is not the console's Last active. No API returns that console field, and a sign-in never counts as activity.

## Layout

```
governance-license-activity-monitor/
├── README.md
├── cloudformation/
│   ├── governance-license-activity-monitor.yaml
│   └── governance-license-activity-quick-dashboard.yaml
├── lambda/
│   ├── collector.py
│   ├── license_rules.py
│   └── requirements.txt
├── scripts/
│   ├── apply-governance-license-activity-monitor.sh
│   ├── remove-governance-license-activity-monitor.sh
│   ├── apply-governance-license-activity-quick-dashboard.sh
│   └── remove-governance-license-activity-quick-dashboard.sh
└── tests/
    └── test_license_rules.py
```

## What the stack creates

The module stack creates the collector Lambda, its role, a log group, a daily EventBridge schedule at 04:30 UTC, and one collector-error alarm. The dashboard stack creates three Glue tables, four Direct Query datasets, and the dashboard. Snapshots go to the foundation bucket under `license-activity/`. The module creates no bucket of its own.

## Data sources

1. ListUsers in the identity Region. The Region is discovered from the ListUsers error when `IdentityRegion` is empty, the same way the Orphaned Assets collector does it.
2. CloudTrail Event history, newest first, in the identity Region and the deploy Region unless `ActivityRegions` lists others. The first run's 90-day backfill continues across later runs from a cursor in state.
3. Agent Hours objects under `agent-hours/`, read incrementally. Skipped with a warning when the prefix is absent.
4. `orphaned-assets/latest-ownership.jsonl`, for assets owned and assets owned alone. Skipped when the object is absent.
5. BatchDescribeUserLimits and ListUsersIndexCapacity. A Region with no index is recorded as missing, not as a failed scan.

## States and license rules

The window defaults to 60 days.

- PENDING: ListUsers `Active` is false.
- ACTIVE: last observed activity is inside the window.
- IDLE: no activity inside the window, and the CloudTrail coverage covers the whole window.
- UNKNOWN: the coverage is shorter than the window. The row says so and gives a re-check date.

IDLE users and administrators are not license-fit candidates. Essentials and Professional are proposed only when their included hours cover the user's peak month of the last 3 full months, unless the current tier has a limit that allows overage. Enterprise can be cheaper when that overage costs more than the step up. A partial Agent Hours read, a user's own AI day with no hours row, a stale feed, or coverage shorter than the window gives low confidence and no saving. The saving starts the first month that begins after the change, because billing uses the highest tier held in a month.

Prices, included hours, and the role-to-tier map live in `lambda/license_rules.py`, with the as-of date of the [AWS Quick pricing page](https://aws.amazon.com/quick/pricing/). The account fee of USD 250 is not part of a user's saving. A role whose price is not on that page shows "price unknown" and no saving.

## Dashboard tour

| Tab | What it shows |
|---|---|
| Overview | Users, active, idle, pending, unknown, downgrade candidates, estimated saving, and the last scan |
| Users | Every user, longest idle first, with removal readiness. Pending invitations are a separate table |
| License fit | Active users only. Current tier, peak hours, the cheapest tier that covers the use, the saving, and the month it starts |
| Evidence | Each data source, its coverage, and days in the last 30 with AI activity in CloudTrail but no Agent Hours record. CloudTrail is SHORT while the 90-day backfill is still open, and MISSING only when the scan read no events |
| History | Idle users and estimated saving per daily scan, from the state stored at scan time |

The Idle after control (30, 60, 90, 180) recalculates Overview, Users, and License fit. History does not move with it.

## Where the data lives

| Prefix | Content |
|---|---|
| `license-activity/users/` | One row per user per scan, plus tombstones |
| `license-activity/sources/` | One row per data source per scan |
| `license-activity/tiers/` | One row per user and tier per scan |
| `license-activity/state/` | Cursors, attributed hits, and the hours already read. Quick cannot read this prefix |

## Parameters

| Parameter | Default | Purpose |
|---|---|---|
| `UnusedThresholdDays` | 60 | Days without activity before a covered user is IDLE |
| `DailySchedule` | `cron(30 4 * * ? *)` | 04:30 UTC, away from the Dataset Lifecycle slow loop |
| `IdentityRegion` | empty | Discover the Quick identity Region |
| `ActivityRegions` | empty | CloudTrail Regions. Empty means the identity Region and the deploy Region |
| `AnalyticsBucketName` | from the foundation | Shared bucket |

## Deploy

1. Re-apply the foundation first when this is an upgrade, so the new prefixes expire with the other data prefixes.

```bash
governance-analytics-foundation/scripts/apply-governance-analytics-foundation.sh --region us-east-1 --profile default
```

2. Deploy the module. The script deploys the dashboard after the module unless you pass `--skip-dashboard`. `--no-execute` stops after the change set.

```bash
governance-license-activity-monitor/scripts/apply-governance-license-activity-monitor.sh --region us-east-1 --profile default
```

3. Run one scan. The command is the stack output `RunNowCommand`.

## Remove

```bash
governance-license-activity-monitor/scripts/remove-governance-license-activity-monitor.sh --region us-east-1 --profile default
```

Snapshots in the foundation bucket are kept.

## Cost

One Lambda run per day, CloudTrail LookupEvents under the account's 2 transactions per second, and Athena queries when the dashboard is opened. There is no SPICE dataset and no refresh schedule. Estimates on the dashboard are list prices (`listPriceUsd`), not an invoice.

## Upgrade

Re-apply the foundation, then this module. The first scans finish the 90-day CloudTrail backfill across more than one run. Until that backfill finishes, users are UNKNOWN rather than IDLE.
