#!/usr/bin/env bash
#
# apply-governance-agent-hours-monitor.sh
#
# Deploys the Amazon Quick agent-hours usage monitor via CloudFormation:
#   * CloudWatch Logs log group for the AGENT_HOURS_LOGS feed
#   * One vended-log delivery source fanning out to:
#       - a CloudWatch Logs delivery  -> account/user dashboard
#       - an Amazon S3 delivery        -> Athena / Grafana / Datadog (optional)
#   * CloudWatch dashboard (Logs Insights: per-user, per-service, overage, totals)
#   * S3 bucket + bucket policy for the log-delivery service principal
#
# This is a thin wrapper around "aws cloudformation deploy". CloudFormation is
# used (rather than imperative CLI calls) because CreateDelivery is NOT
# idempotent -- re-running raw calls would create duplicate deliveries and
# double the per-GB delivery charge. The stack updates safely on re-run and
# tears down cleanly via remove-governance-agent-hours-monitor.sh.
#
# The Quick subscription must be in the SAME AWS account you deploy into. The
# template targets that account via the AWS::AccountId pseudo parameter,
# resolved from the credentials/profile you deploy with -- so use the profile
# for the account that hosts Quick (--profile, or the AWS_PROFILE env var).
#
# PREREQUISITES (not created here):
#   * Quick AI features enabled (Enterprise or Professional).
#   * The deploying principal must hold the IAM permission
#     quicksight:AllowVendedLogDeliveryForResource on the Quick account.
#
# Usage examples:
#
#   # Dashboard + S3 delivery (default credentials)
#   ./apply-governance-agent-hours-monitor.sh --region us-east-1
#
#   # Use a named credentials profile
#   ./apply-governance-agent-hours-monitor.sh --region us-east-1 --profile my-profile
#
#   # Dashboard only (skip the S3 bucket and S3 delivery)
#   ./apply-governance-agent-hours-monitor.sh --region us-east-1 --enable-s3 false
#
#   # Custom log group and retention
#   ./apply-governance-agent-hours-monitor.sh --region us-east-1 \
#       --log-group /aws/vendedlogs/quick/agent-hours \
#       --log-retention-days 30 --s3-expiration-days 730

set -euo pipefail

# ---------- defaults ----------
REGION=""
PROFILE=""
STACK_NAME="quick-governance-agent-hours-monitor"
PREFIX="quick-governance-agent-hours-monitor"
LOG_GROUP="/aws/vendedlogs/quick/agent-hours"
LOG_RETENTION_DAYS="90"
ENABLE_S3="true"
S3_EXPIRATION_DAYS="365"

# Resolve script dir so we can locate ../cloudformation regardless of cwd
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-agent-hours-monitor.yaml"

# ---------- helpers ----------
err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[agent-hours] %s\n' "$*"; }

usage() {
  # Print the leading comment block (line 2 to the first non-comment line),
  # stripping the leading "# ".
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)              REGION="$2";              shift 2 ;;
    --profile)             PROFILE="$2";             shift 2 ;;
    --stack-name)          STACK_NAME="$2";          shift 2 ;;
    --prefix)              PREFIX="$2";              shift 2 ;;
    --log-group)           LOG_GROUP="$2";           shift 2 ;;
    --log-retention-days)  LOG_RETENTION_DAYS="$2";  shift 2 ;;
    --enable-s3)           ENABLE_S3="$2";           shift 2 ;;
    --s3-expiration-days)  S3_EXPIRATION_DAYS="$2";  shift 2 ;;
    -h|--help)             usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"
[[ "$ENABLE_S3" == "true" || "$ENABLE_S3" == "false" ]] || err "--enable-s3 must be true or false"
[[ -f "$TEMPLATE" ]] || err "Cannot find template at $TEMPLATE"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

if [[ "$ENABLE_S3" == "true" && ! "$PREFIX" =~ ^[a-z0-9-]+$ ]]; then
  err "--prefix must be lowercase/DNS-safe when --enable-s3 is true (used in the bucket name)"
fi

# Route every aws call through the requested named profile, if any.
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- deploy ----------
log "Deploying stack '$STACK_NAME' in $REGION (S3 delivery: $ENABLE_S3${PROFILE:+, profile: $PROFILE})."
log "Reminder: the calling identity needs quicksight:AllowVendedLogDeliveryForResource,"
log "and the Quick subscription must be in this same AWS account."

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --no-fail-on-empty-changeset \
  --tags "Purpose=$PREFIX" "ManagedBy=CloudFormation" \
  --parameter-overrides \
      "ResourcePrefix=$PREFIX" \
      "LogGroupName=$LOG_GROUP" \
      "LogRetentionDays=$LOG_RETENTION_DAYS" \
      "EnableS3Delivery=$ENABLE_S3" \
      "S3ExpirationDays=$S3_EXPIRATION_DAYS"

# ---------- report outputs ----------
log "Stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
  --output text 2>/dev/null | sed 's/^/  /' || true

log "Done. Open the DashboardUrl above and set the time range to the current month."
log "It can take a few minutes for the first AGENT_HOURS_LOGS events to appear."
