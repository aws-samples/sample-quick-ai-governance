#!/usr/bin/env bash
#
# apply-governance-license-activity-quick-dashboard.sh
#
# Deploys the License and Activity dashboard. The template is uploaded to the
# foundation query-results bucket because it is larger than the inline limit.
#
# Usage:
#   ./apply-governance-license-activity-quick-dashboard.sh --region us-east-1 --profile default
#   ./apply-governance-license-activity-quick-dashboard.sh --region us-east-1 --profile default --no-execute

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-license-activity-quick-dashboard"
FOUNDATION_STACK="quick-governance-analytics-foundation"
QUICK_PRINCIPAL_ARN=""
NO_EXECUTE="false"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-license-activity-quick-dashboard.yaml"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[license-activity-quick] %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --foundation-stack) FOUNDATION_STACK="$2"; shift 2 ;;
    --quick-principal-arn) QUICK_PRINCIPAL_ARN="$2"; shift 2 ;;
    --no-execute) NO_EXECUTE="true"; shift ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

FOUNDATION_JSON=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$FOUNDATION_STACK" --query 'Stacks[0]' --output json)
if [[ -z "$QUICK_PRINCIPAL_ARN" ]]; then
  QUICK_PRINCIPAL_ARN=$(printf '%s' "$FOUNDATION_JSON" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(next(p["ParameterValue"] for p in s["Parameters"] if p["ParameterKey"]=="QuickPrincipalArn"))')
fi
ARTIFACT_BUCKET=$(printf '%s' "$FOUNDATION_JSON" | python3 -c 'import json,sys; s=json.load(sys.stdin); print(next(o["OutputValue"] for o in s["Outputs"] if o["OutputKey"]=="QueryResultsBucketName"))')

EXECUTE_ARGS=()
if [[ "$NO_EXECUTE" == "true" ]]; then
  EXECUTE_ARGS=(--no-execute-changeset)
fi

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --s3-prefix cfn-artifacts \
  --no-fail-on-empty-changeset \
  ${EXECUTE_ARGS[@]+"${EXECUTE_ARGS[@]}"} \
  --tags "Purpose=quick-governance-license-activity" "ManagedBy=CloudFormation" \
  --parameter-overrides \
      "FoundationStackName=$FOUNDATION_STACK" \
      "QuickPrincipalArn=$QUICK_PRINCIPAL_ARN"

if [[ "$NO_EXECUTE" == "true" ]]; then
  log "Change set created, nothing executed."
  exit 0
fi
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text
