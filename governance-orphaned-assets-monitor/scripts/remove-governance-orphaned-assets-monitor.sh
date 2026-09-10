#!/usr/bin/env bash
#
# remove-governance-orphaned-assets-monitor.sh
#
# Deletes the orphaned assets monitor stack. The S3 findings/snapshot bucket
# is retained (DeletionPolicy: Retain) so finding history survives; remove it
# manually when no longer needed:
#   aws s3 rb "s3://quick-governance-orphaned-assets-<account-id>" --force
#
# Usage:
#   ./remove-governance-orphaned-assets-monitor.sh --region us-east-1 [--profile p] [--stack-name s]

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-orphaned-assets"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[orphaned-assets] %s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)     REGION="$2";     shift 2 ;;
    --profile)    PROFILE="$2";    shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

log "Deleting stack '$STACK_NAME' in $REGION..."
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME"
log "Stack deleted. The S3 findings bucket is retained by design."
