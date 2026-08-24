#!/usr/bin/env bash
#
# remove-governance-chat-feedback-monitor.sh
#
# Deletes the CloudFormation stack and its CloudWatch dashboard, deliveries,
# log group, retained conversation data, metrics, and optional KMS key.
# Deleting the stack permanently removes the retained chat and feedback logs.
#
# Examples:
#   ./remove-governance-chat-feedback-monitor.sh --region us-east-1
#   ./remove-governance-chat-feedback-monitor.sh --region us-east-1 \
#       --profile my-profile --confirm-delete-data

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-chat-feedback-monitor"
CONFIRM_DELETE_DATA="false"

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
    -h|--help)              usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
[[ "$CONFIRM_DELETE_DATA" == "true" ]] || err "--confirm-delete-data is required because teardown permanently deletes conversation and feedback logs"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

log "Deleting '$STACK_NAME' in '$REGION' and permanently deleting its retained interaction logs."
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
log "Waiting for stack deletion to complete..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"
log "Done. A customer managed KMS key can remain in its scheduled-deletion window after stack removal."
