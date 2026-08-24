#!/usr/bin/env bash
#
# apply-governance-block-sharing.sh
#
# Creates (or updates) an Amazon Quick custom permissions profile that blocks
# sharing of Chat Agents, Spaces, and Datasets, then attaches it at the chosen
# scope (account, role, or user).
#
# The profile is created with `create-custom-permissions`; if one with the same
# name already exists, the script falls back to `update-custom-permissions`, so
# it is idempotent.
#
# The account is resolved from your credentials/profile (via sts:GetCallerIdentity)
# unless you pass --account-id explicitly.
#
# Required AWS permissions for the caller:
#   quicksight:CreateCustomPermissions
#   quicksight:UpdateCustomPermissions
#   quicksight:DescribeCustomPermissions
#   quicksight:UpdateAccountCustomPermission   (scope=account)
#   quicksight:UpdateRoleCustomPermission      (scope=role)
#   quicksight:UpdateUser                      (scope=user)
#   sts:GetCallerIdentity                      (when --account-id is omitted)
#
# Usage examples:
#
#   # Account-wide (default), using your default credentials
#   ./apply-governance-block-sharing.sh --region us-east-1
#
#   # A named profile, restrict to all Authors
#   ./apply-governance-block-sharing.sh \
#       --region us-east-1 --profile my-profile \
#       --scope role --role AUTHOR --namespace default
#
#   # Restrict a single user (existing role + email must be passed)
#   ./apply-governance-block-sharing.sh \
#       --region us-east-1 \
#       --scope user \
#       --user-name alice@example.com \
#       --user-role AUTHOR \
#       --user-email alice@example.com \
#       --namespace default
#
#   # Add extra deny capabilities (comma-separated)
#   ./apply-governance-block-sharing.sh --region us-east-1 --extra-deny ShareDashboards,ShareAnalyses
#
# Flags:
#   --region        (required) AWS Region where Quick is provisioned
#   --profile       named AWS profile (else default credentials)
#   --account-id    AWS account ID (default: resolved from caller identity)
#   --profile-name  custom permissions profile name (default: quick-governance-block-sharing-profile)
#   --scope         account | role | user   (default: account)
#   --role          Quick role when --scope=role (ADMIN, ADMIN_PRO, AUTHOR, AUTHOR_PRO, READER, READER_PRO)
#   --namespace     Quick namespace (default: default)
#   --user-name / --user-role / --user-email   required when --scope=user
#   --extra-deny    comma-separated extra capabilities to DENY (e.g. ShareDashboards,ShareAnalyses)

set -euo pipefail

# ---------- defaults ----------
SCOPE="account"
NAMESPACE="default"
EXTRA_DENY=""
PROFILE_NAME="quick-governance-block-sharing-profile"
ROLE=""
USER_NAME=""
USER_ROLE=""
USER_EMAIL=""
ACCOUNT_ID=""
REGION=""
PROFILE=""

# ---------- helpers ----------
err() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '[block-sharing] %s\n' "$*"; }

usage() {
  # Print the leading comment block (line 2 to the first non-comment line),
  # stripping the leading "# ".
  awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
  exit 1
}

# ---------- arg parsing ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)        REGION="$2";        shift 2 ;;
    --profile)       PROFILE="$2";       shift 2 ;;
    --account-id)    ACCOUNT_ID="$2";    shift 2 ;;
    --profile-name)  PROFILE_NAME="$2";  shift 2 ;;
    --scope)         SCOPE="$2";         shift 2 ;;
    --role)          ROLE="$2";          shift 2 ;;
    --namespace)     NAMESPACE="$2";     shift 2 ;;
    --user-name)     USER_NAME="$2";     shift 2 ;;
    --user-role)     USER_ROLE="$2";     shift 2 ;;
    --user-email)    USER_EMAIL="$2";    shift 2 ;;
    --extra-deny)    EXTRA_DENY="$2";    shift 2 ;;
    -h|--help)       usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"

# Route every aws call through the requested named profile, if any.
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"

command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"
command -v jq  >/dev/null 2>&1 || err "jq not found in PATH (used to build the capabilities JSON)"

# Resolve the account from caller identity if not supplied.
if [[ -z "$ACCOUNT_ID" ]]; then
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
    || err "Could not resolve account ID from caller identity; pass --account-id."
