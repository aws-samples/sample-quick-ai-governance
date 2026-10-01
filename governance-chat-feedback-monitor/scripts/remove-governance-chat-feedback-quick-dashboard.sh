#!/usr/bin/env bash
#
# remove-governance-chat-feedback-quick-dashboard.sh
#
# Deletes the native Amazon Quick dashboard stack of the Chat and Feedback Monitor
# (dashboard, dataset(s) and Glue table(s)). The data in S3 is not touched.
#
# Usage:
#   ./remove-governance-chat-feedback-quick-dashboard.sh --region us-east-1 [--profile <name>] [--stack-name <name>]

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-chat-feedback-quick-dashboard"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[chat-feedback-quick] %s\n' "$*"; }

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

log "Deleting stack '$STACK_NAME'${PROFILE:+ (profile: $PROFILE)}."
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
log "Waiting for stack deletion to complete..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME" \
  || log "  (wait returned non-zero; check the console if the stack lingers)"
log "Done. Data in S3 was left untouched."
