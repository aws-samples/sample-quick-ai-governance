# Amazon Quick — Chat and Feedback Monitor

Monitor **messages sent to Amazon Quick**, the users sending them, the agents and flows being used,
response outcomes, and **Useful / Not Useful** feedback on one CloudWatch dashboard.

The source is Amazon Quick's regional `CHAT_LOGS` and `FEEDBACK_LOGS` vended-log feeds. CloudTrail is
not the source for conversation content or feedback. The stack delivers both feeds to one CloudWatch
Logs log group, creates low-cardinality count metrics, and builds Logs Insights dashboard widgets.

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
│   └── governance-chat-feedback-monitor.yaml
└── scripts/
    ├── apply-governance-chat-feedback-monitor.sh
    └── remove-governance-chat-feedback-monitor.sh
```

## Architecture

```text
Amazon Quick (one account and Region)
  ├─ CHAT_LOGS delivery source ─────┐
  └─ FEEDBACK_LOGS delivery source ─┤
                                    v
                    CloudWatch Logs log group
                    /aws/vendedlogs/quick/chat-feedback
                    ├─ KMS encryption (default)
                    ├─ 30-day retention (default)
                    ├─ metric filters
                    └─ Logs Insights queries
                                    |
                 lookup agent_id at query time
                                    |
        Daily EventBridge rule -> 128 MB Lambda (900s timeout)
                                    |
                       ListAgents -> Logs lookup table
                                    |
                                    v
                    CloudWatch dashboard
```

Vended-log delivery is **regional and non-retroactive**. Deploy this module separately in every
Region where Amazon Quick activity occurs, and deploy it soon after enabling Quick AI features.

## Dashboard views

All widgets follow the dashboard time picker, which defaults to the last 30 days.

| View | What it answers |
|---|---|
| Messages | How many chat interactions occurred? |
| Useful / Not Useful | How many thumbs-up and thumbs-down ratings were submitted? |
| Users sending the most messages | Descending ranking by Quick `user_arn` and user type. |
| Most-used agents | Descending ranking by friendly name and message count, followed by `agent_id` for correlation; unknown IDs fall back to the ID. |
| Message status trend | How many requests succeeded, were blocked, or returned no answer? |
| Feedback trend and reasons | How is sentiment changing, and why are users dissatisfied? |
| Blocked and unanswered | Which prompts need investigation? |
| Latest messages and responses | Prompt/response audit table with correlation IDs. |
| Latest feedback | User, rating, reason, comments, and correlation IDs. |

CloudWatch Logs Insights caps a query result at 10,000 records. The two detail widgets therefore show
up to 10,000 events in the selected range. Narrow the time range or query the log group directly when
reviewing more events. Metric filters process only events ingested after stack creation; log widgets
show all retained events delivered after setup.

## What the stack creates

- One `AWS::Logs::LogGroup` for chat and feedback events.
- An optional dedicated `AWS::KMS::Key` and alias (enabled by default), with annual rotation.
- Up to two `AWS::Logs::DeliverySource` resources: `CHAT_LOGS` and `FEEDBACK_LOGS`. The deploy
  script reuses matching sources when they already exist because a Quick account/log-type pair is unique.
- Two CloudWatch Logs delivery destinations and deliveries. A reused source can continue delivering
  to existing S3 or Firehose destinations while also feeding this module's log group.
- Three metric filters: messages, Useful feedback, and Not Useful feedback.
- One minimal arm64 Lambda (128 MB, 900-second timeout), an EventBridge daily rule, and a
  CloudWatch Logs lookup table initialized through a Lambda-backed custom resource.
- One `AWS::CloudWatch::Dashboard` containing summary, trend, usage, and detail widgets.

It intentionally does **not** copy conversation content to S3 or another analytics system. That keeps
the data footprint small. Add a governed archival path separately if your retention or cross-Region
analytics requirements justify it.

## Prerequisites

- Active Amazon Quick Enterprise or Professional subscription with AI features enabled.
- Deploy in the same AWS account and Region as Amazon Quick.
- AWS CLI v2 configured with deployment credentials.
- The deploying principal must have CloudFormation permissions for the resources in the template and
  `quicksight:AllowVendedLogDeliveryForResource` on:
  `arn:aws:quicksight:<region>:<account-id>:account/<account-id>`.
- Security administrators must restrict access to the dashboard and log group. At minimum, review who
  can run CloudWatch Logs Insights queries, read log events, edit delivery configuration, and manage
  the KMS key.

Cross-account delivery requires an additional destination policy and is outside this module's scope.

## Deploy

```bash
cd governance-chat-feedback-monitor/scripts
./apply-governance-chat-feedback-monitor.sh --region us-east-1
```

Use a named AWS profile or change retention:

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
agent-name resolver Lambda. If deploying the template directly, include:

```bash
aws cloudformation deploy \
  --capabilities CAPABILITY_IAM \
  --template-file cloudformation/governance-chat-feedback-monitor.yaml \
  --stack-name quick-governance-chat-feedback-monitor \
  --region us-east-1
