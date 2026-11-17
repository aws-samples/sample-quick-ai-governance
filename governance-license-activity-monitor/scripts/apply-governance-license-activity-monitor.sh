#!/usr/bin/env bash
#
# apply-governance-license-activity-monitor.sh
#
# Deploys the License and Activity Monitor, then its AWS Quick dashboard.
#
# Usage:
#   ./apply-governance-license-activity-monitor.sh --region us-east-1 --profile default
#   ./apply-governance-license-activity-monitor.sh --region us-east-1 --profile default --no-execute
#   ./apply-governance-license-activity-monitor.sh --region us-east-1 --profile default --skip-dashboard
#
# Flags:
#   --region               (required) Region to deploy into
#   --profile              named AWS profile
#   --stack-name           default quick-governance-license-activity
#   --foundation-stack     default quick-governance-analytics-foundation
#   --unused-threshold-days  default 60
#   --skip-dashboard       module stack only
#   --no-execute           create the change set and stop

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-license-activity"
RESOURCE_PREFIX="quick-governance-license-activity"
FOUNDATION_STACK="quick-governance-analytics-foundation"
UNUSED_THRESHOLD_DAYS="60"
SKIP_DASHBOARD="false"
NO_EXECUTE="false"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-license-activity-monitor.yaml"
DASHBOARD_SCRIPT="${SCRIPT_DIR}/apply-governance-license-activity-quick-dashboard.sh"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[license-activity] %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --foundation-stack) FOUNDATION_STACK="$2"; shift 2 ;;
    --unused-threshold-days) UNUSED_THRESHOLD_DAYS="$2"; shift 2 ;;
    --skip-dashboard) SKIP_DASHBOARD="true"; shift ;;
    --no-execute) NO_EXECUTE="true"; shift ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

ANALYTICS_BUCKET="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$FOUNDATION_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='AnalyticsBucketName'].OutputValue" --output text)"
[[ -n "$ANALYTICS_BUCKET" && "$ANALYTICS_BUCKET" != "None" ]] || err "Foundation stack '$FOUNDATION_STACK' was not found in $REGION"

BUILD_DIR="${SCRIPT_DIR}/../.aws-sam/build"
sam build --template-file "$TEMPLATE" --build-dir "$BUILD_DIR" > /dev/null

EXECUTE_ARGS=()
if [[ "$NO_EXECUTE" == "true" ]]; then
  EXECUTE_ARGS=(--no-execute-changeset)
  log "Change set only (--no-execute)."
fi

sam deploy \
  --template-file "$BUILD_DIR/template.yaml" \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --resolve-s3 \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  ${EXECUTE_ARGS[@]+"${EXECUTE_ARGS[@]}"} \
  --tags "Purpose=$RESOURCE_PREFIX" "ManagedBy=SAM" \
  --parameter-overrides \
      "ResourcePrefix=$RESOURCE_PREFIX" \
      "UnusedThresholdDays=$UNUSED_THRESHOLD_DAYS" \
      "AnalyticsBucketName=$ANALYTICS_BUCKET"

if [[ "$NO_EXECUTE" == "true" ]]; then
  log "Change set created, nothing executed."
  exit 0
fi

aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text

if [[ "$SKIP_DASHBOARD" == "true" ]]; then
  log "Module only. The dashboard was not deployed."
  exit 0
fi
"$DASHBOARD_SCRIPT" --region "$REGION" ${PROFILE:+--profile "$PROFILE"} --foundation-stack "$FOUNDATION_STACK"
