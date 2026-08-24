#!/usr/bin/env bash
#
# apply-governance-chat-feedback-monitor.sh
#
# Deploys regional Amazon Quick CHAT_LOGS and FEEDBACK_LOGS vended-log delivery,
# an encrypted CloudWatch Logs log group, metrics, a CloudWatch dashboard, and
# a minimal daily Lambda that refreshes the agent ID-to-name lookup table.
# Delivery is not retroactive. Deploy once in each Region where Quick is used.
#
# Prerequisites:
#   * Amazon Quick AI features enabled (Enterprise or Professional).
#   * The caller can deploy the template and has
#     quicksight:AllowVendedLogDeliveryForResource on the Quick account.
#
# Examples:
#   ./apply-governance-chat-feedback-monitor.sh --region us-east-1
#   ./apply-governance-chat-feedback-monitor.sh --region us-east-1 --profile my-profile
#   ./apply-governance-chat-feedback-monitor.sh --region us-east-1 \
#       --log-retention-days 90 --enable-kms true

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-chat-feedback-monitor"
PREFIX="quick-governance-chat-feedback-monitor"
LOG_GROUP="/aws/vendedlogs/quick/chat-feedback"
LOG_RETENTION_DAYS="90"
ENABLE_KMS="true"
CHAT_SOURCE_NAME=""
FEEDBACK_SOURCE_NAME=""

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-chat-feedback-monitor.yaml"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[chat-feedback] %s\n' "$*"; }
usage() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)              REGION="${2:-}";             shift 2 ;;
    --profile)             PROFILE="${2:-}";            shift 2 ;;
    --stack-name)          STACK_NAME="${2:-}";         shift 2 ;;
    --prefix)              PREFIX="${2:-}";             shift 2 ;;
    --log-group)           LOG_GROUP="${2:-}";          shift 2 ;;
    --log-retention-days)  LOG_RETENTION_DAYS="${2:-}"; shift 2 ;;
    --enable-kms)          ENABLE_KMS="${2:-}";         shift 2 ;;
    --chat-source-name)     CHAT_SOURCE_NAME="${2:-}";    shift 2 ;;
    --feedback-source-name) FEEDBACK_SOURCE_NAME="${2:-}"; shift 2 ;;
    -h|--help)             usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
[[ "$ENABLE_KMS" == "true" || "$ENABLE_KMS" == "false" ]] || err "--enable-kms must be true or false"
[[ "$PREFIX" =~ ^[a-z0-9-]{1,40}$ ]] || err "--prefix must contain 1-40 lowercase letters, digits, or hyphens"
[[ -f "$TEMPLATE" ]] || err "Cannot find template at $TEMPLATE"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# A Quick account can have only one delivery source for each account/log-type
# pair. Reuse an existing source (which can fan out to multiple destinations)
# instead of attempting to create a duplicate.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "$REGION")"
QUICK_ACCOUNT_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:account/${ACCOUNT_ID}"

find_source() {
  local log_type="$1"
  aws logs describe-delivery-sources \
    --region "$REGION" \
    --query "deliverySources[?logType=='${log_type}' && contains(resourceArns, '${QUICK_ACCOUNT_ARN}')].name | [0]" \
    --output text
}

CREATE_CHAT_SOURCE="true"
if [[ -z "$CHAT_SOURCE_NAME" ]]; then
  CHAT_SOURCE_NAME="$(find_source CHAT_LOGS)"
fi
if [[ -n "$CHAT_SOURCE_NAME" && "$CHAT_SOURCE_NAME" != "None" ]]; then
  CREATE_CHAT_SOURCE="false"
  log "Reusing CHAT_LOGS delivery source '$CHAT_SOURCE_NAME'."
else
  CHAT_SOURCE_NAME=""
  log "No CHAT_LOGS delivery source found; the stack will create one."
fi

CREATE_FEEDBACK_SOURCE="true"
if [[ -z "$FEEDBACK_SOURCE_NAME" ]]; then
  FEEDBACK_SOURCE_NAME="$(find_source FEEDBACK_LOGS)"
fi
if [[ -n "$FEEDBACK_SOURCE_NAME" && "$FEEDBACK_SOURCE_NAME" != "None" ]]; then
  CREATE_FEEDBACK_SOURCE="false"
  log "Reusing FEEDBACK_LOGS delivery source '$FEEDBACK_SOURCE_NAME'."
else
  FEEDBACK_SOURCE_NAME=""
  log "No FEEDBACK_LOGS delivery source found; the stack will create one."
fi

STACK_STATUS="$(aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
if [[ "$STACK_STATUS" == "ROLLBACK_COMPLETE" ]]; then
  err "Stack '$STACK_NAME' is ROLLBACK_COMPLETE. Delete the failed stack before retrying: aws cloudformation delete-stack --stack-name '$STACK_NAME' --region '$REGION'${PROFILE:+ --profile '$PROFILE'}"
fi

log "Deploying '$STACK_NAME' in '$REGION'${PROFILE:+ with profile '$PROFILE'}."
log "WARNING: the log group contains prompts, responses, feedback comments, and resource metadata."
log "Restrict CloudWatch Logs and dashboard access according to your data policy."

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --tags "Purpose=$PREFIX" "DataClassification=Sensitive" "ManagedBy=CloudFormation" \
  --parameter-overrides \
      "ResourcePrefix=$PREFIX" \
      "LogGroupName=$LOG_GROUP" \
      "LogRetentionDays=$LOG_RETENTION_DAYS" \
      "EnableKmsEncryption=$ENABLE_KMS" \
      "CreateChatDeliverySource=$CREATE_CHAT_SOURCE" \
      "ExistingChatDeliverySourceName=$CHAT_SOURCE_NAME" \
      "CreateFeedbackDeliverySource=$CREATE_FEEDBACK_SOURCE" \
      "ExistingFeedbackDeliverySourceName=$FEEDBACK_SOURCE_NAME"

log "Stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
  --output text 2>/dev/null | sed 's/^/  /' || true

log "Done. Open DashboardUrl after sending a new Quick message."
log "Only interactions created after deployment in this Region will appear."
