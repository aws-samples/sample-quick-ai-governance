#!/usr/bin/env bash
#
# remove-governance-license-activity-monitor.sh
#
# Deletes the dashboard stack, then the module stack. Snapshots under
# license-activity/ stay in the foundation bucket.
#
# Usage:
#   ./remove-governance-license-activity-monitor.sh --region us-east-1 --profile default

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-license-activity"
KEEP_DASHBOARD="false"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[license-activity] %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --keep-dashboard) KEEP_DASHBOARD="true"; shift ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

if [[ "$KEEP_DASHBOARD" != "true" ]]; then
  "$SCRIPT_DIR/remove-governance-license-activity-quick-dashboard.sh" --region "$REGION" ${PROFILE:+--profile "$PROFILE"}
fi
log "Deleting stack $STACK_NAME"
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"
log "Deleted $STACK_NAME. Snapshots in the foundation bucket were kept."
