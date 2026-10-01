#!/usr/bin/env bash
#
# remove-governance-analytics-foundation.sh
#
# Deletes the governance analytics foundation stack. Fails while any module
# dashboard stack still imports its exports - remove those stacks first.
# The analytics, query-results and access-log buckets are RETAINED; remove
# them manually with "aws s3 rb --force" when no longer needed.
#
# Usage:
#   ./remove-governance-analytics-foundation.sh --region us-east-1 [--profile <name>] [--stack-name <name>]

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-analytics-foundation"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[analytics-foundation] %s\n' "$*"; }

usage() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)      REGION="$2";      shift 2 ;;
    --profile)     PROFILE="$2";     shift 2 ;;
    --stack-name)  STACK_NAME="$2";  shift 2 ;;
    -h|--help)     usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

IMPORTERS=$(aws cloudformation list-imports --region "$REGION" \
  --export-name "${STACK_NAME}-GlueDatabase" --query 'Imports' --output text 2>/dev/null || true)
if [[ -n "$IMPORTERS" && "$IMPORTERS" != "None" ]]; then
  err "Exports still imported by: $IMPORTERS - remove those stacks first."
fi

log "Deleting stack '$STACK_NAME'${PROFILE:+ (profile: $PROFILE)}."
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
log "Waiting for stack deletion to complete..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME" \
  || log "  (wait returned non-zero; check the console if the stack lingers)"
log "Done. Buckets were retained; delete them manually when no longer needed."
