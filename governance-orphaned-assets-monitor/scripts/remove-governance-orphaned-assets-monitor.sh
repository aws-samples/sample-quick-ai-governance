#!/usr/bin/env bash
#
# remove-governance-orphaned-assets-monitor.sh
#
# Tears down the orphaned assets monitor: the Quick dashboard stack first (skip
# with --keep-dashboard), then the module stack (collector, alarms, SNS topic).
# Nothing in S3 is deleted: snapshots and collector state live in the shared
# analytics bucket owned by the Analytics Foundation stack, under
# orphaned-assets/. Remove that data, or the foundation itself, through
# governance-analytics-foundation/scripts.
#
# Usage:
#   ./remove-governance-orphaned-assets-monitor.sh --region us-east-1 [--profile p] [--stack-name s] [--keep-dashboard]

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-orphaned-assets"
KEEP_DASHBOARD="false"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DASHBOARD_SCRIPT="${SCRIPT_DIR}/remove-governance-orphaned-assets-quick-dashboard.sh"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[orphaned-assets] %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)          REGION="$2";            shift 2 ;;
    --profile)         PROFILE="$2";           shift 2 ;;
    --stack-name)      STACK_NAME="$2";        shift 2 ;;
    --keep-dashboard)  KEEP_DASHBOARD="true";  shift ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

if [[ "$KEEP_DASHBOARD" == "true" ]]; then
  log "Keeping the Quick dashboard stack (--keep-dashboard)."
elif [[ -x "$DASHBOARD_SCRIPT" ]]; then
  "$DASHBOARD_SCRIPT" --region "$REGION" ${PROFILE:+--profile "$PROFILE"} || log "  (dashboard removal returned non-zero, continuing)"
else
  log "Dashboard remove script not found at $DASHBOARD_SCRIPT; skipping."
fi

log "Deleting stack '$STACK_NAME' in $REGION..."
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"
log "Stack deleted. Data under orphaned-assets/ in the shared analytics bucket is preserved."
