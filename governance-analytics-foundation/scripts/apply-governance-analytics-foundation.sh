#!/usr/bin/env bash
#
# apply-governance-analytics-foundation.sh
#
# Deploys the Amazon Quick governance analytics foundation via CloudFormation:
#   * Shared analytics S3 bucket (per-module prefixes) + delivery bucket policy
#   * Athena workgroup + query-results bucket
#   * Glue database for the module tables
#   * Amazon Quick data source (Athena) owned by --quick-principal-arn
#
# Deploy once per Region BEFORE the monitoring modules (Agent Hours, Chat and
# Feedback, Dataset Lifecycle, Orphaned Assets); Spreadsheet File Rename does
# not need this stack.
#
# No console step is needed by default: the stack attaches a scoped policy to
# Quick's service role (--quick-service-role) so Quick can read the analytics
# bucket, use the workgroup and read the Glue database. Only when that grant is
# skipped (--quick-service-role "" -- Lake Formation accounts or a custom Quick
# role) tick the analytics bucket on Manage Quick -> Security & permissions ->
# AWS resources, as described in the module README.
#
# Usage examples:
#
#   ./apply-governance-analytics-foundation.sh --region us-east-1 \
#       --quick-principal-arn arn:aws:quicksight:sa-east-1:123456789012:user/default/admin
#
#   # Named profile, custom Glue database name
#   ./apply-governance-analytics-foundation.sh --region us-east-1 --profile my-profile \
#       --quick-principal-arn <arn> --glue-database quick_governance
#
#   # Skip the service-role grant (Lake Formation accounts or a custom Quick role)
#   ./apply-governance-analytics-foundation.sh --region us-east-1 --quick-principal-arn <arn> --quick-service-role ""
#
#   # Create the change set but do not execute it (review first)
#   ./apply-governance-analytics-foundation.sh --region us-east-1 --quick-principal-arn <arn> --no-execute

set -euo pipefail

# ---------- defaults ----------
REGION=""
PROFILE=""
STACK_NAME="quick-governance-analytics-foundation"
PREFIX="quick-governance-analytics"
GLUE_DATABASE="quick_governance"
ATHENA_WORKGROUP="quick-governance"
QUICK_PRINCIPAL_ARN=""
RESULTS_EXPIRATION_DAYS="7"
ANALYTICS_EXPIRATION_DAYS="365"
QUICK_SERVICE_ROLE="aws-quicksight-service-role-v0"
NO_EXECUTE="false"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-analytics-foundation.yaml"

# ---------- helpers ----------
err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[analytics-foundation] %s\n' "$*"; }

usage() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)                     REGION="$2";                    shift 2 ;;
    --profile)                    PROFILE="$2";                   shift 2 ;;
    --stack-name)                 STACK_NAME="$2";                shift 2 ;;
    --prefix)                     PREFIX="$2";                    shift 2 ;;
    --glue-database)              GLUE_DATABASE="$2";             shift 2 ;;
    --athena-workgroup)           ATHENA_WORKGROUP="$2";          shift 2 ;;
    --quick-principal-arn)        QUICK_PRINCIPAL_ARN="$2";       shift 2 ;;
    --results-expiration-days)    RESULTS_EXPIRATION_DAYS="$2";   shift 2 ;;
    --analytics-expiration-days)  ANALYTICS_EXPIRATION_DAYS="$2"; shift 2 ;;
    --quick-service-role)         QUICK_SERVICE_ROLE="$2";         shift 2 ;;
    --no-execute)                 NO_EXECUTE="true";              shift ;;
    -h|--help)                    usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"
[[ -n "$QUICK_PRINCIPAL_ARN" ]] || err "--quick-principal-arn is required (Quick user or group ARN, identity Region)"
[[ "$QUICK_PRINCIPAL_ARN" =~ ^arn:aws[a-z-]*:quicksight:[a-z0-9-]+:[0-9]{12}:(user|group)/.+$ ]] \
  || err "--quick-principal-arn must be a Quick user or group ARN"
[[ "$PREFIX" =~ ^[a-z0-9-]{1,32}$ ]] || err "--prefix must be 1-32 lowercase letters, digits, hyphens"
[[ "$GLUE_DATABASE" =~ ^[a-z0-9_]{1,64}$ ]] || err "--glue-database must be lowercase letters, digits, underscores"
[[ -f "$TEMPLATE" ]] || err "Cannot find template at $TEMPLATE"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- deploy ----------
log "Deploying stack '$STACK_NAME' in $REGION${PROFILE:+ (profile: $PROFILE)}."

EXECUTE_ARGS=()
if [[ "$NO_EXECUTE" == "true" ]]; then
  EXECUTE_ARGS=(--no-execute-changeset)
  log "Change set only (--no-execute): review it, then run 'aws cloudformation execute-change-set --change-set-name <arn>'."
fi

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  ${EXECUTE_ARGS[@]+"${EXECUTE_ARGS[@]}"} \
  --tags "Purpose=$PREFIX" "ManagedBy=CloudFormation" \
  --parameter-overrides \
      "ResourcePrefix=$PREFIX" \
      "GlueDatabaseName=$GLUE_DATABASE" \
      "AthenaWorkGroupName=$ATHENA_WORKGROUP" \
      "QuickPrincipalArn=$QUICK_PRINCIPAL_ARN" \
      "QueryResultsExpirationDays=$RESULTS_EXPIRATION_DAYS" \
      "AnalyticsExpirationDays=$ANALYTICS_EXPIRATION_DAYS" \
      "QuickServiceRoleName=$QUICK_SERVICE_ROLE"

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

log "Done. Check the ManualStep output: with the default --quick-service-role no console step is needed."
