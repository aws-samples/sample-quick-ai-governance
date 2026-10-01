#!/usr/bin/env bash
#
# apply-governance-chat-feedback-monitor.sh
#
# Deploys the Amazon Quick chat and feedback monitor:
#   1. the module stack - regional CHAT_LOGS and FEEDBACK_LOGS vended-log delivery
#      to an encrypted CloudWatch Logs log group (the content audit trail) and,
#      field-minimized (no prompts, responses or comments), to the shared
#      analytics bucket of the Analytics Foundation stack; plus the Lambda that
#      exports the agent ID-to-name CSV the Quick dashboard joins on;
#   2. the module's Amazon Quick dashboard stack, via
#      apply-governance-chat-feedback-quick-dashboard.sh - skip with --skip-dashboard.
# Delivery is not retroactive. Deploy once in each Region where Quick is used.
#
# Prerequisites:
#   * The Analytics Foundation stack in this Region
#     (../../governance-analytics-foundation/scripts/apply-governance-analytics-foundation.sh).
#   * Amazon Quick AI features enabled (Enterprise or Professional).
#   * The caller can deploy the template and has
#     quicksight:AllowVendedLogDeliveryForResource on the Quick account.
#
# Examples:
#   ./apply-governance-chat-feedback-monitor.sh --region us-east-1
#   ./apply-governance-chat-feedback-monitor.sh --region us-east-1 --profile my-profile
#   ./apply-governance-chat-feedback-monitor.sh --region us-east-1 \
#       --log-retention-days 90 --enable-kms true
#   # Module only (data in S3 and the log group, no Quick dashboard):
#   ./apply-governance-chat-feedback-monitor.sh --region us-east-1 --skip-dashboard
#   # Agent renames refresh names immediately via the AGENT_METADATA_LOGS feed
#   # (default on); disable if another stack already owns that log type's source:
#   ./apply-governance-chat-feedback-monitor.sh --region us-east-1 --enable-agent-lifecycle false

set -euo pipefail

REGION=""
PROFILE=""
STACK_NAME="quick-governance-chat-feedback-monitor"
PREFIX="quick-governance-chat-feedback-monitor"
FOUNDATION_STACK="quick-governance-analytics-foundation"
LOG_GROUP="/aws/vendedlogs/quick/chat-feedback"
LOG_RETENTION_DAYS="90"
ENABLE_KMS="true"
CHAT_SOURCE_NAME=""
FEEDBACK_SOURCE_NAME=""
ENABLE_AGENT_LIFECYCLE="true"
SKIP_DASHBOARD="false"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TEMPLATE="${SCRIPT_DIR}/../cloudformation/governance-chat-feedback-monitor.yaml"
DASHBOARD_SCRIPT="${SCRIPT_DIR}/apply-governance-chat-feedback-quick-dashboard.sh"

err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[chat-feedback] %s\n' "$*"; }
usage() {
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)                 REGION="${2:-}";                 shift 2 ;;
    --profile)                PROFILE="${2:-}";                shift 2 ;;
    --stack-name)             STACK_NAME="${2:-}";             shift 2 ;;
    --prefix)                 PREFIX="${2:-}";                 shift 2 ;;
    --foundation-stack)       FOUNDATION_STACK="${2:-}";       shift 2 ;;
    --log-group)              LOG_GROUP="${2:-}";              shift 2 ;;
    --log-retention-days)     LOG_RETENTION_DAYS="${2:-}";     shift 2 ;;
    --enable-kms)             ENABLE_KMS="${2:-}";             shift 2 ;;
    --chat-source-name)       CHAT_SOURCE_NAME="${2:-}";       shift 2 ;;
    --feedback-source-name)   FEEDBACK_SOURCE_NAME="${2:-}";   shift 2 ;;
    --enable-agent-lifecycle) ENABLE_AGENT_LIFECYCLE="${2:-}"; shift 2 ;;
    --skip-dashboard)         SKIP_DASHBOARD="true";           shift ;;
    -h|--help)                usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

[[ -n "$REGION" ]] || err "--region is required"
[[ "$ENABLE_KMS" == "true" || "$ENABLE_KMS" == "false" ]] || err "--enable-kms must be true or false"
[[ "$PREFIX" =~ ^[a-z0-9-]{1,40}$ ]] || err "--prefix must contain 1-40 lowercase letters, digits, or hyphens"
[[ -f "$TEMPLATE" ]] || err "Cannot find template at $TEMPLATE"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"
[[ "$ENABLE_AGENT_LIFECYCLE" == "true" || "$ENABLE_AGENT_LIFECYCLE" == "false" ]] || err "--enable-agent-lifecycle must be true or false"

[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- resolve the foundation ----------
ANALYTICS_BUCKET="$(aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$FOUNDATION_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='AnalyticsBucketName'].OutputValue" \
  --output text 2>/dev/null || true)"
[[ -n "$ANALYTICS_BUCKET" && "$ANALYTICS_BUCKET" != "None" ]] \
  || err "Foundation stack '$FOUNDATION_STACK' not found in $REGION - deploy governance-analytics-foundation first"

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

log "Deploying '$STACK_NAME' in '$REGION' -> s3://$ANALYTICS_BUCKET/chat-feedback/${PROFILE:+ (profile: $PROFILE)}."
log "WARNING: the log group contains prompts, responses, feedback comments, and resource metadata."
log "Restrict CloudWatch Logs access according to your data policy (the S3 copies exclude that content)."

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
      "ExistingFeedbackDeliverySourceName=$FEEDBACK_SOURCE_NAME" \
      "AnalyticsBucketName=$ANALYTICS_BUCKET" \
      "EnableAgentLifecycleTrigger=$ENABLE_AGENT_LIFECYCLE"

log "Stack outputs:"
aws cloudformation describe-stacks \
  --region "$REGION" --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' \
  --output text 2>/dev/null | sed 's/^/  /' || true
log "Only interactions created after deployment in this Region will appear."

# ---------- Quick dashboard ----------
if [[ "$SKIP_DASHBOARD" == "true" ]]; then
  log "Done (--skip-dashboard)."
  exit 0
fi
[[ -x "$DASHBOARD_SCRIPT" ]] || err "Dashboard script not found or not executable: $DASHBOARD_SCRIPT"
log "Deploying the Amazon Quick dashboard."
"$DASHBOARD_SCRIPT" --region "$REGION" ${PROFILE:+--profile "$PROFILE"} --foundation-stack "$FOUNDATION_STACK"
