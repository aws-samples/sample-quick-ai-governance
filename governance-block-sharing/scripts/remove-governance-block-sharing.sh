#!/usr/bin/env bash
#
# remove-governance-block-sharing.sh
#
# Detaches the Amazon Quick block-sharing custom permissions profile from the
# chosen scope and (optionally) deletes the profile itself.
#
# Use the same --scope value (and identifiers) you used with
# apply-governance-block-sharing.sh. The account is resolved from your
# credentials/profile unless you pass --account-id.
#
# Usage examples:
#
#   # Detach from account, then delete the profile
#   ./remove-governance-block-sharing.sh \
#       --region us-east-1 --scope account --delete-profile
#
#   # Detach from a role only (leave the profile in place for reuse)
#   ./remove-governance-block-sharing.sh \
#       --region us-east-1 --scope role --role AUTHOR --namespace default
#
#   # Detach from a single user
#   ./remove-governance-block-sharing.sh \
#       --region us-east-1 --scope user \
#       --user-name alice@example.com --user-role AUTHOR \
#       --user-email alice@example.com --namespace default
#
# Flags:
#   --region        (required) AWS Region where Quick is provisioned
#   --profile       named AWS profile (else default credentials)
#   --account-id    AWS account ID (default: resolved from caller identity)
#   --profile-name  custom permissions profile name (default: quick-governance-block-sharing-profile)
#   --scope         account | role | user   (default: account)
#   --role          Quick role when --scope=role
#   --namespace     Quick namespace (default: default)
#   --user-name / --user-role / --user-email   required when --scope=user
#   --delete-profile   also delete the profile object after detaching

set -euo pipefail

# ---------- defaults ----------
SCOPE="account"
NAMESPACE="default"
PROFILE_NAME="quick-governance-block-sharing-profile"
ROLE=""
USER_NAME=""
USER_ROLE=""
USER_EMAIL=""
ACCOUNT_ID=""
REGION=""
PROFILE=""
DELETE_PROFILE="false"

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
    --region)         REGION="$2";        shift 2 ;;
    --profile)        PROFILE="$2";       shift 2 ;;
    --account-id)     ACCOUNT_ID="$2";    shift 2 ;;
    --profile-name)   PROFILE_NAME="$2";  shift 2 ;;
    --scope)          SCOPE="$2";         shift 2 ;;
    --role)           ROLE="$2";          shift 2 ;;
    --namespace)      NAMESPACE="$2";     shift 2 ;;
    --user-name)      USER_NAME="$2";     shift 2 ;;
    --user-role)      USER_ROLE="$2";     shift 2 ;;
    --user-email)     USER_EMAIL="$2";    shift 2 ;;
    --delete-profile) DELETE_PROFILE="true"; shift ;;
    -h|--help)        usage ;;
    *) err "Unknown argument: $1" ;;
  esac
done

# ---------- validation ----------
[[ -n "$REGION" ]] || err "--region is required"
[[ -n "$PROFILE" ]] && export AWS_PROFILE="$PROFILE"
command -v aws >/dev/null 2>&1 || err "aws CLI not found in PATH"

if [[ -z "$ACCOUNT_ID" ]]; then
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
    || err "Could not resolve account ID from caller identity; pass --account-id."
fi
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || err "account-id must be 12 digits (got '$ACCOUNT_ID')"

case "$SCOPE" in
  account) ;;
  role)
    [[ -n "$ROLE" ]] || err "--role is required when --scope=role"
    ;;
  user)
    [[ -n "$USER_NAME"  ]] || err "--user-name is required when --scope=user"
    [[ -n "$USER_ROLE"  ]] || err "--user-role is required when --scope=user"
    [[ -n "$USER_EMAIL" ]] || err "--user-email is required when --scope=user"
    ;;
  *) err "--scope must be one of: account, role, user" ;;
esac

# ---------- detach ----------
case "$SCOPE" in
  account)
    log "Detaching account-level custom permissions."
    aws quicksight delete-account-custom-permission \
      --aws-account-id "$ACCOUNT_ID" \
      --region "$REGION" >/dev/null \
      || log "  (no account-level custom permissions to remove, continuing)"
    ;;
  role)
    log "Detaching custom permissions from role '$ROLE' in namespace '$NAMESPACE'."
    aws quicksight delete-role-custom-permission \
      --aws-account-id "$ACCOUNT_ID" \
      --region "$REGION" \
      --namespace "$NAMESPACE" \
      --role "$ROLE" >/dev/null \
      || log "  (no role-level custom permissions to remove, continuing)"
    ;;
  user)
    log "Detaching custom permissions from user '$USER_NAME' in namespace '$NAMESPACE'."
    # update-user requires --role and --email; --unapply-custom-permissions clears the profile.
    aws quicksight update-user \
      --aws-account-id "$ACCOUNT_ID" \
      --region "$REGION" \
      --namespace "$NAMESPACE" \
      --user-name "$USER_NAME" \
      --role "$USER_ROLE" \
      --email "$USER_EMAIL" \
      --unapply-custom-permissions >/dev/null
    ;;
esac

# ---------- optionally delete the profile ----------
if [[ "$DELETE_PROFILE" == "true" ]]; then
  log "Deleting custom permissions profile '$PROFILE_NAME'."
  aws quicksight delete-custom-permissions \
    --aws-account-id "$ACCOUNT_ID" \
    --region "$REGION" \
    --custom-permissions-name "$PROFILE_NAME" >/dev/null \
    || log "  (profile not found or still referenced -- skipping delete)"
fi

log "Done."
