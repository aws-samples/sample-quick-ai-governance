#!/usr/bin/env bash
#
# remove-governance-spreadsheet-file-rename.sh
#
# Thin wrapper around "sam delete" — tears down the Amazon Quick spreadsheet-file
# auto-rename stack created by apply-governance-spreadsheet-file-rename.sh.
#
# Deletes the CloudFormation stack (Lambda, IAM role/policy, EventBridge rule,
# CloudWatch log group, error alarm) and the SAM-managed deployment artifacts
# for this stack. The log group is a stack resource, so its logs are removed
# with the stack.
#
# Credentials: uses your default AWS credentials, or pass --profile <name>
# (equivalently, set the AWS_PROFILE environment variable).
#
# Usage examples:
#
#   ./remove-governance-spreadsheet-file-rename.sh --region us-east-1
#   ./remove-governance-spreadsheet-file-rename.sh --region us-east-1 --profile my-profile
#   ./remove-governance-spreadsheet-file-rename.sh --region us-east-1 --stack-name my-stack

set -euo pipefail

# ---------- defaults ----------
REGION=""
PROFILE=""
STACK_NAME="quick-governance-spreadsheet-file-rename"

# ---------- helpers ----------
err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[spreadsheet-rename] %s\n' "$*"; }

usage() {
  # Print the leading comment block (line 2 to the first non-comment line),
  # stripping the leading "# ".
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)     REGION="$2";     shift 2 ;;
    --profile)    PROFILE="$2";    shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    -h|--help)    usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"
command -v sam >/dev/null 2>&1 || err "AWS SAM CLI not found in PATH"

# Route every sam call through the requested named profile, if any.
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- delete ----------
log "Deleting stack '$STACK_NAME' in $REGION${PROFILE:+ (profile: $PROFILE)}."
sam delete \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --no-prompts

log "Done."
