#!/usr/bin/env bash
#
# remove-governance-agent-hours-monitor.sh
#
# Tears down the Amazon Quick agent-hours monitor deployed by
# apply-governance-agent-hours-monitor.sh (deletes the CloudFormation stack).
#
# The S3 log bucket is RETAINED by the stack (DeletionPolicy: Retain) so
# historical logs survive teardown. Pass --delete-bucket to empty and delete
# it after the stack is gone.
#
# Credentials: uses your default AWS credentials, or pass --profile <name>
# (equivalently, set the AWS_PROFILE environment variable).
#
# Usage examples:
#
#   # Delete the stack, keep the S3 bucket and its logs
#   ./remove-governance-agent-hours-monitor.sh --region us-east-1
#
#   # Use a named profile and also delete the S3 bucket (irreversible)
#   ./remove-governance-agent-hours-monitor.sh \
#       --region us-east-1 --profile my-profile --delete-bucket

set -euo pipefail

# ---------- defaults ----------
REGION=""
PROFILE=""
STACK_NAME="quick-governance-agent-hours-monitor"
DELETE_BUCKET="false"

# ---------- helpers ----------
err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[agent-hours] %s\n' "$*"; }

usage() {
  # Print the leading comment block (line 2 to the first non-comment line),
  # stripping the leading "# ".
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)         REGION="$2";           shift 2 ;;
    --profile)        PROFILE="$2";          shift 2 ;;
    --stack-name)     STACK_NAME="$2";       shift 2 ;;
    --delete-bucket)  DELETE_BUCKET="true";  shift ;;
    -h|--help)        usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

# Route every aws call through the requested named profile, if any.
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- capture the retained bucket name before deleting the stack ----------
BUCKET_NAME=""
if [[ "$DELETE_BUCKET" == "true" ]]; then
  BUCKET_NAME=$(aws cloudformation describe-stacks \
    --region "$REGION" --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='LogBucketName'].OutputValue | [0]" \
    --output text 2>/dev/null || true)
fi

# ---------- delete the stack ----------
log "Deleting stack '$STACK_NAME'${PROFILE:+ (profile: $PROFILE)}."
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
log "Waiting for stack deletion to complete..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME" \
  || log "  (wait returned non-zero; check the console if the stack lingers)"

# ---------- optionally remove the retained bucket ----------
if [[ "$DELETE_BUCKET" == "true" ]]; then
  if [[ -n "$BUCKET_NAME" && "$BUCKET_NAME" != "None" ]]; then
    log "Emptying and deleting retained S3 bucket '$BUCKET_NAME' (irreversible)."
    aws s3 rm "s3://${BUCKET_NAME}" --recursive --region "$REGION" >/dev/null 2>&1 \
      || log "  (nothing to empty, continuing)"
    aws s3api delete-bucket --bucket "$BUCKET_NAME" --region "$REGION" >/dev/null 2>&1 \
      || log "  (bucket not found or not empty, continuing)"
  else
    log "Could not resolve the bucket name from stack outputs; delete it manually if needed."
  fi
else
  log "S3 log bucket (if any) preserved. Re-run with --delete-bucket to remove it."
fi

log "Done."
