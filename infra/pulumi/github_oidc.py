"""GitHub Actions OIDC trust for CI-driven Lambda deploys.

Creates an OIDC provider (one per AWS account) and a single IAM role that GitHub
Actions can assume to push new pmproxy Lambda code via
`aws lambda update-function-code`. Pulumi-managed infra changes stay manual.

Trust is scoped to this repo only, on master pushes and pmproxy-v* tags.
"""

import json

import pulumi
import pulumi_aws as aws

REPO = "hunterjsb/pmt"

# OIDC provider for GitHub Actions. One per AWS account; safe to import if pre-existing.
oidc_provider = aws.iam.OpenIdConnectProvider(
    "github-actions-oidc",
    url="https://token.actions.githubusercontent.com",
    client_id_lists=["sts.amazonaws.com"],
    # Thumbprint of GitHub's OIDC cert. AWS still requires this field but no longer
    # validates it; STS uses the JWT signature against the JWKS endpoint instead.
    thumbprint_lists=["6938fd4d98bab03faadb97b34396831e3780aea1"],
)

# Trust policy: only this repo, only master branch + pmproxy-v* tags.
trust_policy = oidc_provider.arn.apply(
    lambda arn: json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Federated": arn},
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Condition": {
                        "StringEquals": {
                            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                        },
                        "StringLike": {
                            "token.actions.githubusercontent.com:sub": [
                                f"repo:{REPO}:ref:refs/heads/master",
                                f"repo:{REPO}:ref:refs/tags/pmproxy-v*",
                                f"repo:{REPO}:environment:prod",
                            ]
                        },
                    },
                }
            ],
        }
    )
)

ci_role = aws.iam.Role(
    "pmproxy-ci-deploy",
    name="pmproxy-ci-deploy",
    description="Role assumed by GitHub Actions to deploy pmproxy Lambda",
    assume_role_policy=trust_policy,
)

# Permissions for code-only CI deploys via `aws lambda update-function-code`.
# CI does NOT run Pulumi (config changes stay manual), so this role has no S3
# state-bucket access, no IAM PassRole, no IAM/Logs/Budget reads.
deploy_policy = aws.iam.RolePolicy(
    "pmproxy-ci-deploy-policy",
    role=ci_role.id,
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "LambdaCodeDeploy",
                    "Effect": "Allow",
                    "Action": [
                        # Read for the verify + wait steps
                        "lambda:GetFunction",
                        "lambda:GetFunctionConfiguration",
                        "lambda:GetFunctionUrlConfig",
                        # Mutate (only the code, plus publish a version)
                        "lambda:UpdateFunctionCode",
                        "lambda:PublishVersion",
                    ],
                    "Resource": "arn:aws:lambda:eu-west-1:350985642081:function:pmproxy",
                },
            ],
        }
    ),
)
