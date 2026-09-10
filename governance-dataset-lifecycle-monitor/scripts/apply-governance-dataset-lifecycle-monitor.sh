#!/usr/bin/env bash
#
# apply-governance-dataset-lifecycle-monitor.sh
#
# Thin wrapper around "sam build" + "sam deploy" for the Amazon Quick dataset lifecycle
# monitor stack (../cloudformation/governance-dataset-lifecycle-monitor.yaml).
#
# The SAM template is the single source of truth. This script packages
# ../lambda/collector.py, deploys/updates the stack idempotently, then prints
# the stack outputs. Tear it down with
# remove-governance-dataset-lifecycle-monitor.sh.
#
# The stack deploys into the account resolved from your credentials/profile
# and targets it via AWS::AccountId -- there is no account ID to pass. The
# Amazon Quick subscription must be in that same account/Region.
#
# What the stack creates (all named ${ResourcePrefix}-*):
#   * Collector Lambda (Python 3.14, arm64) on two EventBridge schedules
#     (fast: sync health / slow: usage + lineage + capacity)
#   * CloudWatch Logs data log group (one JSON event per dataset per run)
#   * CloudWatch dashboard (fleet health, durations, last-used, cost signal)
#   * Low-cardinality KPI metrics (QuickGovernance/DatasetLifecycle)
#   * S3 state/snapshot bucket (retained on stack delete)
#   * Native ingestion-failure alarm + collector error alarm
#   * Optional SNS topic + email subscription for failure alerts
#
# Prerequisites:
#   * AWS SAM CLI installed
#     https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html
#   * AWS CLI v2 authenticated (used here to print stack outputs)
#   * "--resolve-s3" lets SAM manage the deployment-artifact bucket for you.
#
# Usage examples:
#
#   # Minimum (uses default credentials)
#   ./apply-governance-dataset-lifecycle-monitor.sh --region us-east-1
#
#   # With email alerts and a custom unused threshold
#   ./apply-governance-dataset-lifecycle-monitor.sh \
#       --region us-east-1 --profile my-profile \
#       --alert-email bi-admins@example.com --unused-threshold-days 60
#
#   # Cheaper cadence for a small account: fast loop every 6 hours
#   ./apply-governance-dataset-lifecycle-monitor.sh \
#       --region us-east-1 --fast-schedule 'rate(6 hours)'
#
# Flags:
#   --region                  (required) AWS Region to deploy into
#   --profile                 named AWS profile (else default credentials)
#   --stack-name              CloudFormation stack name
#   --resource-prefix         prefix for every AWS resource name (lowercase)
#   --alert-email             email for SNS alerts (empty = no SNS)
#   --fast-schedule           sync-health schedule (default 'rate(1 hour)')
#   --slow-schedule           usage/capacity schedule (default 'cron(15 3 * * ? *)')
#   --disable-cloudtrail      skip CloudTrail Event history evidence
#   --chat-log-group          CHAT_LOGS log group scanned for agent-citation
#                             usage evidence (default
#                             '/aws/vendedlogs/quick/chat-feedback', the Chat
#                             and Feedback Monitor default; '' disables)
#   --spice-rate              USD per SPICE GB-month for the estimate (default 0.38)
#   --unused-threshold-days   no-observed-use threshold (default 30)
#   --log-retention-days      CloudWatch Logs retention (default 90)
#   --snapshot-expiration-days  S3 snapshot expiry (default 180)

set -euo pipefail

# ---------- defaults ----------
REGION=""
PROFILE=""
STACK_NAME="quick-governance-dataset-lifecycle"
RESOURCE_PREFIX="quick-governance-dataset-lifecycle"
ALERT_EMAIL=""
FAST_SCHEDULE="rate(1 hour)"
SLOW_SCHEDULE="cron(15 3 * * ? *)"
ENABLE_CLOUDTRAIL="true"
CHAT_LOG_GROUP="/aws/vendedlogs/quick/chat-feedback"
SPICE_RATE="0.38"
UNUSED_THRESHOLD_DAYS="30"
LOG_RETENTION_DAYS="90"
SNAPSHOT_EXPIRATION_DAYS="180"

