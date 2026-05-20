"""Cognito User Pool + App Client for pmproxy multi-tenant authentication.

Originally provisioned by CFN stack `pmproxy-cognito` (2026-01-18). Imported
into Pulumi 2026-05-20; the CFN stack was deleted with `--retain-resources`
so the physical Cognito resources continued uninterrupted.

Use `custom:tenant_tier` (Free | Pro | Enterprise) on user records to drive
the rate limit tier in pmproxy.

Resources are marked `protect=True` — deleting them would revoke all user
access. To destroy intentionally, drop the protect flag and run
`pulumi up` before `pulumi destroy`.
"""

import pulumi
import pulumi_aws as aws

user_pool = aws.cognito.UserPool(
    "pmproxy-userpool",
    name="pmproxy-prod",
    auto_verified_attributes=["email"],
    username_attributes=["email"],
    mfa_configuration="OFF",
    user_pool_tier="ESSENTIALS",
    password_policy=aws.cognito.UserPoolPasswordPolicyArgs(
        minimum_length=12,
        require_lowercase=True,
        require_numbers=True,
        require_symbols=False,
        require_uppercase=True,
        temporary_password_validity_days=7,
    ),
    schemas=[
        aws.cognito.UserPoolSchemaArgs(
            name="tenant_tier",
            attribute_data_type="String",
            mutable=True,
            string_attribute_constraints=aws.cognito.UserPoolSchemaStringAttributeConstraintsArgs(
                min_length="0",
                max_length="20",
            ),
        ),
    ],
    admin_create_user_config=aws.cognito.UserPoolAdminCreateUserConfigArgs(
        allow_admin_create_user_only=True,
    ),
    account_recovery_setting=aws.cognito.UserPoolAccountRecoverySettingArgs(
        recovery_mechanisms=[
            aws.cognito.UserPoolAccountRecoverySettingRecoveryMechanismArgs(
                name="verified_email",
                priority=1,
            ),
        ],
    ),
    email_configuration=aws.cognito.UserPoolEmailConfigurationArgs(
        email_sending_account="COGNITO_DEFAULT",
    ),
    sign_in_policy=aws.cognito.UserPoolSignInPolicyArgs(
        allowed_first_auth_factors=["PASSWORD"],
    ),
    verification_message_template=aws.cognito.UserPoolVerificationMessageTemplateArgs(
        default_email_option="CONFIRM_WITH_CODE",
    ),
    opts=pulumi.ResourceOptions(protect=True),
)

user_pool_client = aws.cognito.UserPoolClient(
    "pmproxy-userpool-client",
    name="pmproxy-client-prod",
    user_pool_id=user_pool.id,
    # generate_secret omitted intentionally — setting it (even to False, the default)
    # is treated as a force-new change vs the imported state, which left it null.
    explicit_auth_flows=[
        "ALLOW_REFRESH_TOKEN_AUTH",
        "ALLOW_USER_PASSWORD_AUTH",
    ],
    prevent_user_existence_errors="ENABLED",
    access_token_validity=1,
    id_token_validity=1,
    refresh_token_validity=30,
    auth_session_validity=3,
    enable_token_revocation=True,
    opts=pulumi.ResourceOptions(protect=True),
)
