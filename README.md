# Amazon Quick — Best Practices

Deployable, best-practice reference modules for **Amazon Quick** — governance guardrails, usage
monitoring, identity integration, and demos that Solutions Architects and customers can apply to a
production Quick deployment. Each module is self-contained (its own CloudFormation template, helper
scripts, and README), so you can adopt only what you need.

> **Status:** This repository is being published one module at a time. The **Agent Hours Usage
> Monitor** below is complete and ready to use. Additional modules are in progress and will be
> listed here as each one is finished.

## Modules

### Agent Hours Usage Monitor

Track Amazon Quick **agent-hours usage** — total, per user, per feature, and license-covered vs.
chargeable — on a CloudWatch dashboard, with an optional Amazon S3 feed for downstream analysis
(Athena, Grafana, Datadog). Everything is provisioned by a single CloudFormation stack, wrapped by
`apply` / `remove` helper scripts.

| Resource | Path |
|---|---|
| Module README (full documentation) | [`governance-agent-hours-monitor/README.md`](./governance-agent-hours-monitor/README.md) |
| CloudFormation template | [`governance-agent-hours-monitor/cloudformation/governance-agent-hours-monitor.yaml`](./governance-agent-hours-monitor/cloudformation/governance-agent-hours-monitor.yaml) |
| Deploy script | [`governance-agent-hours-monitor/scripts/apply-governance-agent-hours-monitor.sh`](./governance-agent-hours-monitor/scripts/apply-governance-agent-hours-monitor.sh) |
| Remove script | [`governance-agent-hours-monitor/scripts/remove-governance-agent-hours-monitor.sh`](./governance-agent-hours-monitor/scripts/remove-governance-agent-hours-monitor.sh) |

Quick start:

```bash
cd governance-agent-hours-monitor/scripts
./apply-governance-agent-hours-monitor.sh --region us-east-1
```

See the [module README](./governance-agent-hours-monitor/README.md) for prerequisites, parameters,
dashboard views, cost, and teardown.
