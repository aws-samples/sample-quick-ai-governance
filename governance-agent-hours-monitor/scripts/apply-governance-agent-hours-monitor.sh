#!/usr/bin/env bash
#
# apply-governance-agent-hours-monitor.sh
#
# Deploys the Amazon Quick agent-hours usage monitor:
#   1. the module stack - one vended-log delivery of AGENT_HOURS_LOGS into the
#      shared analytics bucket (agent-hours/ prefix) created by the Analytics
#      Foundation stack;
#   2. the module's Amazon Quick dashboard stack (Glue table, datasets, dashboard),
#      via apply-governance-agent-hours-quick-dashboard.sh - skip with --skip-dashboard.
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
#   * The Analytics Foundation stack in this Region
#     (../../governance-analytics-foundation/scripts/apply-governance-analytics-foundation.sh).
#   * Quick AI features enabled (Enterprise or Professional).
#   * The deploying principal must hold the IAM permission
#     quicksight:AllowVendedLogDeliveryForResource on the Quick account.
#
# Usage examples:
#
#   # Module + Quick dashboard (default credentials)
#   ./apply-governance-agent-hours-monitor.sh --region us-east-1
#
#   # Use a named credentials profile
#   ./apply-governance-agent-hours-monitor.sh --region us-east-1 --profile my-profile
#
#   # Module only - data in S3 for your own tools, no Quick dashboard
#   ./apply-governance-agent-hours-monitor.sh --region us-east-1 --skip-dashboard
#
#   # Keep the legacy AWSLogs/<account-id>/... S3 layout (existing Athena tables)
#   ./apply-governance-agent-hours-monitor.sh --region us-east-1 --hive-path false
#
#   # Foundation deployed under a non-default stack name
#   ./apply-governance-agent-hours-monitor.sh --region us-east-1 --foundation-stack my-foundation
#
#   # Create the change set but do not execute it (review first, then run
#   # "aws cloudformation execute-change-set --change-set-name <arn>")
#   ./apply-governance-agent-hours-monitor.sh --region us-east-1 --no-execute

set -euo pipefail

# ---------- defaults ----------
REGION=""
PROFILE=""
STACK_NAME="quick-governance-agent-hours-monitor"
PREFIX="quick-governance-agent-hours-monitor"
FOUNDATION_STACK="quick-governance-analytics-foundation"
HIVE_PATH="true"
NO_EXECUTE="false"
SKIP_DASHBOARD="false"

# Resolve script dir so we can locate ../cloudformation regardless of cwd
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-agent-hours-monitor.yaml"
DASHBOARD_SCRIPT="${SCRIPT_DIR}/apply-governance-agent-hours-quick-dashboard.sh"

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
    --region)            REGION="$2";            shift 2 ;;
    --profile)           PROFILE="$2";           shift 2 ;;
    --stack-name)        STACK_NAME="$2";        shift 2 ;;
    --prefix)            PREFIX="$2";            shift 2 ;;
    --foundation-stack)  FOUNDATION_STACK="$2";  shift 2 ;;
    --hive-path)         HIVE_PATH="$2";         shift 2 ;;
    --skip-dashboard)    SKIP_DASHBOARD="true";  shift ;;
    --no-execute)        NO_EXECUTE="true";      shift ;;
    -h|--help)           usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"
[[ "$HIVE_PATH" == "true" || "$HIVE_PATH" == "false" ]] || err "--hive-path must be true or false"
[[ -f "$TEMPLATE" ]] || err "Cannot find template at $TEMPLATE"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

# Route every aws call through the requested named profile, if any.
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- resolve the foundation ----------
ANALYTICS_BUCKET="$(aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$FOUNDATION_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='AnalyticsBucketName'].OutputValue" \
  --output text 2>/dev/null || true)"
[[ -n "$ANALYTICS_BUCKET" && "$ANALYTICS_BUCKET" != "None" ]] \
  || err "Foundation stack '$FOUNDATION_STACK' not found in $REGION - deploy governance-analytics-foundation first"

# ---------- deploy ----------
log "Deploying stack '$STACK_NAME' in $REGION -> s3://$ANALYTICS_BUCKET/agent-hours/${PROFILE:+ (profile: $PROFILE)}."
log "Reminder: the calling identity needs quicksight:AllowVendedLogDeliveryForResource,"
log "and the Quick subscription must be in this same AWS account."

EXECUTE_ARGS=()
if [[ "$NO_EXECUTE" == "true" ]]; then
  EXECUTE_ARGS=(--no-execute-changeset)
  log "Change set only (--no-execute): review it, then run 'aws cloudformation execute-change-set --change-set-name <arn>'."
fi

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --no-fail-on-empty-changeset \
  ${EXECUTE_ARGS[@]+"${EXECUTE_ARGS[@]}"} \
  --tags "Purpose=$PREFIX" "ManagedBy=CloudFormation" \
  --parameter-overrides \
      "ResourcePrefix=$PREFIX" \
      "AnalyticsBucketName=$ANALYTICS_BUCKET" \
      "S3HiveCompatiblePath=$HIVE_PATH"

if [[ "$NO_EXECUTE" == "true" ]]; then
  log "Change set created, nothing executed."
  exit 0
fi

# ---------- report outputs ----------
log "Stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
  --output text 2>/dev/null | sed 's/^/  /' || true
log "It can take a few minutes for the first AGENT_HOURS_LOGS events to appear."

# ---------- Quick dashboard ----------
if [[ "$SKIP_DASHBOARD" == "true" ]]; then
  log "Done (--skip-dashboard). Point your analytics tool at the S3DataLocation above."
  exit 0
fi
[[ -x "$DASHBOARD_SCRIPT" ]] || err "Dashboard script not found or not executable: $DASHBOARD_SCRIPT"
log "Deploying the Amazon Quick dashboard."
"$DASHBOARD_SCRIPT" --region "$REGION" ${PROFILE:+--profile "$PROFILE"} --foundation-stack "$FOUNDATION_STACK"
