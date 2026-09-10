#!/usr/bin/env bash
#
# remove-governance-dataset-lifecycle-monitor.sh
#
# Tears down the Amazon Quick dataset lifecycle monitor stack deployed by
# apply-governance-dataset-lifecycle-monitor.sh.
#
# NOT deleted (kept on purpose):
#   * The S3 state/snapshot bucket (<resource-prefix>-<account-id>) has
#     DeletionPolicy: Retain so scan history survives teardown. Empty and
#     delete it manually if you want it gone:
#       aws s3 rb "s3://<bucket>" --force --region <region>
#
# Usage:
#   ./remove-governance-dataset-lifecycle-monitor.sh --region us-east-1
#   ./remove-governance-dataset-lifecycle-monitor.sh \
#       --region us-east-1 --profile my-profile --stack-name my-stack
#
# Flags:
#   --region      (required) AWS Region the stack was deployed into
#   --profile     named AWS profile (else default credentials)
#   --stack-name  CloudFormation stack name
#                 (default quick-governance-dataset-lifecycle)

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-dataset-lifecycle"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[dataset-lifecycle] %s\n' "$*"; }

usage() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)     REGION="$2";     shift 2 ;;
    --profile)    PROFILE="$2";    shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    -h|--help)    usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# Surface the retained bucket name before the stack (and its outputs) go away.
BUCKET="$(aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='StateBucketName'].OutputValue" \
  --output text 2>/dev/null || true)"

log "Deleting stack '$STACK_NAME' in $REGION${PROFILE:+ (profile: $PROFILE)}..."
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"

log "Waiting for deletion to complete..."
aws cloudformation wait stack-delete-complete \
  --region "$REGION" --stack-name "$STACK_NAME"

log "Stack deleted."
if [[ -n "$BUCKET" && "$BUCKET" != "None" ]]; then
  log "Retained S3 bucket (delete manually if no longer needed):"
  log "  aws s3 rb \"s3://$BUCKET\" --force --region $REGION"
fi
