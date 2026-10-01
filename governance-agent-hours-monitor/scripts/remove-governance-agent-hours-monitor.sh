#!/usr/bin/env bash
#
# remove-governance-agent-hours-monitor.sh
#
# Tears down the Amazon Quick agent-hours monitor deployed by
# apply-governance-agent-hours-monitor.sh: the Quick dashboard stack first
# (skip with --keep-dashboard), then the module stack (the vended-log delivery).
#
# Nothing in S3 is deleted: the feed lives in the shared analytics bucket owned
# by the Analytics Foundation stack, under agent-hours/. Remove that data, or
# the foundation itself, through governance-analytics-foundation/scripts.
#
# Credentials: uses your default AWS credentials, or pass --profile <name>
# (equivalently, set the AWS_PROFILE environment variable).
#
# Usage examples:
#
#   ./remove-governance-agent-hours-monitor.sh --region us-east-1
#   ./remove-governance-agent-hours-monitor.sh --region us-east-1 --profile my-profile --keep-dashboard

set -euo pipefail

# ---------- defaults ----------
REGION=""
PROFILE=""
STACK_NAME="quick-governance-agent-hours-monitor"
KEEP_DASHBOARD="false"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DASHBOARD_SCRIPT="${SCRIPT_DIR}/remove-governance-agent-hours-quick-dashboard.sh"

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
    --region)          REGION="$2";            shift 2 ;;
    --profile)         PROFILE="$2";           shift 2 ;;
    --stack-name)      STACK_NAME="$2";        shift 2 ;;
    --keep-dashboard)  KEEP_DASHBOARD="true";  shift ;;
    -h|--help)         usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

# Route every aws call through the requested named profile, if any.
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

# ---------- Quick dashboard ----------
if [[ "$KEEP_DASHBOARD" == "true" ]]; then
  log "Keeping the Quick dashboard stack (--keep-dashboard)."
elif [[ -x "$DASHBOARD_SCRIPT" ]]; then
  "$DASHBOARD_SCRIPT" --region "$REGION" ${PROFILE:+--profile "$PROFILE"} || log "  (dashboard removal returned non-zero, continuing)"
else
  log "Dashboard remove script not found at $DASHBOARD_SCRIPT; skipping."
fi

# ---------- delete the module stack ----------
log "Deleting stack '$STACK_NAME'${PROFILE:+ (profile: $PROFILE)}."
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK_NAME"
log "Waiting for stack deletion to complete..."
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK_NAME" \
  || log "  (wait returned non-zero; check the console if the stack lingers)"

log "Done. Data under agent-hours/ in the shared analytics bucket is preserved."
