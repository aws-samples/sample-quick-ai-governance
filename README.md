# Amazon Quick — AI Governance

Deployable, self-contained reference modules for governing [Amazon Quick](https://aws.amazon.com/quick/) at scale — consumption visibility, interaction auditing, sharing controls, and data-asset lifecycle — together with the guidance that connects them to the controls Amazon Quick already provides natively.

Each module is independent: its own [AWS CloudFormation](https://aws.amazon.com/cloudformation/) or [AWS SAM](https://aws.amazon.com/serverless/sam/) template, `apply` / `remove` helper scripts, and a README covering prerequisites, parameters, cost, and teardown. Adopt only what you need, in any order.

## Why govern Amazon Quick

Amazon Quick lets teams build custom Chat Agents without code, ask natural-language questions over governed datasets, run deep research, and automate workflows. When that adoption moves from a pilot to hundreds or thousands of users, the question for technology leadership changes from "does it work?" to "how do we scale with cost predictability, data security, and auditability?".

That is the AI governance agenda — and it is not a brake on adoption; it is what makes adoption sustainable. Organizations that can see who consumes what, audit interactions, control how access expands, and guarantee that every answer respects existing data permissions can open the technology to more teams, faster, with less friction with security, privacy, and finance.

This repository organizes that agenda into five pillars. Pillars 1–4 combine native Amazon Quick controls with the reference modules published here. Pillar 5 is a property of the Amazon Quick architecture itself — no module is required, but it is the most important point to bring to a security review.

| Pillar | Question it answers | Native Amazon Quick capability | Reference module in this repository |
|---|---|---|---|
| 1 · Consumption | Who consumes agent hours, on which feature, and with what cap? | [Limit profiles](https://docs.aws.amazon.com/quick/latest/userguide/limit-profiles.html) (per-user caps on agent hours and index storage) | [Agent Hours Usage Monitor](./governance-agent-hours-monitor/README.md) |
| 2 · Interactions | Are people using it? Do they trust the answers? What is failing? | [`CHAT_LOGS` and `FEEDBACK_LOGS` vended logs](https://docs.aws.amazon.com/quick/latest/userguide/monitoring-cloudwatch-logs.html) | [Chat and Feedback Monitor](./governance-chat-feedback-monitor/README.md) |
| 3 · Sharing | Who may expand access to an asset, and how? | [Custom permissions](https://docs.aws.amazon.com/quick/latest/userguide/custom-permissions.html) and [approval workflows](https://docs.aws.amazon.com/quick/latest/userguide/approval-workflows.html) | [Block Sharing](./governance-block-sharing/README.md) |
| 4 · Data asset lifecycle | Is the asset estate healthy, still in use, and still owned? | [EventBridge events](https://docs.aws.amazon.com/quick/latest/userguide/events-integration.html), native CloudWatch metrics, and the [asset management console](https://docs.aws.amazon.com/quick/latest/userguide/manage-qs-assets.html) | [Spreadsheet File Rename](./governance-spreadsheet-file-rename/README.md) · [Dataset Lifecycle Monitor](./governance-dataset-lifecycle-monitor/README.md) · [Orphaned Assets Monitor](./governance-orphaned-assets-monitor/README.md) |
| 5 · Data security in agent access | Does the agent respect each user's data permissions? | [RLS](https://docs.aws.amazon.com/quick/latest/userguide/restrict-access-to-a-data-set-using-row-level-security.html) / [CLS](https://docs.aws.amazon.com/quick/latest/userguide/restrict-access-to-a-data-set-using-column-level-security.html) inherited through the semantic layer · [DLP with Microsoft Purview](https://docs.aws.amazon.com/quick/latest/userguide/data-loss-prevention.html) | — (platform property) |

```
+------------------+------------------+------------------+------------------+
| 1 - Consumption  | 2 - Interactions | 3 - Sharing      | 4 - Data assets  |
+------------------+------------------+------------------+------------------+
| Agent Hours      | Chat and         | Block Sharing    | Spreadsheet File |  <- reference modules
| Usage Monitor    | Feedback Monitor |                  | Rename           |     (this repository)
|                  |                  |                  | Dataset Lifecycle|
|                  |                  |                  | Monitor          |
|                  |                  |                  | Orphaned Assets  |
|                  |                  |                  | Monitor          |
+------------------+------------------+------------------+------------------+
| Limit profiles   | CHAT_LOGS and    | Custom           | EventBridge      |  <- native controls
| (agent hours,    | FEEDBACK_LOGS    | permissions and  | events, native   |     (Amazon Quick)
| index storage)   | vended logs      | approval         | CloudWatch       |
|                  |                  | workflows        | metrics, asset   |
|                  |                  |                  | management       |
+------------------+------------------+------------------+------------------+
| 5 - Data security in agent access: permissions inherited end to end       |  <- native platform
|     RLS / CLS evaluated per user through the semantic layer               |     property, no
|     DLP with Microsoft Purview for unstructured content                   |     module required
+---------------------------------------------------------------------------+
| Amazon Quick platform: Datasets, semantic layer (topics), Spaces,         |
| Chat Agents                                                               |
+---------------------------------------------------------------------------+
```

## Modules

| Module | Pillar | What it does | Deploy with |
|---|---|---|---|
| [Agent Hours Usage Monitor](./governance-agent-hours-monitor/README.md) | 1 | Delivers the `AGENT_HOURS_LOGS` feed to CloudWatch Logs (optionally Amazon S3) and builds an executive dashboard: total hours, top consumers, spend by feature, license-covered vs. chargeable, and overage by feature, user, and automation. | CloudFormation |
| [Chat and Feedback Monitor](./governance-chat-feedback-monitor/README.md) | 2 | Delivers `CHAT_LOGS` and `FEEDBACK_LOGS` to one KMS-encrypted log group and builds a dashboard: message volume, most active users, most-used agents (daily friendly-name resolution), response outcomes, Useful / Not Useful trend with reasons, and an audit trail with correlation IDs. | CloudFormation |
| [Block Sharing](./governance-block-sharing/README.md) | 3 | Creates a Quick custom permissions profile that denies sharing of Chat Agents, Spaces, and Datasets (optionally dashboards, analyses, data sources) and assigns it at account, role, or user scope. | CloudFormation + Quick APIs |
| [Spreadsheet File Rename](./governance-spreadsheet-file-rename/README.md) | 4 | Renames any dataset created from an uploaded spreadsheet with a standard prefix (`xls-` by default) within seconds, via an EventBridge → Lambda automation, with no change to the upload experience. | AWS SAM |
| [Dataset Lifecycle Monitor](./governance-dataset-lifecycle-monitor/README.md) | 4 | Fleet-wide dataset health without co-owning datasets: last sync status with typed failure reason, sync duration (last vs. average), SPICE capacity with an estimated cost signal, and last observed use with confidence labels — evidence from dashboards, CloudTrail, topics, and Chat Agent citations (`CHAT_LOGS`) — on a CloudWatch dashboard with SNS failure alerts and S3 snapshots. | AWS SAM |
| [Orphaned Assets Monitor](./governance-orphaned-assets-monitor/README.md) | 4 | Daily read-only reconciliation of every asset's owner grants (datasets, dashboards, analyses, data sources, folders, spaces, agents, topics) against the Quick user/group inventory: flags orphaned and missing-owner assets (HIGH), single-owner assets (bus factor 1), and recoveries, on a CloudWatch dashboard with SNS alerts. Audit-only — ownership transfer stays a human decision. | AWS SAM |

## Pillar 1 — Visibility and control of consumption

AI features in Amazon Quick — Quick Research, Flows, Automate, the desktop assistant, custom apps, and artifact generation — consume **agent hours**, the metered unit of the subscription. Usage beyond the monthly allowance included in each plan is billed per second, and index storage beyond the pooled allocation is billed per GB (see [Amazon Quick pricing](https://aws.amazon.com/quick/pricing/) for current values). Anyone accountable for the budget needs three answers: who is consuming, on which feature, and how much of it is chargeable overage?

**Native control.** The built-in usage analytics aggregate consumption by subscription tier, which is enough to track the whole but not to hold teams and users accountable. For enforcement, [limit profiles](https://docs.aws.amazon.com/quick/latest/userguide/limit-profiles.html) let an administrator define reusable per-user caps on agent hours per billing cycle and on index storage, assigned to individual users, to a role (Author or Reader), or as the account default — the most specific assignment wins. When a user reaches 100% of a cap, new agent invocations (or new uploads and knowledge base ingestions, for storage) are blocked until the next billing cycle. Existing content is always preserved, enforcement is global across Regions, and each user can follow their own consumption in the *My usage* widget.

**Reference module.** The [Agent Hours Usage Monitor](./governance-agent-hours-monitor/README.md) provisions delivery of the `AGENT_HOURS_LOGS` vended-log feed — the granular source the console does not expose — to CloudWatch Logs and, optionally, Amazon S3, and builds a dashboard with total hours for the period, top consumers, consumption trend by feature, included vs. extra hours, and the overage broken down by user and by automation. Every chargeable hour has an owner: interactive events are attributed to `user_arn`, and scheduled or deployed automations to the resource that ran them. The S3 copy feeds downstream analysis in Athena, Grafana, or Quick itself, including joins with groups and cost centers for internal chargeback. The account-total metric can back an alarm as monthly consumption approaches the contracted entitlement.

**Outcome.** Predictability in two layers: the native cap prevents a single user from exhausting the shared allowance or generating unexpected overage, and the monitor answers what the cap cannot — who consumes, on what, and with what trend.

## Pillar 2 — Interaction audit and response quality

The next questions from any adoption committee are qualitative: are people actually using it? Do they trust the answers? What is being blocked or left unanswered? Amazon Quick emits two vended-log feeds for this — `CHAT_LOGS` (messages, responses, and status) and `FEEDBACK_LOGS` (Useful / Not Useful ratings with reasons and comments) — documented in [Monitoring Amazon Quick using CloudWatch Logs](https://docs.aws.amazon.com/quick/latest/userguide/monitoring-cloudwatch-logs.html). The same delivery mechanism also serves `AGENT_HOURS_LOGS` (Pillar 1), `DLP_LOGS` (Pillar 5), `AGENT_METADATA_LOGS`, `INDEX_USAGE_LOGS`, and `KB_FILE_SYNC_LOGS`.

**Reference module.** The [Chat and Feedback Monitor](./governance-chat-feedback-monitor/README.md) delivers both feeds to a single log group and builds the dashboard: message volume, most active users, most-used agents (with daily resolution of agent friendly names), response status (success, blocked, no answer), sentiment trend with the reasons behind negative ratings, and an audit trail whose correlation IDs (`conversation_id`, `user_message_id`, `system_message_id`) tie a question, its response, and its rating together — essential during investigations.

**Two points for leadership.** First, conversation logs contain prompts and responses — potentially sensitive data. The module encrypts the log group with a dedicated AWS KMS key by default and tags it `DataClassification=Sensitive`, but controlling who can read those views, and complying with internal employee-monitoring policies, remains the organization's responsibility: involve privacy and legal teams before exposing per-user rankings. Second, vended-log delivery is **per Region and not retroactive**: enable it early, in every Region with Amazon Quick activity, because history before the delivery was configured cannot be recovered.

## Pillar 3 — Sharing and permission controls

Democratizing agent creation does not mean democratizing their distribution. A well-built Chat Agent over commercial data is a valuable asset — and sharing it with the wrong audience expands access to that asset. Amazon Quick's [custom permissions](https://docs.aws.amazon.com/quick/latest/userguide/custom-permissions.html) deny specific capabilities without stopping people from working.

**Reference module.** [Block Sharing](./governance-block-sharing/README.md) creates a custom permissions profile that denies sharing of Chat Agents, Spaces, and Datasets (optionally also dashboards, analyses, and data sources) and assigns it at the desired scope: the whole account, a role (for example, all Authors), or specific users — the most specific assignment prevails. Users keep creating and consuming governed content, but widening an audience becomes a flow controlled by the central team.

**Be clear about the limits.** The profile blocks *future* shares; it does not revoke shares already granted — remediating existing shares is separate work that changes live access and deserves its own review. Denying sharing is not complete leak prevention either: exports and downloads need complementary controls. Custom permissions require Enterprise Edition, and they cannot be assigned directly to a Quick group (use a role, or automate user-level assignments).

**Deny is not the only posture.** Amazon Quick's [approval workflows](https://docs.aws.amazon.com/quick/latest/userguide/approval-workflows.html) (Enterprise Edition, opt-in) route share requests for Spaces, knowledge bases, and custom Chat Agents to designated approver groups — existing identity groups from AWS IAM Identity Center, IAM federation, or Active Directory — before the recipient receives access. Every submit, approve, deny, and revoke event is recorded in AWS CloudTrail. Two design details matter to risk committees: an approver can test a Chat Agent before deciding, running it in the creator's context without gaining direct access to the underlying data sources (Pillar 5 in action); and an agent can be approved as a package — the agent and its dependencies (knowledge bases, connectors, Spaces) in one all-or-nothing decision, with the full dependency list visible to the approver. Note that the approver group keeps viewer-level access to the asset after the decision until the asset owner removes it.

In practice the organization gets a three-position scale per asset type and population: **allow**, **require approval**, or **deny**. Block Sharing covers the most restrictive position; approval workflows instrument the governed middle ground.

## Pillar 4 — Data asset health and lifecycle

The quality of an agent's answers is bounded by the quality of the data behind it. At scale the dataset estate grows fast — one-off spreadsheet uploads coexist with governed pipelines — and the operational question becomes: what do we have, what is healthy, and what is still used?

**[Spreadsheet File Rename](./governance-spreadsheet-file-rename/README.md)** automates a naming convention. When someone creates a dataset by uploading a spreadsheet, Amazon EventBridge captures the [dataset-created event](https://docs.aws.amazon.com/quick/latest/userguide/events-integration.html) and an AWS Lambda function renames the asset within seconds with a standard prefix (`xls-` by default; format and prefix are configurable), with no change to the uploader's experience. Administrators can tell ad hoc data from pipeline data at a glance. Governance by convention — automatic and invisible to the user.

**[Dataset Lifecycle Monitor](./governance-dataset-lifecycle-monitor/README.md)** gives the fleet view the console does not offer an enterprise administrator: the last sync status of every dataset with the typed failure reason, refresh duration (last vs. average, full vs. incremental), SPICE capacity consumed with an estimated cost signal, and the last observed use — always with a confidence label, because the available usage evidence is a signal, not a verdict. Usage evidence spans dashboard views, CloudTrail queries, topic lineage, and — when the Chat and Feedback Monitor is deployed — resources cited by Chat Agents in `CHAT_LOGS`, covering the agent consumption path that dashboard telemetry alone misses. New refresh failures raise Amazon SNS alerts. The design matters for the operating model: all collection uses IAM-authorized read-only APIs, so the governance team sees the operation without becoming co-owner of anyone's dataset or gaining access to anyone's data. The module is strictly observational: it never deletes assets, disables schedules, or changes permissions.

**[Orphaned Assets Monitor](./governance-orphaned-assets-monitor/README.md)** closes the ownership gap: Amazon Quick does not automatically transfer assets when a user is removed — the `DeleteUser` API runs no cleanup at all, and identity-provider removals only park the user in the *Inactive users* list pending review. A daily read-only scan reconciles every asset's owner grants against the current user and group inventory and flags the risk ladder: assets with no owner grants or only missing principals (HIGH — verified live: after a user is deleted the dangling grant survives briefly and is then purged, leaving an empty permission document), assets kept alive by a single active owner (the bus-factor-1 list to review before offboarding), and recoveries once ownership is restored. Findings carry evidence and confidence, are never closed after a partial scan, and remediation deliberately stays manual in the Quick asset management console — the module is audit-only by design.

The combined result is a curated, explainable estate — exactly the foundation the agents in the next pillar depend on.

## Pillar 5 — Data security in agent access: permissions inherited end to end

This is the pillar that raises the most doubt in security reviews: "if an AI agent answers any question about the data, doesn't it become a shortcut around permissions?" In Amazon Quick the answer is no — provided agent access to data is built through the governed path: datasets with a semantic layer.

The path: structured data enters as **Datasets**, assets with their own permissions. On top of them the semantic layer adds meaning and structure — dataset enrichment describes the business meaning of each field, and [multi-dataset topics](https://aws.amazon.com/blogs/machine-learning/build-a-unified-semantic-layer-across-datasets-with-multi-dataset-topics-in-amazon-quick) define the relationships between datasets once, so the service performs the joins at query time. These assets are gathered in **Spaces** and connected to custom **Chat Agents**, which answer natural-language questions by generating queries over the datasets.

**The central point: the agent does not create a parallel path to the data.** Every answer is generated in the context of the user who asks and inherits the permissions defined on the datasets — including [row-level security (RLS)](https://docs.aws.amazon.com/quick/latest/userguide/restrict-access-to-a-data-set-using-row-level-security.html) and [column-level security (CLS)](https://docs.aws.amazon.com/quick/latest/userguide/restrict-access-to-a-data-set-using-column-level-security.html). A sales representative asks the commercial agent "what were my sales this quarter?" and, with RLS on the dataset, receives only the rows for their own accounts and region — not the whole sales force. A colleague in another region asks the same agent the same question and gets their own slice; the sales director, with a broader rule, sees the consolidated figure. One question, three answers — each limited to what that user could already see. With CLS, restricted columns (margin, cost, compensation) are simply not available to anyone without access to them. Multi-dataset topics reuse the permissions of the datasets they compose: the semantic layer widens what the agent can answer without widening anyone's access.

The architectural implication is the one to bring to an executive committee: **the security rule is defined once, at the data layer, and holds for dashboards, natural-language questions, and agents** — instead of being re-implemented (and inevitably forgotten) in agent instructions. Instructions define behavior; permissions define security. A prompt is not an access control. Two precision notes: RLS restricts data *consumers* — dataset owners still see everything, so ownership should stay limited to data roles (Pillar 3 helps contain that group); and RLS / CLS are Enterprise Edition features.

The same principle — no parallel path — extends to unstructured content. Amazon Quick [integrates with Microsoft Purview to enforce data loss prevention (DLP) policies](https://docs.aws.amazon.com/quick/latest/userguide/data-loss-prevention.html): organizations already on Microsoft 365 reuse their existing sensitivity labels (such as *Confidential* or *Highly Confidential*) to control how files are handled in chat, Spaces, and knowledge bases, with Block, Warn, or Allow actions per label and no additional tooling. Two details show the maturity of the design: a default action covers unlabeled files and labels created after setup, and a provider-outage action can be configured fail-closed, blocking ingestion while Purview is unreachable. DLP decisions are emitted as their own `DLP_LOGS` feed, which slots into the monitoring of Pillar 2.

There is no module in this repository for Pillar 5 by design: RLS, CLS, and the semantic layer belong in the design of each dataset, maintained by the data teams that own it.

## Getting started

### Prerequisites

- An Amazon Quick subscription in the AWS account and Region you deploy into. Every module targets the deploying account through the `AWS::AccountId` pseudo-parameter — there is no account ID to pass — and cross-account deployment is out of scope. Vended-log delivery requires Enterprise or Professional subscriptions; Block Sharing, approval workflows, and RLS / CLS require Enterprise Edition.
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) authenticated to that account (pass `--profile <name>` to the scripts or set `AWS_PROFILE`).
- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) for the SAM-based modules (Spreadsheet File Rename, Dataset Lifecycle Monitor, Orphaned Assets Monitor).
- `jq` on `PATH` for the Block Sharing apply script.
- Deployment credentials with CloudFormation, IAM, Lambda, S3, SNS, and CloudWatch permissions as required by each template, plus `quicksight:AllowVendedLogDeliveryForResource` on the Quick account for the two log-based monitors. Amazon Quick APIs and IAM actions still use the `quicksight` namespace.

### Deploy and remove a module

Every module follows the same layout and one-command workflow:

```text
governance-<module>/
├── README.md                          prerequisites, parameters, dashboards, cost, teardown
├── cloudformation/<module>.yaml       CloudFormation or SAM template (source of truth)
├── lambda/                            Python source, SAM modules only (plus a pinned
│                                      requirements.txt where the runtime SDK lacks newer Quick APIs)
└── scripts/
    ├── apply-<module>.sh              idempotent deploy / update, prints stack outputs
    └── remove-<module>.sh             teardown
```

```bash
cd governance-<module>/scripts
./apply-governance-<module>.sh  --region <region> [--profile <aws-cli-profile>]
./remove-governance-<module>.sh --region <region> [--profile <aws-cli-profile>]
```

Each README lists the module-specific flags (for example, the Chat and Feedback Monitor requires an explicit confirmation to delete conversation history, and Block Sharing takes a `--scope`). The templates can also be deployed directly with `aws cloudformation deploy` or `sam deploy`.

### Region matters

- Vended-log delivery (Pillars 1 and 2) is per Region: deploy the monitors in every Region with Amazon Quick activity.
- EventBridge events (Spreadsheet File Rename) fire in the Region where the dataset lives.
- Asset inventories (Dataset Lifecycle Monitor, Orphaned Assets Monitor) are per Region: deploy one stack per Region that holds Quick assets. The Orphaned Assets Monitor discovers the identity Region (users and groups) automatically when it differs from the asset Region.
- Custom permissions (Block Sharing) must be called against the account's Quick capacity Region.

### Suggested adoption sequence

The five pillars form a simple operating model: the platform team or data center of excellence deploys the monitoring and control modules in the account and Regions where Amazon Quick runs, while data teams keep RLS / CLS and the semantic layer as part of each dataset's design.

1. **Pillars 1 and 5 first** — consumption under control, and data permissions guaranteed from the first agent in production.
2. **Pillar 2 early** — log delivery is not retroactive, so start it before you need the history.
3. **Pillars 3 and 4** as the number of creators and datasets grows.

### Cost

Operating cost is on the order of a few US dollars per month per module, mostly CloudWatch Logs ingestion and storage; Block Sharing has no runtime cost and Spreadsheet File Rename is pay-per-use. Each module README breaks down its own cost drivers and defaults (retention, schedules, optional S3 delivery).

## Repository layout

```text
.
├── governance-agent-hours-monitor/         Pillar 1 · CloudFormation
├── governance-chat-feedback-monitor/       Pillar 2 · CloudFormation
├── governance-block-sharing/               Pillar 3 · CloudFormation + Quick APIs
├── governance-spreadsheet-file-rename/     Pillar 4 · AWS SAM (Python Lambda)
├── governance-dataset-lifecycle-monitor/   Pillar 4 · AWS SAM (Python Lambda)
└── governance-orphaned-assets-monitor/     Pillar 4 · AWS SAM (Python Lambda)
```

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
