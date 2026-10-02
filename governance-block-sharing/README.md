# Amazon Quick — Block Sharing

> [!IMPORTANT]
> **Sample code — review and adapt before production use.** Not an AWS service and not supported by AWS; see the [repository disclaimer](../README.md#disclaimer).

Prevent users in an **Amazon Quick (QuickSight)** Enterprise account from sharing selected assets while still allowing them to create and use those assets. The module creates a Quick **custom permissions profile** with sharing capabilities set to `DENY`, then assigns that profile at account, role, or user scope.

## Business capability

The default policy means:

> Users may create and consume governed content, but they may not expand who can access Chat Agents, Spaces, or Datasets.

| Default capability | Business effect |
|---|---|
| `ShareChatAgents: DENY` | Prevents future sharing of custom Chat Agents. |
| `ShareSpaces: DENY` | Prevents future sharing of Spaces. |
| `ShareDatasets: DENY` | Prevents future sharing of datasets and their underlying business data. |

Optional denials can also cover dashboards, analyses, and data sources. Custom permissions only restrict capabilities; they never grant access.

## Important limitations

- **Existing shares remain:** the policy blocks future share actions but does not revoke permissions already granted.
- **Creation and use remain available:** the module denies the `Share*` capability, not the parent `ChatAgent` or `Space` capability.
- **Not complete egress prevention:** exports, downloads, screenshots, and other channels require separate controls.
- **Enterprise Edition is required.**
- **No group scope:** Quick custom permissions can be assigned to an account, role, or user, but not directly to a Quick group. For a group-like population, use a suitable Quick role or automate user-level assignments.
- **Scope precedence matters:** a user-level assignment is more specific than a role-level assignment, which is more specific than an account-level assignment.

## Module layout

```text
governance-block-sharing/
├── README.md
├── cloudformation/
│   └── governance-block-sharing.yaml
└── scripts/
    ├── apply-governance-block-sharing.sh
    └── remove-governance-block-sharing.sh
```

## Understand the different “default” terms

These concepts are independent:

| Term | Meaning |
|---|---|
| `--profile default` | Selects the local AWS CLI credential profile. It does not control Quick permissions. |
| `--namespace default` | Selects the standard Quick identity namespace for role/user operations. It is not account-wide scope. |
| `--scope account` | Assigns the custom permissions profile to the whole Quick account. |
| `CustomPermissionsName: Default` | The built-in/default account permissions assignment shown when no account-level custom profile is applied. |
| `quick-governance-block-sharing-profile` | The module’s default, specifically named custom permissions profile. |

## Choose the correct Region

Use the account’s **Quick capacity/home Region**, not necessarily `us-east-1`. This repository’s current account reports `sa-east-1`; examples below therefore use `sa-east-1`.

Using the wrong endpoint produces an error similar to:

```text
Operation is being called from endpoint us-east-1, but your capacity region is
sa-east-1. Please use the sa-east-1 endpoint.
```

In that case, rerun the command with the Region named in the error. The scripts are idempotent.

## What gets created

| Deployment path | Creates | Assigns the profile? |
|---|---|---|
| Apply script | A Quick custom permissions profile through Quick APIs | Yes: account, role, or user scope |
| CloudFormation | `AWS::QuickSight::CustomPermissions` | No: run a Quick API/CLI assignment afterward |

The recommended apply script performs both steps. Because it calls Quick APIs directly, it does **not** create a CloudFormation stack.

Default names:

| Item | Default |
|---|---|
| Profile | `quick-governance-block-sharing-profile` |
| Purpose tag | `quick-governance-block-sharing` |
| CloudFormation stack, if that path is used | `quick-governance-block-sharing` |

## Prerequisites

- Amazon Quick Enterprise Edition in the target account.
- AWS CLI v2 authenticated to the target account.
- `jq` available on `PATH` for the apply script.
- The account’s Quick capacity Region.
- Caller permissions for the operations being used:
  - `quicksight:CreateCustomPermissions`
  - `quicksight:UpdateCustomPermissions`
  - `quicksight:DescribeCustomPermissions`
  - `quicksight:DeleteCustomPermissions` for profile deletion
  - `quicksight:UpdateAccountCustomPermission` / `DeleteAccountCustomPermission`
  - `quicksight:UpdateRoleCustomPermission` / `DeleteRoleCustomPermission`
  - `quicksight:UpdateUser` for user assignment/removal
  - `sts:GetCallerIdentity` when `--account-id` is omitted

## Recommended deployment: apply script

Run commands from `governance-block-sharing/scripts`, or prefix the script path when running from the repository root.

### Account-wide

This is the script default and affects all users who do not have a more-specific assignment:

```bash
./apply-governance-block-sharing.sh \
  --region sa-east-1 \
  --profile default \
  --scope account
```

### One Quick role

Apply to all users with a specific Quick role:

```bash
./apply-governance-block-sharing.sh \
  --region sa-east-1 \
  --profile default \
  --scope role \
  --role AUTHOR \
  --namespace default
```

Supported role values are `ADMIN`, `ADMIN_PRO`, `AUTHOR`, `AUTHOR_PRO`, `READER`, and `READER_PRO`. Run the command once for each role that should receive the profile.

### One user

Apply to a single Quick user:

```bash
./apply-governance-block-sharing.sh \
  --region sa-east-1 \
  --profile default \
  --scope user \
  --namespace default \
  --user-name user@example.com \
  --user-role AUTHOR \
  --user-email user@example.com
```

The script uses `UpdateUser`; `--user-role` and `--user-email` must match the user’s intended current values so that the assignment does not unintentionally change them.

### Additional denied asset types

Add optional sharing restrictions as a comma-separated list:

```bash
./apply-governance-block-sharing.sh \
  --region sa-east-1 \
  --profile default \
  --scope role \
  --role AUTHOR \
  --namespace default \
  --extra-deny ShareDashboards,ShareAnalyses,ShareDataSources
```

The three core denials—`ShareChatAgents`, `ShareSpaces`, and `ShareDatasets`—are always included. Unknown capability names are rejected by the Quick API.

### Use a different custom profile name

```bash
./apply-governance-block-sharing.sh \
  --region sa-east-1 \
  --profile default \
  --profile-name finance-block-sharing \
  --scope role \
  --role AUTHOR \
  --namespace default
```

Use the same `--profile-name` during verification and removal.

## Apply-script options

| Option | Required | Default | Description |
|---|---:|---|---|
| `--region REGION` | Yes | None | Quick capacity/home Region, such as `sa-east-1`. |
| `--profile PROFILE` | No | AWS CLI default credential resolution | Named local AWS CLI credential profile. |
| `--account-id ID` | No | Caller account from STS | Explicit 12-digit target AWS account ID. |
| `--profile-name NAME` | No | `quick-governance-block-sharing-profile` | Custom permissions profile to create or update. Valid pattern: `^[a-zA-Z0-9+=,.@_-]+$`. |
| `--scope SCOPE` | No | `account` | Assignment scope: `account`, `role`, or `user`. |
| `--role ROLE` | For role scope | None | Quick role receiving the profile. |
| `--namespace NAMESPACE` | For role/user targeting when non-default | `default` | Quick identity namespace. |
| `--user-name NAME` | For user scope | None | Existing Quick user name. |
| `--user-role ROLE` | For user scope | None | Role supplied to `UpdateUser`; use the user’s intended role. |
| `--user-email EMAIL` | For user scope | None | Email supplied to `UpdateUser`; use the user’s intended email. |
| `--extra-deny LIST` | No | Empty | Comma-separated additional capabilities to set to `DENY`. |
| `-h`, `--help` | No | — | Prints script usage. |

The script is idempotent: it creates the profile when absent and updates it when present, then asserts the requested assignment.

## Safely change scope

To move from account-wide enforcement to a narrower role or user scope, assign the narrower scope first, then detach the account assignment. This avoids a protection gap for the intended population.

Example: move from account scope to Authors only:

```bash
# 1. Assign Authors.
./apply-governance-block-sharing.sh \
  --region sa-east-1 --profile default \
  --scope role --role AUTHOR --namespace default

# 2. Remove account-wide assignment, retaining the reusable profile.
./remove-governance-block-sharing.sh \
  --region sa-east-1 --profile default \
  --scope account
```

Do not pass `--delete-profile` while another role or user still references the profile.

## Verify

Set reusable values:

```bash
REGION=sa-east-1
AWS_PROFILE=default
ACCOUNT_ID="$(aws sts get-caller-identity \
  --profile "$AWS_PROFILE" --query Account --output text)"
PROFILE_NAME=quick-governance-block-sharing-profile
```

### Verify profile capabilities

```bash
aws quicksight describe-custom-permissions \
  --aws-account-id "$ACCOUNT_ID" \
  --custom-permissions-name "$PROFILE_NAME" \
  --region "$REGION" \
  --profile "$AWS_PROFILE" \
  --query 'CustomPermissions.{Name:CustomPermissionsName,Capabilities:Capabilities}'
```

Expected core capabilities:

```json
{
  "ShareChatAgents": "DENY",
  "ShareSpaces": "DENY",
  "ShareDatasets": "DENY"
}
```

### Verify account assignment

```bash
aws quicksight describe-account-custom-permission \
  --aws-account-id "$ACCOUNT_ID" \
  --region "$REGION" \
  --profile "$AWS_PROFILE"
```

For account-wide enforcement, `CustomPermissionsName` should be `quick-governance-block-sharing-profile`, not `Default`.

For role/user scope, use the Quick admin console’s **Check permissions** function for a representative user and confirm which profile and scope are effective. Then test as a covered user: Share should be unavailable or the request should fail with a permissions error. CloudTrail records failed Quick permission-change attempts.

## Remove or roll back

The remove script detaches the profile from the specified scope. It leaves the profile available for reuse unless `--delete-profile` is supplied.

### Detach account-wide policy

```bash
./remove-governance-block-sharing.sh \
  --region sa-east-1 \
  --profile default \
  --scope account
```

### Detach from a role

```bash
./remove-governance-block-sharing.sh \
  --region sa-east-1 \
  --profile default \
  --scope role \
  --role AUTHOR \
  --namespace default
```

### Detach from a user

```bash
./remove-governance-block-sharing.sh \
  --region sa-east-1 \
  --profile default \
  --scope user \
  --namespace default \
  --user-name user@example.com \
  --user-role AUTHOR \
  --user-email user@example.com
```

### Detach and delete the profile

Only delete after it has been detached from every scope:

```bash
./remove-governance-block-sharing.sh \
  --region sa-east-1 \
  --profile default \
  --scope account \
  --delete-profile
```

### Remove-script options

The remove script accepts `--region`, `--profile`, `--account-id`, `--profile-name`, `--scope`, `--role`, `--namespace`, `--user-name`, `--user-role`, and `--user-email` with the same meanings as the apply script. It also accepts:

| Option | Default | Description |
|---|---|---|
| `--delete-profile` | Off | Deletes the custom permissions profile after detaching the selected scope. Omit it when the profile is still assigned elsewhere or should be reused. |

## Alternative deployment: CloudFormation

CloudFormation creates and manages only the profile. Assignment remains a separate Quick API operation.

```bash
aws cloudformation deploy \
  --region sa-east-1 \
  --profile default \
  --stack-name quick-governance-block-sharing \
  --template-file cloudformation/governance-block-sharing.yaml
```

Then assign it, for example account-wide:

```bash
ACCOUNT_ID="$(aws sts get-caller-identity \
  --profile default --query Account --output text)"

aws quicksight update-account-custom-permission \
  --aws-account-id "$ACCOUNT_ID" \
  --custom-permissions-name quick-governance-block-sharing-profile \
  --region sa-east-1 \
  --profile default
```

Avoid managing the same profile name simultaneously through both the direct apply script and CloudFormation; direct updates can create configuration drift from the stack.

### CloudFormation parameters

| Parameter | Default | Description |
|---|---|---|
| `CustomPermissionsName` | `quick-governance-block-sharing-profile` | Profile name, 1–64 characters, matching `^[a-zA-Z0-9+=,.@_-]+$`. |
| `AlsoBlockDashboardSharing` | `false` | Adds `ShareDashboards: DENY` when `true`. |
| `AlsoBlockAnalysesSharing` | `false` | Adds `ShareAnalyses: DENY` when `true`. |
| `AlsoBlockDataSourceSharing` | `false` | Adds `ShareDataSources: DENY` when `true`. |

Example with every optional denial:

```bash
aws cloudformation deploy \
  --region sa-east-1 \
  --profile default \
  --stack-name quick-governance-block-sharing \
  --template-file cloudformation/governance-block-sharing.yaml \
  --parameter-overrides \
    AlsoBlockDashboardSharing=true \
    AlsoBlockAnalysesSharing=true \
    AlsoBlockDataSourceSharing=true
```

CloudFormation outputs the profile ARN, profile name, and an account-wide attachment command. Deleting the stack deletes the managed profile only when no external assignment prevents deletion; detach assignments first.

## Existing-share remediation

This policy does not revoke existing permissions. To remove existing shares, inventory each asset type with its `Describe*Permissions` API and revoke principals with the corresponding `Update*Permissions` API. Treat this as a separate, reviewed remediation because it changes current user access.

## Cost

There is no separate runtime or per-request charge for the custom permissions configuration beyond the existing Amazon Quick subscription.

## References

- [Custom permissions in Amazon Quick](https://docs.aws.amazon.com/quick/latest/userguide/custom-permissions.html)
- [Creating a custom permissions profile](https://docs.aws.amazon.com/quick/latest/userguide/create-custom-permissions-profile.html)
- [`UpdateCustomPermissions` API](https://docs.aws.amazon.com/quicksight/latest/APIReference/API_UpdateCustomPermissions.html)
- [`AWS::QuickSight::CustomPermissions`](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-quicksight-custompermissions.html)
- [Establishing enterprise governance using custom permissions](https://aws.amazon.com/blogs/business-intelligence/establishing-enterprise-governance-in-amazon-quick-using-custom-permissions/)
- [Automate governance of Amazon Quick features using custom permissions](https://aws.amazon.com/blogs/business-intelligence/automate-governance-of-amazon-quick-suite-features-using-custom-permissions/)
