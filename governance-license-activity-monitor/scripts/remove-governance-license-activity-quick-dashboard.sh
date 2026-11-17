#!/usr/bin/env bash
#
# remove-governance-license-activity-quick-dashboard.sh
#
# Deletes the dashboard stack. Snapshots in S3 are kept.
#
# Usage:
#   ./remove-governance-license-activity-quick-dashboard.sh --region us-east-1 --profile default

set -euo pipefail
REGION=""
PROFILE=""
STACK_NAME="quick-governance-license-activity-quick-dashboard"
err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    *) err "Unknown argument: $1" ;;
  esac
done
[[ -n "$REGION" ]] || err "--region is required"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"
