#!/usr/bin/env bash
#
# remove-governance-chat-feedback-monitor.sh
#
# Tears down the chat and feedback monitor: the Quick dashboard stack first
# (skip with --keep-dashboard), then the module stack - deliveries, the
# interaction log group with its retained conversation data, the agent-name
# resolver, and the optional KMS key. Deleting the module stack permanently
# removes the chat and feedback logs held in CloudWatch Logs. The field-minimized
# S3 copies live in the shared analytics bucket (chat-feedback/) and are NOT
# deleted here.
#
# Examples:
#   ./remove-governance-chat-feedback-monitor.sh --region us-east-1 --confirm-delete-data
#   ./remove-governance-chat-feedback-monitor.sh --region us-east-1 \
#       --profile my-profile --confirm-delete-data --keep-dashboard

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-chat-feedback-monitor"
CONFIRM_DELETE_DATA="false"
KEEP_DASHBOARD="false"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DASHBOARD_SCRIPT="${SCRIPT_DIR}/remove-governance-chat-feedback-quick-dashboard.sh"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[chat-feedback] %s\n' "$*"; }
usage() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)               REGION="${2:-}";     shift 2 ;;
    --profile)              PROFILE="${2:-}";    shift 2 ;;
    --stack-name)           STACK_NAME="${2:-}"; shift 2 ;;
    --confirm-delete-data)  CONFIRM_DELETE_DATA="true"; shift ;;
    --keep-dashboard)       KEEP_DASHBOARD="true"; shift ;;
    -h|--help)              usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
[[ "$CONFIRM_DELETE_DATA" == "true" ]] || err "--confirm-delete-data is required because teardown permanently deletes conversation and feedback logs"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

if [[ "$KEEP_DASHBOARD" == "true" ]]; then
  log "Keeping the Quick dashboard stack (--keep-dashboard)."
elif [[ -x "$DASHBOARD_SCRIPT" ]]; then
  "$DASHBOARD_SCRIPT" --region "$REGION" ${PROFILE:+--profile "$PROFILE"} || log "  (dashboard removal returned non-zero, continuing)"
else
  log "Dashboard remove script not found at $DASHBOARD_SCRIPT; skipping."
fi

log "Deleting '$STACK_NAME' in '$REGION' and permanently deleting its retained interaction logs."
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
log "Waiting for stack deletion to complete..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"
log "Done. A customer managed KMS key can remain in its scheduled-deletion window after stack removal."
log "Data under chat-feedback/ in the shared analytics bucket is preserved."
