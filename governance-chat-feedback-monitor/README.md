# Amazon Quick — Chat and Feedback Monitor

> [!IMPORTANT]
> **Sample code — review and adapt before production use.** Not an AWS service and not supported by AWS; see the [repository disclaimer](../README.md#disclaimer).

Monitor **messages sent to Amazon Quick**, the users sending them, the agents and flows being used,
response outcomes, and **Useful / Not Useful** feedback on a native **Amazon Quick dashboard** fed by
a field-minimized S3 copy that never contains conversation content — while the full conversation
record stays in an encrypted CloudWatch Logs log group as the audit trail.

The source is Amazon Quick's regional `CHAT_LOGS` and `FEEDBACK_LOGS` vended-log feeds. CloudTrail is
not the source for conversation content or feedback. The module delivers both feeds twice: complete,
to one KMS-encrypted log group; and without prompts, responses or comments, to the shared analytics
bucket of the [Analytics Foundation](../governance-analytics-foundation/README.md), where the Quick
dashboard reads them through Athena.

> **Sensitive data warning:** chat logs include user prompts, Quick responses, selected resources,
> citations, and attachment metadata. Feedback can include free-form comments. The module encrypts
> the log group with a customer managed KMS key by default and tags it `DataClassification=Sensitive`,
> but IAM access control, data classification, and any data-protection masking policy remain your
> responsibility. Do not deploy this module until those controls satisfy your privacy and compliance
> requirements.

## Module layout

```text
governance-chat-feedback-monitor/
├── README.md
├── cloudformation/
│   ├── governance-chat-feedback-monitor.yaml             # the module stack (source of truth)
│   └── governance-chat-feedback-quick-dashboard.yaml     # the Amazon Quick dashboard stack
└── scripts/
    ├── apply-governance-chat-feedback-monitor.sh          # module stack + Quick dashboard
    ├── remove-governance-chat-feedback-monitor.sh         # tears both down
    ├── apply-governance-chat-feedback-quick-dashboard.sh  # the dashboard stack alone
    └── remove-governance-chat-feedback-quick-dashboard.sh
```

## Architecture

```text
Amazon Quick (one account and Region)
  ├─ CHAT_LOGS delivery source ─────┬──────────────────────────────┐
  └─ FEEDBACK_LOGS delivery source ─┤                              │ RecordFields: no user_message,
                                    v                              │ system_text_message, feedback_details
                    CloudWatch Logs log group                      v
                    /aws/vendedlogs/quick/chat-feedback     SHARED ANALYTICS BUCKET (foundation)
                    ├─ KMS encryption (default)              chat-feedback/chat/
                    ├─ 90-day retention (default)            chat-feedback/feedback/
                    └─ the content AUDIT TRAIL               chat-feedback/agent-names/agent-names.csv
                                                                   ^                 |
  AGENT_METADATA_LOGS delivery source                              |                 | Glue tables (no content columns)
      └─ <log group>-agent-metadata ── subscription filter ──> resolver Lambda       | Athena (Direct Query)
         (create / rename / delete)        daily EventBridge rule ─┘                 v
                                           ListAgents -> CSV export         AMAZON QUICK DASHBOARD
                                                                            Overview · Users · Agents ·
                                                                            Quality · Events
```

Vended-log delivery is **regional and non-retroactive**. Deploy this module separately in every
Region where Amazon Quick activity occurs, and deploy it soon after enabling Quick AI features.

## What the stack creates

- One `AWS::Logs::LogGroup` for chat and feedback events — the only place conversation content exists.
- An optional dedicated `AWS::KMS::Key` and alias (enabled by default), with annual rotation.
- Up to two `AWS::Logs::DeliverySource` resources: `CHAT_LOGS` and `FEEDBACK_LOGS`. The deploy
  script reuses matching sources when they already exist because a Quick account/log-type pair is unique.
- Two CloudWatch Logs delivery destinations and deliveries into the log group. A reused source can
  continue delivering to existing S3 or Firehose destinations while also feeding this module.
- Two S3 delivery destinations and deliveries into the shared analytics bucket (`chat-feedback/chat/`
  and `chat-feedback/feedback/`), **field-minimized** through the delivery's `RecordFields`.
- One minimal arm64 Lambda (128 MB, 900-second timeout) that exports the `agent_id,agent_name` CSV the
  dashboard joins on, seeded at deploy time through a Lambda-backed custom resource and refreshed by an
  EventBridge daily rule.
- The **agent lifecycle trigger** (default on): a third delivery source for `AGENT_METADATA_LOGS`
  into a small dedicated log group (`<LogGroupName>-agent-metadata`, identifiers and names only) and a
  subscription filter that runs the resolver whenever an agent is created, renamed or deleted — so a
  rename by an owner or co-owner is reflected on the dashboard within about a minute.

Conversation content — `user_message`, `system_text_message` and `feedback_details` — exists **only in
the CloudWatch log group**. The S3 copies omit those three fields at the source, and the dashboard's
Glue tables do not declare them either, so content cannot be read through this module even from a
full-content bucket. Correlation IDs are kept in the copy so an investigator can pivot from a Quick
table to the CloudWatch audit trail. The module creates no buckets of its own.

## Prerequisites

- The [Analytics Foundation](../governance-analytics-foundation/README.md) stack deployed in the same
  account and Region (the apply script resolves its bucket; `--foundation-stack` if you renamed it).
- Active Amazon Quick Enterprise or Professional subscription with AI features enabled.
- Deploy in the same AWS account and Region as Amazon Quick.
- AWS CLI v2 configured with deployment credentials.
- The deploying principal must have CloudFormation permissions for the resources in the template and
  `quicksight:AllowVendedLogDeliveryForResource` on:
  `arn:aws:quicksight:<region>:<account-id>:account/<account-id>`.
- Security administrators must restrict access to the log group and the dashboard. At minimum, review
  who can run CloudWatch Logs Insights queries, read log events, edit delivery configuration, manage
  the KMS key, and view the Quick dashboard's per-user tabs.

Cross-account delivery requires an additional destination policy and is outside this module's scope.

## Deploy

```bash
cd governance-chat-feedback-monitor/scripts
./apply-governance-chat-feedback-monitor.sh --region us-east-1
```

That deploys the module stack and then the Quick dashboard stack (`--skip-dashboard` for the module
alone). Use a named AWS profile or change retention:

```bash
./apply-governance-chat-feedback-monitor.sh \
  --region us-east-1 \
  --profile my-profile \
  --log-retention-days 90
```

KMS encryption is enabled by default. Disabling it is supported for constrained test environments but
is not recommended for production conversation data:

```bash
./apply-governance-chat-feedback-monitor.sh \
  --region us-east-1 \
  --enable-kms false
```

The wrapper passes `CAPABILITY_IAM` because the stack creates a least-privilege execution role for the
agent-name resolver Lambda. If deploying the template directly, include it and the foundation's bucket:

```bash
aws cloudformation deploy \
  --capabilities CAPABILITY_IAM \
  --template-file cloudformation/governance-chat-feedback-monitor.yaml \
  --stack-name quick-governance-chat-feedback-monitor \
  --parameter-overrides AnalyticsBucketName=<foundation analytics bucket> \
  --region us-east-1
```

The script discovers existing `CHAT_LOGS` and `FEEDBACK_LOGS` sources for the Quick account and reuses
them automatically (`--chat-source-name` and `--feedback-source-name` to select explicitly). During
stack creation, the resolver Lambda immediately exports the agent-name CSV; an EventBridge rule then
re-exports it from `ListAgents` every day at **06:00 GMT-3** (`09:00 UTC`) as a safety net, and the
agent lifecycle trigger (default on) re-exports it within about a minute of any agent being created,
renamed or deleted, so a rename by an owner or co-owner never shows stale. The script prints
`TailFeedCommand` and `S3DataLocation`, then the dashboard's `DashboardUrl`. After deployment, send a
new Quick message and optionally submit feedback; delivery can take a few minutes.

A delivery source can carry several S3 deliveries, so the minimized copy coexists with any full-content
S3 delivery created in the Quick console; retiring the latter is a privacy improvement worth considering.

If a previous create attempt reached `ROLLBACK_COMPLETE`, delete only that failed stack record before
retrying (its newly created resources have already rolled back):

```bash
aws cloudformation delete-stack \
  --stack-name quick-governance-chat-feedback-monitor \
  --region us-east-1
aws cloudformation wait stack-delete-complete \
  --stack-name quick-governance-chat-feedback-monitor \
  --region us-east-1
```

**Upgrading from a release with the CloudWatch dashboard.** Re-applying removes the CloudWatch dashboard
and its three metric filters, and the resolver stops maintaining the CloudWatch Logs lookup table
(`quick_agent_catalog` by default) — delete that table with `aws logs delete-lookup-table` once the
stack is updated. Log groups, deliveries and the KMS key are untouched.

## Verify delivery

```bash
aws logs describe-deliveries --region us-east-1
aws logs tail /aws/vendedlogs/quick/chat-feedback \
  --since 1h --follow --region us-east-1
aws s3 ls s3://<foundation analytics bucket>/chat-feedback/ --recursive | tail
```

A chat record should have `logType=CHAT_LOGS`; a rating record should have
`logType=FEEDBACK_LOGS`. Feedback is emitted only when a user submits a rating.

Verify the agent-name export or trigger an immediate refresh without waiting for the daily schedule:

```bash
aws s3 cp s3://<foundation analytics bucket>/chat-feedback/agent-names/agent-names.csv - | head

aws lambda invoke \
  --function-name quick-governance-chat-feedback-monitor-agent-name-resolver \
  --region us-east-1 \
  /tmp/agent-name-refresh.json
```

## Remove

Teardown deletes the log group and its conversation history, so the script requires explicit
confirmation. It removes the Quick dashboard stack first (`--keep-dashboard` to leave it):

```bash
cd governance-chat-feedback-monitor/scripts
./remove-governance-chat-feedback-monitor.sh \
  --region us-east-1 \
  --confirm-delete-data
```

If enabled, KMS schedules key deletion according to CloudFormation/KMS behavior and the key may remain
visible during its deletion window. The field-minimized copies under `chat-feedback/` in the shared
analytics bucket are not deleted; remove them, or the foundation itself, through
`governance-analytics-foundation/scripts`.

## Amazon Quick dashboard

`cloudformation/governance-chat-feedback-quick-dashboard.yaml` builds the dashboard over the
field-minimized S3 copy: Glue tables `chat_logs`, `feedback_logs` and `chat_agent_names` (none declares
a content column), two Direct Query datasets, and a five-tab administrator dashboard. The module's apply
script deploys it; on its own:

```bash
cd scripts
./apply-governance-chat-feedback-quick-dashboard.sh --region us-east-1
./remove-governance-chat-feedback-quick-dashboard.sh --region us-east-1   # data in S3 is untouched
```

| Tab | What it shows |
|---|---|
| Overview | Messages, conversations, active users, agents used, answered successfully, thumbs up / down, satisfaction; daily messages by status; daily feedback by type; top 10 agents; messages by surface and user type; how-to note with the sensitivity warning |
| Users | Per-user table (messages, conversations, agents used, answered %, flow runs, first / last activity), users × agents pivot, top 15 users |
| Agents | Per-agent usage, answered %, answers with citations, flow runs, average latency, last used; sentiment per agent (ratings joined to the rated message); messages by agent and data scope |
| Quality | Outcome, latency and satisfaction KPIs; feedback reasons; blocked and unanswered messages; negative feedback — each row with the conversation and message IDs to look the exchange up in CloudWatch Logs |
| Events | Message metadata and feedback events, newest first, exportable |

Agent friendly names come from the `agent_id,agent_name` CSV the resolver exports to
`chat-feedback/agent-names/` on every refresh (deploy, daily schedule, and every agent lifecycle event),
so a renamed agent shows its new name on the next dashboard load. Datasets are Direct Query: every load
reads S3 through Athena, so the dashboard is as fresh as the delivery. Per-user views are sensitive:
apply your access and employee-monitoring policies, and use Quick row-level security to restrict what
each viewer sees where needed.

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `ResourcePrefix` | `quick-governance-chat-feedback-monitor` | Prefix for named resources. |
| `AnalyticsBucketName` | *(required)* | The foundation's shared analytics bucket; objects land under `chat-feedback/chat/` and `chat-feedback/feedback/`, the agent-name CSV under `chat-feedback/agent-names/`. The apply script resolves it from the foundation stack. |
| `LogGroupName` | `/aws/vendedlogs/quick/chat-feedback` | Destination log group (the content audit trail). |
| `LogRetentionDays` | `90` | Retention for prompts, responses, and feedback. |
| `EnableKmsEncryption` | `true` | Creates and attaches a dedicated customer managed KMS key. |
| `CreateChatDeliverySource` | `true` | Direct-template option; set false to reuse a source. The script sets this automatically. |
| `ExistingChatDeliverySourceName` | empty | Existing `CHAT_LOGS` source when creation is false. |
| `CreateFeedbackDeliverySource` | `true` | Direct-template option; set false to reuse a source. The script sets this automatically. |
| `ExistingFeedbackDeliverySourceName` | empty | Existing `FEEDBACK_LOGS` source when creation is false. |
| `EnableAgentLifecycleTrigger` | `true` | Deliver `AGENT_METADATA_LOGS` (identifiers and names only) and refresh agent names on every create / rename / delete. Set `false` if another stack already owns a source for that log type. |

## Operational and security notes

- **Coverage:** temporary conversations excluded from Quick history and memory are still delivered to
  chat logs. This increases audit coverage but may surprise users.
- **Identity:** `user_arn` and `user_type` identify the initiating user. Apply your organization's
  access and employee-monitoring policies before exposing per-user views.
- **Correlation:** `conversation_id`, `user_message_id`, and `system_message_id` appear in both feeds
  and can correlate a rating to a chat interaction during investigations.
- **Agent attribution:** the Lambda paginates through the complete `ListAgents` result and fully
  rewrites the CSV on every run — daily, and within about a minute of any agent lifecycle event when the
  trigger is enabled. The 900-second timeout provides headroom for accounts with 100+ agents. Unknown
  IDs remain visible as IDs. `flow_id=-` denotes a regular chat rather than a Flow invocation.
- **Agent metadata log group:** holds one small record per agent operation (event name, agent ID,
  name, status, version, acting user); the fields Amazon Quick documents as sensitive —
  `instructions`, `custom_prompt_input`, `welcome_message`, `starter_prompts`, `magic_builder_query` —
  are excluded at the delivery. 30-day retention, encrypted with the module key when enabled.
- **Agent-name CSV:** contains only agent IDs and friendly names — no prompts, instructions, or chat
  content.
- **Feedback denominator:** absence of feedback does not mean neutral sentiment; it means no rating
  record was submitted. Interpret Useful/Not Useful counts alongside message volume.
- **Schema evolution:** group by documented fields rather than hard-coding all status values. New
  values can appear as Quick evolves.
- **Costs:** expect vended-log delivery (two destinations per feed), CloudWatch Logs ingestion and
  storage for the audit log group, S3 storage in cents, Athena per-query charges for dashboard loads,
  one brief Lambda invocation per day plus one per agent lifecycle event, and one customer managed KMS
  key when enabled. Rates vary by Region and usage; use AWS Pricing Calculator for a workload-specific
  estimate.

## AWS documentation

- [Monitoring Amazon Quick using CloudWatch Logs](https://docs.aws.amazon.com/quick/latest/userguide/monitoring-cloudwatch-logs.html) — delivery setup, regional/non-retroactive behavior, security guidance, and chat/feedback schemas.
- [Incident response, logging, and monitoring in Amazon Quick](https://docs.aws.amazon.com/quick/latest/userguide/incident-response-logging-and-monitoring.html) — when to use vended logs versus CloudTrail and the monitoring checklist.