```

The script discovers existing `CHAT_LOGS` and `FEEDBACK_LOGS` sources for the Quick account and reuses
them automatically. During stack creation, the resolver Lambda immediately seeds the agent lookup table;
an EventBridge rule then replaces the table from `ListAgents` every day at **06:00 GMT-3**
(`09:00 UTC`). New or renamed agents are resolved on the next daily run, while unknown IDs remain
visible until then. You can explicitly select sources with `--chat-source-name` and `--feedback-source-name`. The script prints `DashboardUrl` and
`TailFeedCommand`. After deployment, send a new Quick message and optionally submit feedback; delivery
can take a few minutes.

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

## Verify delivery

```bash
aws logs describe-deliveries --region us-east-1
aws logs tail /aws/vendedlogs/quick/chat-feedback \
  --since 1h --follow --region us-east-1
```

A chat record should have `logType=CHAT_LOGS`; a rating record should have
`logType=FEEDBACK_LOGS`. Feedback is emitted only when a user submits a rating.

Verify the agent catalog or trigger an immediate refresh without waiting for the daily schedule:

```bash
aws logs describe-lookup-tables \
  --lookup-table-name-prefix quick_agent_catalog \
  --region us-east-1

aws lambda invoke \
  --function-name quick-governance-chat-feedback-monitor-agent-name-resolver \
  --region us-east-1 \
  /tmp/agent-name-refresh.json
```

## Remove

Teardown deletes the log group and its conversation history, so the script requires explicit
confirmation:

```bash
cd governance-chat-feedback-monitor/scripts
./remove-governance-chat-feedback-monitor.sh \
  --region us-east-1 \
  --confirm-delete-data
```

If enabled, KMS schedules key deletion according to CloudFormation/KMS behavior and the key may remain
visible during its deletion window.

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `ResourcePrefix` | `quick-governance-chat-feedback-monitor` | Prefix for named resources. |
| `LogGroupName` | `/aws/vendedlogs/quick/chat-feedback` | Destination log group. |
| `LogRetentionDays` | `90` | Retention for prompts, responses, and feedback. |
| `EnableKmsEncryption` | `true` | Creates and attaches a dedicated customer managed KMS key. |
| `AgentLookupTableName` | `quick_agent_catalog` | Regional Logs lookup table replaced from `ListAgents` daily. |
| `CreateChatDeliverySource` | `true` | Direct-template option; set false to reuse a source. The script sets this automatically. |
| `ExistingChatDeliverySourceName` | empty | Existing `CHAT_LOGS` source when creation is false. |
| `CreateFeedbackDeliverySource` | `true` | Direct-template option; set false to reuse a source. The script sets this automatically. |
| `ExistingFeedbackDeliverySourceName` | empty | Existing `FEEDBACK_LOGS` source when creation is false. |

## Operational and security notes

- **Coverage:** temporary conversations excluded from Quick history and memory are still delivered to
  chat logs. This increases audit coverage but may surprise users.
- **Identity:** `user_arn` and `user_type` identify the initiating user. Apply your organization's
  access and employee-monitoring policies before exposing per-user views.
- **Correlation:** `conversation_id`, `user_message_id`, and `system_message_id` appear in both feeds
  and can correlate a rating to a chat interaction during investigations.
- **Agent attribution:** the Lambda paginates through the complete `ListAgents` result and fully
  replaces the lookup table daily. The 900-second timeout provides headroom for accounts with 100+
  agents. Unknown IDs remain visible, and the ranking columns are friendly name, message count, then
  agent ID for correlation. `flow_id=-` denotes a regular chat rather than a Flow invocation.
- **Lookup-table security:** the catalog contains only agent IDs and friendly names and uses CloudWatch
  Logs' AWS-owned encryption key. It does not contain prompts, instructions, or chat content.
- **Feedback denominator:** absence of feedback does not mean neutral sentiment; it means no rating
  record was submitted. Interpret Useful/Not Useful counts alongside message volume.
- **Schema evolution:** group by documented fields rather than hard-coding all status values. New
  values can appear as Quick evolves.
- **Costs:** expect CloudWatch vended-log delivery/ingestion, storage, Logs Insights scan, three custom
  metrics, one brief Lambda invocation per day, and one customer managed KMS key when enabled. Rates
  vary by Region and usage; use AWS Pricing Calculator for a workload-specific estimate.

## AWS documentation

- [Monitoring Amazon Quick using CloudWatch Logs](https://docs.aws.amazon.com/quick/latest/userguide/monitoring-cloudwatch-logs.html) — delivery setup, regional/non-retroactive behavior, security guidance, and chat/feedback schemas.
- [Incident response, logging, and monitoring in Amazon Quick](https://docs.aws.amazon.com/quick/latest/userguide/incident-response-logging-and-monitoring.html) — when to use vended logs versus CloudTrail and the monitoring checklist.
- [CloudWatch Logs lookup](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/CWL_QuerySyntax-Lookup.html) — lookup-table enrichment and aggregation syntax.
