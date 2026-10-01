#!/usr/bin/env bash
#
# apply-governance-orphaned-assets-quick-dashboard.sh
#
# Deploys the native Amazon Quick dashboard for the Orphaned Assets Monitor:
#   * Glue table(s) over the module's data in S3 (metadata columns only)
#   * Direct Query dataset(s) on the foundation's Athena data source
#   * the dashboard (control bar, tabs, exportable tables)
#
# Requires the governance analytics foundation stack in the same Region
# (governance-analytics-foundation, default stack name
# quick-governance-analytics-foundation). Unless overridden, the dashboard
# owner is the QuickPrincipalArn the foundation was deployed with, and the data
# is read from the shared analytics bucket under orphaned-assets/ownership/.
# Pass --data-bucket plus the prefix flag(s) to read a module-owned bucket.
#
# The template exceeds CloudFormation's inline size limit, so it is uploaded
# to the foundation's Athena query-results bucket (cfn-artifacts/) first.
#
# Usage examples:
#
#   # Shared analytics bucket, owner from the foundation stack
#   ./apply-governance-orphaned-assets-quick-dashboard.sh --region us-east-1
#
#   # Named profile and an explicit owner (Quick user or group ARN)
#   ./apply-governance-orphaned-assets-quick-dashboard.sh --region us-east-1 --profile my-profile \
#       --quick-principal-arn arn:aws:quicksight:sa-east-1:123456789012:group/default/bi-admins
#
#   # Module-owned bucket (deployments that predate the shared bucket)
#   ./apply-governance-orphaned-assets-quick-dashboard.sh --region us-east-1 \
#       --data-bucket quick-governance-orphaned-assets-<account-id> --snapshot-prefix snapshots/ownership/
#
#   # Create the change set but do not execute it (review first)
#   ./apply-governance-orphaned-assets-quick-dashboard.sh --region us-east-1 --no-execute

set -euo pipefail

# ---------- defaults ----------
REGION=""
PROFILE=""
STACK_NAME="quick-governance-orphaned-assets-quick-dashboard"
FOUNDATION_STACK="quick-governance-analytics-foundation"
QUICK_PRINCIPAL_ARN=""
DATA_BUCKET=""
SNAPSHOT_PREFIX="orphaned-assets/ownership/"
NO_EXECUTE="false"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-orphaned-assets-quick-dashboard.yaml"

# ---------- helpers ----------
err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[orphaned-assets-quick] %s\n' "$*"; }

usage() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)               REGION="$2";               shift 2 ;;
    --profile)              PROFILE="$2";              shift 2 ;;
    --stack-name)           STACK_NAME="$2";           shift 2 ;;
    --foundation-stack)     FOUNDATION_STACK="$2";     shift 2 ;;
    --quick-principal-arn)  QUICK_PRINCIPAL_ARN="$2";  shift 2 ;;
    --data-bucket)          DATA_BUCKET="$2";          shift 2 ;;
    --snapshot-prefix)  SNAPSHOT_PREFIX="$2";     shift 2 ;;
    --no-execute)           NO_EXECUTE="true";         shift ;;
    -h|--help)              usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"
[[ -z "$DATA_BUCKET" || "$DATA_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || err "--data-bucket must be a valid S3 bucket name"
[[ -z "$QUICK_PRINCIPAL_ARN" || "$QUICK_PRINCIPAL_ARN" =~ ^arn:aws[a-z-]*:quicksight:[a-z0-9-]+:[0-9]{12}:(user|group)/.+$ ]] \
  || err "--quick-principal-arn must be a Quick user or group ARN"
[[ -f "$TEMPLATE" ]] || err "Cannot find template at $TEMPLATE"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- resolve the foundation (owner, artifact bucket) ----------
FOUNDATION_JSON=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$FOUNDATION_STACK" \
  --query 'Stacks[0]' --output json 2>/dev/null) \
  || err "Foundation stack '$FOUNDATION_STACK' not found in $REGION - deploy governance-analytics-foundation first"

if [[ -z "$QUICK_PRINCIPAL_ARN" ]]; then
  QUICK_PRINCIPAL_ARN=$(printf '%s' "$FOUNDATION_JSON" | python3 -c \
    'import json,sys; s=json.load(sys.stdin); print(next(p["ParameterValue"] for p in s["Parameters"] if p["ParameterKey"]=="QuickPrincipalArn"))')
  log "Dashboard owner: QuickPrincipalArn of stack '$FOUNDATION_STACK' (override with --quick-principal-arn)."
fi
ARTIFACT_BUCKET=$(printf '%s' "$FOUNDATION_JSON" | python3 -c \
  'import json,sys; s=json.load(sys.stdin); print(next(o["OutputValue"] for o in s["Outputs"] if o["OutputKey"]=="QueryResultsBucketName"))')

# ---------- deploy ----------
if [[ -n "$DATA_BUCKET" ]]; then
  log "Deploying stack '$STACK_NAME' in $REGION reading module-owned bucket $DATA_BUCKET${PROFILE:+ (profile: $PROFILE)}."
else
  log "Deploying stack '$STACK_NAME' in $REGION reading the shared analytics bucket${PROFILE:+ (profile: $PROFILE)}."
fi

EXECUTE_ARGS=()
if [[ "$NO_EXECUTE" == "true" ]]; then
  EXECUTE_ARGS=(--no-execute-changeset)
  log "Change set only (--no-execute): review it, then run 'aws cloudformation execute-change-set --change-set-name <arn>'."
fi

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --s3-prefix cfn-artifacts \
  --no-fail-on-empty-changeset \
  ${EXECUTE_ARGS[@]+"${EXECUTE_ARGS[@]}"} \
  --tags "Purpose=quick-governance-orphaned-assets" "ManagedBy=CloudFormation" \
  --parameter-overrides \
      "FoundationStackName=$FOUNDATION_STACK" \
      "QuickPrincipalArn=$QUICK_PRINCIPAL_ARN" \
      "DataBucketName=$DATA_BUCKET" \
      "SnapshotPrefix=$SNAPSHOT_PREFIX"

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

log "Done. Open the DashboardUrl above in Amazon Quick."
