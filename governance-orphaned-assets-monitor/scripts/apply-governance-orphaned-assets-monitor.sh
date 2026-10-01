#!/usr/bin/env bash
#
# apply-governance-orphaned-assets-monitor.sh
#
# Thin wrapper around "sam build" + "sam deploy" for the Amazon Quick
# orphaned assets monitor stack
# (../cloudformation/governance-orphaned-assets-monitor.yaml).
#
# The SAM template is the single source of truth. sam build resolves
# ../lambda/requirements.txt (pins a boto3 recent enough for the
# Space/Agent/Topic-V2 permission APIs), then the stack is deployed
# idempotently and its outputs printed. Tear down with
# remove-governance-orphaned-assets-monitor.sh.
#
# The monitor is AUDIT_ONLY: read-only towards Quick, never transfers
# ownership, never deletes anything.
#
# Usage examples:
#
#   ./apply-governance-orphaned-assets-monitor.sh --region us-east-1
#
#   ./apply-governance-orphaned-assets-monitor.sh \
#       --region us-east-1 --profile my-profile \
#       --alert-email bi-admins@example.com
#
# Flags:
#   --region                  (required) AWS Region holding the Quick assets
#   --profile                 named AWS profile (else default credentials)
#   --stack-name              CloudFormation stack name
#   --resource-prefix         prefix for every AWS resource name (lowercase)
#   --alert-email             email for new HIGH-finding alerts (empty = no SNS)
#   --schedule                scan cadence (default 'cron(45 3 * * ? *)')
#   --identity-region         Quick identity region (empty = auto-discover)
#   --namespaces              comma-separated namespaces (default 'default')
#   --asset-types             comma-separated asset types (default: all 8)
#   --log-retention-days      CloudWatch Logs retention of the collector's own log group (default 90)
#   --foundation-stack        Analytics Foundation stack name (default quick-governance-analytics-foundation);
#                             its bucket receives snapshots and state under orphaned-assets/
#   --skip-dashboard          deploy the module stack only, not its Amazon Quick dashboard

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-orphaned-assets"
RESOURCE_PREFIX="quick-governance-orphaned-assets"
ALERT_EMAIL=""
SCHEDULE="cron(45 3 * * ? *)"
IDENTITY_REGION=""
NAMESPACES="default"
ASSET_TYPES="DATASET,DASHBOARD,ANALYSIS,DATA_SOURCE,FOLDER,SPACE,AGENT,TOPIC"
LOG_RETENTION_DAYS="90"
FOUNDATION_STACK="quick-governance-analytics-foundation"
SKIP_DASHBOARD="false"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-orphaned-assets-monitor.yaml"
DASHBOARD_SCRIPT="${SCRIPT_DIR}/apply-governance-orphaned-assets-quick-dashboard.sh"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[orphaned-assets] %s\n' "$*"; }

usage() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)                   REGION="$2";                   shift 2 ;;
    --profile)                  PROFILE="$2";                  shift 2 ;;
    --stack-name)               STACK_NAME="$2";               shift 2 ;;
    --resource-prefix)          RESOURCE_PREFIX="$2";          shift 2 ;;
    --alert-email)              ALERT_EMAIL="$2";              shift 2 ;;
    --schedule)                 SCHEDULE="$2";                 shift 2 ;;
    --identity-region)          IDENTITY_REGION="$2";          shift 2 ;;
    --namespaces)               NAMESPACES="$2";               shift 2 ;;
    --asset-types)              ASSET_TYPES="$2";              shift 2 ;;
    --log-retention-days)       LOG_RETENTION_DAYS="$2";       shift 2 ;;
    --foundation-stack)         FOUNDATION_STACK="$2";         shift 2 ;;
    --skip-dashboard)           SKIP_DASHBOARD="true";         shift ;;
    -h|--help)                  usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
[[ -f "$TEMPLATE" ]] || err "Cannot find template at $TEMPLATE"
[[ "$RESOURCE_PREFIX" =~ ^[a-z0-9-]{1,40}$ ]] \
  || err "--resource-prefix must be 1-40 chars: lowercase letters, digits, hyphens"
command -v sam >/dev/null 2>&1 || err "AWS SAM CLI not found in PATH"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- resolve the foundation ----------
ANALYTICS_BUCKET="$(aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$FOUNDATION_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='AnalyticsBucketName'].OutputValue" \
  --output text 2>/dev/null || true)"
[[ -n "$ANALYTICS_BUCKET" && "$ANALYTICS_BUCKET" != "None" ]] \
  || err "Foundation stack '$FOUNDATION_STACK' not found in $REGION - deploy governance-analytics-foundation first"

log "Deploying stack '$STACK_NAME' in $REGION${PROFILE:+ (profile: $PROFILE)}."
log "Schedule: $SCHEDULE | asset types: $ASSET_TYPES | snapshots and state -> s3://$ANALYTICS_BUCKET/orphaned-assets/"
[[ -n "$ALERT_EMAIL" ]] && log "Alerts will be emailed to: $ALERT_EMAIL (confirm the SNS subscription!)"

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
      "ScheduleExpression=\"$SCHEDULE\"" \
      "IdentityRegion=\"$IDENTITY_REGION\"" \
      "Namespaces=\"$NAMESPACES\"" \
      "AssetTypes=\"$ASSET_TYPES\"" \
      "LogRetentionDays=\"$LOG_RETENTION_DAYS\"" \
      "AnalyticsBucketName=\"$ANALYTICS_BUCKET\""

log "Stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
  --output text 2>/dev/null | sed 's/^/  /' || true

log "Done. Trigger a first scan now with the RunScanNowCommand output above,"
log "or wait for the schedule."

# ---------- Quick dashboard ----------
if [[ "$SKIP_DASHBOARD" == "true" ]]; then
  log "Module only (--skip-dashboard); the Quick dashboard was not deployed."
  exit 0
fi
[[ -x "$DASHBOARD_SCRIPT" ]] || err "Dashboard script not found or not executable: $DASHBOARD_SCRIPT"
log "Deploying the Amazon Quick dashboard."
"$DASHBOARD_SCRIPT" --region "$REGION" ${PROFILE:+--profile "$PROFILE"} --foundation-stack "$FOUNDATION_STACK"
