#!/usr/bin/env bash
#
# remove-governance-dataset-lifecycle-monitor.sh
#
# Tears down the Amazon Quick dataset lifecycle monitor deployed by
# apply-governance-dataset-lifecycle-monitor.sh: the Quick dashboard stack first
# (skip with --keep-dashboard), then the module stack (collector, alarms, SNS).
#
# NOT deleted (kept on purpose): the snapshots and collector state under
# dataset-lifecycle/ in the shared analytics bucket owned by the Analytics
# Foundation stack. Remove that data, or the foundation itself, through
# governance-analytics-foundation/scripts.
#
# Usage:
#   ./remove-governance-dataset-lifecycle-monitor.sh --region us-east-1
#   ./remove-governance-dataset-lifecycle-monitor.sh \
#       --region us-east-1 --profile my-profile --stack-name my-stack --keep-dashboard
#
# Flags:
#   --region          (required) AWS Region the stack was deployed into
#   --profile         named AWS profile (else default credentials)
#   --stack-name      CloudFormation stack name (default quick-governance-dataset-lifecycle)
#   --keep-dashboard  leave the Quick dashboard stack in place

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-dataset-lifecycle"
KEEP_DASHBOARD="false"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DASHBOARD_SCRIPT="${SCRIPT_DIR}/remove-governance-dataset-lifecycle-quick-dashboard.sh"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[dataset-lifecycle] %s\n' "$*"; }

usage() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)          REGION="$2";            shift 2 ;;
    --profile)         PROFILE="$2";           shift 2 ;;
    --stack-name)      STACK_NAME="$2";        shift 2 ;;
    --keep-dashboard)  KEEP_DASHBOARD="true";  shift ;;
    -h|--help)         usage ;;
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

log "Deleting stack '$STACK_NAME' in $REGION${PROFILE:+ (profile: $PROFILE)}..."
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"

log "Waiting for deletion to complete..."
aws cloudformation wait stack-delete-complete \
  --region "$REGION" --stack-name "$STACK_NAME"

log "Stack deleted. Data under dataset-lifecycle/ in the shared analytics bucket is preserved."