# Resolve script dir so we can locate ../cloudformation regardless of cwd
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-dataset-lifecycle-monitor.yaml"

# ---------- helpers ----------
err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[dataset-lifecycle] %s\n' "$*"; }

usage() {
  # Print the leading comment block (line 2 to the first non-comment line),
  # stripping the leading "# ".
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)                   REGION="$2";                   shift 2 ;;
    --profile)                  PROFILE="$2";                  shift 2 ;;
    --stack-name)               STACK_NAME="$2";               shift 2 ;;
    --resource-prefix)          RESOURCE_PREFIX="$2";          shift 2 ;;
    --alert-email)              ALERT_EMAIL="$2";              shift 2 ;;
    --fast-schedule)            FAST_SCHEDULE="$2";            shift 2 ;;
    --slow-schedule)            SLOW_SCHEDULE="$2";            shift 2 ;;
    --disable-cloudtrail)       ENABLE_CLOUDTRAIL="false";     shift ;;
    --chat-log-group)           CHAT_LOG_GROUP="$2";           shift 2 ;;
    --spice-rate)               SPICE_RATE="$2";               shift 2 ;;
    --unused-threshold-days)    UNUSED_THRESHOLD_DAYS="$2";    shift 2 ;;
    --log-retention-days)       LOG_RETENTION_DAYS="$2";       shift 2 ;;
    --snapshot-expiration-days) SNAPSHOT_EXPIRATION_DAYS="$2"; shift 2 ;;
    -h|--help)                  usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"
[[ -f "$TEMPLATE" ]] || err "Cannot find template at $TEMPLATE"
[[ "$RESOURCE_PREFIX" =~ ^[a-z0-9-]{1,40}$ ]] \
  || err "--resource-prefix must be 1-40 chars: lowercase letters, digits, hyphens"
command -v sam >/dev/null 2>&1 || err "AWS SAM CLI not found in PATH (see https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

# Route every sam/aws call through the requested named profile, if any.
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- build + deploy ----------
log "Deploying stack '$STACK_NAME' in $REGION${PROFILE:+ (profile: $PROFILE)}."
log "Fast loop: $FAST_SCHEDULE | slow loop: $SLOW_SCHEDULE | CloudTrail evidence: $ENABLE_CLOUDTRAIL"
[[ -n "$ALERT_EMAIL" ]] && log "Alerts will be emailed to: $ALERT_EMAIL (confirm the SNS subscription!)"

# sam build resolves lambda/requirements.txt (bundles a boto3 recent enough
# for the Quick Space/Agent lineage APIs) into .aws-sam/build.
BUILD_DIR="${SCRIPT_DIR}/../.aws-sam/build"
log "Building (sam build; bundles pinned boto3 from lambda/requirements.txt)..."
sam build \
  --template-file "$TEMPLATE" \
  --build-dir "$BUILD_DIR" > /dev/null

sam deploy \
  --template-file "$BUILD_DIR/template.yaml" \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --resolve-s3 \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --tags "Purpose=$RESOURCE_PREFIX" "ManagedBy=SAM" \
  --parameter-overrides \
      "ResourcePrefix=\"$RESOURCE_PREFIX\"" \
      "AlertEmail=\"$ALERT_EMAIL\"" \
      "FastLoopSchedule=\"$FAST_SCHEDULE\"" \
      "SlowLoopSchedule=\"$SLOW_SCHEDULE\"" \
      "EnableCloudTrailEvidence=\"$ENABLE_CLOUDTRAIL\"" \
      "ChatLogGroupName=\"$CHAT_LOG_GROUP\"" \
      "SpiceRatePerGBMonth=\"$SPICE_RATE\"" \
      "UnusedThresholdDays=\"$UNUSED_THRESHOLD_DAYS\"" \
      "LogRetentionDays=\"$LOG_RETENTION_DAYS\"" \
      "SnapshotExpirationDays=\"$SNAPSHOT_EXPIRATION_DAYS\""

# ---------- report outputs ----------
log "Stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
  --output text 2>/dev/null | sed 's/^/  /' || true

log "Done. Trigger a first scan now with the RunFastLoopNowCommand /"
log "RunSlowLoopNowCommand outputs above, or wait for the schedules."
