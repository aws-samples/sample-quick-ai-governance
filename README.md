# Amazon Quick — Best Practices

Deployable, best-practice reference modules for **Amazon Quick** — governance guardrails, usage
monitoring, identity integration, and demos that Solutions Architects and customers can apply to a
production Quick deployment. Each module is self-contained (its own CloudFormation/SAM template,
helper scripts, and README), so you can adopt only what you need.

## Modules

| Module | What it does | Deploy with |
|---|---|---|
| [Agent Hours Usage Monitor](./governance-agent-hours-monitor/README.md) | Track Amazon Quick agent-hours usage — total, per user, per feature, and license-covered vs. chargeable — on a CloudWatch dashboard, with an optional Amazon S3 feed. | CloudFormation |
| [Chat and Feedback Monitor](./governance-chat-feedback-monitor/README.md) | Monitor Quick messages and responses, users, agent and flow usage, response outcomes, and Useful / Not Useful feedback, with daily agent friendly-name resolution, on an encrypted CloudWatch dashboard. | CloudFormation |
| [Spreadsheet File Rename](./governance-spreadsheet-file-rename/README.md) | Auto-prefix the name of any dataset created from an uploaded spreadsheet (`.xlsx`) with `xls-`, via an EventBridge → Lambda automation. | AWS SAM |
| [Block Sharing](./governance-block-sharing/README.md) | Block users from sharing Chat Agents, Spaces, and Datasets (extendable to dashboards, analyses, data sources) using a Quick custom permissions profile applied at account, role, or user scope. | CloudFormation + CLI |

Each module's README covers prerequisites, parameters, deploy/remove commands, cost, and teardown.
Modules are published as they're completed; more will be added to this table.