fi
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || err "account-id must be 12 digits (got '$ACCOUNT_ID')"

case "$SCOPE" in
  account) ;;
  role)
    [[ -n "$ROLE" ]] || err "--role is required when --scope=role"
    case "$ROLE" in
      ADMIN|ADMIN_PRO|AUTHOR|AUTHOR_PRO|READER|READER_PRO) ;;
      *) err "--role must be one of ADMIN, ADMIN_PRO, AUTHOR, AUTHOR_PRO, READER, READER_PRO" ;;
    esac
    ;;
  user)
    [[ -n "$USER_NAME"  ]] || err "--user-name is required when --scope=user"
    [[ -n "$USER_ROLE"  ]] || err "--user-role is required when --scope=user"
    [[ -n "$USER_EMAIL" ]] || err "--user-email is required when --scope=user"
    ;;
  *) err "--scope must be one of: account, role, user" ;;
esac

# ---------- build capabilities JSON ----------
CAPABILITIES_JSON=$(jq -n \
  '{
    ShareChatAgents: "DENY",
    ShareSpaces:     "DENY",
    ShareDatasets:   "DENY"
  }')

if [[ -n "$EXTRA_DENY" ]]; then
  IFS=',' read -ra EXTRAS <<< "$EXTRA_DENY"
  for cap in "${EXTRAS[@]}"; do
    cap_trimmed="${cap// /}"
    [[ -z "$cap_trimmed" ]] && continue
    CAPABILITIES_JSON=$(echo "$CAPABILITIES_JSON" | jq --arg k "$cap_trimmed" '. + {($k): "DENY"}')
  done
fi

log "Account: $ACCOUNT_ID | Region: $REGION | Scope: $SCOPE${PROFILE:+ | profile: $PROFILE}"
log "Capabilities to be denied:"
echo "$CAPABILITIES_JSON" | jq .

# ---------- create or update the profile ----------
if aws quicksight describe-custom-permissions \
      --aws-account-id "$ACCOUNT_ID" \
      --region "$REGION" \
      --custom-permissions-name "$PROFILE_NAME" >/dev/null 2>&1; then
  log "Profile '$PROFILE_NAME' already exists -- updating."
  aws quicksight update-custom-permissions \
    --aws-account-id "$ACCOUNT_ID" \
    --region "$REGION" \
    --custom-permissions-name "$PROFILE_NAME" \
    --capabilities "$CAPABILITIES_JSON" >/dev/null
else
  log "Creating profile '$PROFILE_NAME'."
  aws quicksight create-custom-permissions \
    --aws-account-id "$ACCOUNT_ID" \
    --region "$REGION" \
    --custom-permissions-name "$PROFILE_NAME" \
    --capabilities "$CAPABILITIES_JSON" \
    --tags Key=Purpose,Value=quick-governance-block-sharing >/dev/null
fi

# ---------- attach the profile at the chosen scope ----------
case "$SCOPE" in
  account)
    log "Attaching '$PROFILE_NAME' account-wide."
    aws quicksight update-account-custom-permission \
      --aws-account-id "$ACCOUNT_ID" \
      --region "$REGION" \
      --custom-permissions-name "$PROFILE_NAME" >/dev/null
    ;;
  role)
    log "Attaching '$PROFILE_NAME' to role '$ROLE' in namespace '$NAMESPACE'."
    aws quicksight update-role-custom-permission \
      --aws-account-id "$ACCOUNT_ID" \
      --region "$REGION" \
      --namespace "$NAMESPACE" \
      --role "$ROLE" \
      --custom-permissions-name "$PROFILE_NAME" >/dev/null
    ;;
  user)
    log "Attaching '$PROFILE_NAME' to user '$USER_NAME' in namespace '$NAMESPACE'."
    aws quicksight update-user \
      --aws-account-id "$ACCOUNT_ID" \
      --region "$REGION" \
      --namespace "$NAMESPACE" \
      --user-name "$USER_NAME" \
      --role "$USER_ROLE" \
      --email "$USER_EMAIL" \
      --custom-permissions-name "$PROFILE_NAME" >/dev/null
    ;;
esac

log "Done."
log "Verify with:"
log "  aws quicksight describe-custom-permissions --aws-account-id $ACCOUNT_ID --region $REGION --custom-permissions-name $PROFILE_NAME"
