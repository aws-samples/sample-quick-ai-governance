#!/usr/bin/env bash
#
# apply-governance-spreadsheet-file-rename.sh
#
# Thin wrapper around "sam deploy" for the Amazon Quick spreadsheet-file
# auto-rename stack (../cloudformation/governance-spreadsheet-file-rename.yaml).
#
# The SAM template is the single source of truth. This script just packages
# ../lambda/app.py and deploys/updates the stack idempotently, then prints the
# stack outputs. Tear it down with remove-governance-spreadsheet-file-rename.sh.
#
# The stack deploys into the account resolved from your credentials/profile and
# targets it via the AWS::AccountId pseudo-parameter -- there is no account ID
# to pass. The Amazon Quick subscription must be in that same account/Region.
#
# What the stack creates (all named ${ResourcePrefix}-*):
#   * Lambda function (Python 3.14) that renames matching dataset uploads
#   * IAM role + inline policy (quicksight:DescribeDataSet / UpdateDataSet)
#   * EventBridge rule on "QuickSight DataSet Created"
#     (and optionally "... Updated") + invoke permission
#   * CloudWatch Logs log group (with retention) and an error alarm
#
# Prerequisites:
#   * AWS SAM CLI installed
#     https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html
#   * AWS CLI v2 authenticated (used here to print stack outputs)
#   * "--resolve-s3" lets SAM manage the deployment-artifact bucket for you.
#
# Usage examples:
#
#   # Minimum (uses default credentials)
#   ./apply-governance-spreadsheet-file-rename.sh --region us-east-1
#
#   # Named profile, also rename on re-uploads, custom dataset prefix
#   ./apply-governance-spreadsheet-file-rename.sh \
#       --region us-east-1 --profile my-profile \
#       --also-on-update --prefix excel-
#
#   # Act on CSV uploads instead of XLSX
#   ./apply-governance-spreadsheet-file-rename.sh \
#       --region us-east-1 --target-format CSV --prefix csv-
#
# Flags:
#   --region                (required) AWS Region to deploy into
#   --profile               named AWS profile (else default credentials)
#   --stack-name            CloudFormation stack name
#   --resource-prefix       prefix for every AWS resource name
#   --prefix                dataset-name prefix (default xls-)
#   --target-format         CSV|TSV|CLF|ELF|XLSX|JSON (default XLSX)
#   --also-on-update        also rename on "Dataset Updated" events
#   --reserved-concurrency  reserved concurrent executions (default 5)
#   --log-retention-days    CloudWatch Logs retention (default 90)

set -euo pipefail

# ---------- defaults ----------
REGION=""
PROFILE=""
STACK_NAME="quick-governance-spreadsheet-file-rename"
RESOURCE_PREFIX="quick-governance-spreadsheet-file-rename"
PREFIX="xls-"
TARGET_FORMAT="XLSX"
ALSO_ON_UPDATE="false"
RESERVED_CONCURRENCY="5"
LOG_RETENTION_DAYS="90"

# Resolve script dir so we can locate ../cloudformation regardless of cwd
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-spreadsheet-file-rename.yaml"

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
    --region)               REGION="$2";               shift 2 ;;
    --profile)              PROFILE="$2";              shift 2 ;;
    --stack-name)           STACK_NAME="$2";           shift 2 ;;
    --resource-prefix)      RESOURCE_PREFIX="$2";      shift 2 ;;
    --prefix)               PREFIX="$2";               shift 2 ;;
    --target-format)        TARGET_FORMAT="$2";        shift 2 ;;
    --also-on-update)       ALSO_ON_UPDATE="true";     shift ;;
    --reserved-concurrency) RESERVED_CONCURRENCY="$2"; shift 2 ;;
    --log-retention-days)   LOG_RETENTION_DAYS="$2";   shift 2 ;;
    -h|--help)              usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"
case "$TARGET_FORMAT" in
  CSV|TSV|CLF|ELF|XLSX|JSON) ;;
  *) err "--target-format must be one of: CSV TSV CLF ELF XLSX JSON" ;;
esac
[[ -f "$TEMPLATE" ]] || err "Cannot find template at $TEMPLATE"
command -v sam >/dev/null 2>&1 || err "AWS SAM CLI not found in PATH (see https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

# Route every sam/aws call through the requested named profile, if any.
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- deploy ----------
log "Deploying stack '$STACK_NAME' in $REGION${PROFILE:+ (profile: $PROFILE)}."
log "Target format: $TARGET_FORMAT | dataset prefix: '$PREFIX' | also-on-update: $ALSO_ON_UPDATE"

sam deploy \
  --template-file "$TEMPLATE" \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --resolve-s3 \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --tags "Purpose=$RESOURCE_PREFIX" "ManagedBy=SAM" \
  --parameter-overrides \
      "ResourcePrefix=$RESOURCE_PREFIX" \
      "Prefix=$PREFIX" \
      "TargetFormat=$TARGET_FORMAT" \
      "AlsoOnUpdate=$ALSO_ON_UPDATE" \
      "ReservedConcurrency=$RESERVED_CONCURRENCY" \
      "LogRetentionDays=$LOG_RETENTION_DAYS"

# ---------- report outputs ----------
log "Stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
  --output text 2>/dev/null | sed 's/^/  /' || true

log "Done. New $TARGET_FORMAT dataset uploads will be renamed with the '$PREFIX' prefix within seconds."
