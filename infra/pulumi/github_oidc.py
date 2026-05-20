"""GitHub Actions OIDC trust for CI-driven Lambda deploys.

Creates an OIDC provider (one per AWS account) and a single IAM role that GitHub
Actions can assume to:
  - Update the pmproxy Lambda function code
  - Read/write the Pulumi state bucket
  - Read CloudWatch log groups

Trust is scoped to this repo only, on master pushes and pmproxy-v* tags.
"""

import json

import pulumi
import pulumi_aws as aws

REPO = "hunterjsb/pmt"
PULUMI_STATE_BUCKET = "pmt-pulumi-state-350985642081-euw1"

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

# Permissions: Lambda update on the pmproxy function, S3 R/W on Pulumi state bucket,
# IAM PassRole for the Lambda exec role, CloudWatch log read.
deploy_policy = aws.iam.RolePolicy(
    "pmproxy-ci-deploy-policy",
    role=ci_role.id,
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "LambdaManage",
                    "Effect": "Allow",
                    "Action": [
                        # Reads (Pulumi provider performs these during refresh)
                        "lambda:GetFunction",
                        "lambda:GetFunctionConfiguration",
                        "lambda:GetFunctionUrlConfig",
                        "lambda:GetFunctionCodeSigningConfig",
                        "lambda:GetFunctionEventInvokeConfig",
                        "lambda:GetFunctionConcurrency",
                        "lambda:GetPolicy",
                        "lambda:GetAlias",
                        "lambda:ListVersionsByFunction",
                        "lambda:ListAliases",
                        "lambda:ListTags",
                        "lambda:ListFunctionUrlConfigs",
                        "lambda:ListFunctionEventInvokeConfigs",
                        # Mutations
                        "lambda:UpdateFunctionCode",
                        "lambda:UpdateFunctionConfiguration",
                        "lambda:UpdateFunctionUrlConfig",
                        "lambda:PublishVersion",
                        "lambda:PutFunctionConcurrency",
                        "lambda:AddPermission",
                        "lambda:RemovePermission",
                        "lambda:TagResource",
                        "lambda:UntagResource",
                    ],
                    "Resource": "arn:aws:lambda:eu-west-1:350985642081:function:pmproxy*",
                },
                {
                    "Sid": "PulumiStateBucket",
                    "Effect": "Allow",
                    "Action": [
                        "s3:GetObject",
                        "s3:PutObject",
                        "s3:DeleteObject",
                        "s3:ListBucket",
                        "s3:GetBucketLocation",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::{PULUMI_STATE_BUCKET}",
                        f"arn:aws:s3:::{PULUMI_STATE_BUCKET}/*",
                    ],
                },
                {
                    "Sid": "PassLambdaExecRole",
                    "Effect": "Allow",
                    "Action": "iam:PassRole",
                    "Resource": "arn:aws:iam::350985642081:role/pmproxy-lambda-role",
                },
                {
                    "Sid": "ReadIamForPulumiRefresh",
                    "Effect": "Allow",
                    "Action": [
                        "iam:GetRole",
                        "iam:GetRolePolicy",
                        "iam:ListAttachedRolePolicies",
                        "iam:GetOpenIDConnectProvider",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "ReadLogGroup",
                    "Effect": "Allow",
                    "Action": [
                        "logs:DescribeLogGroups",
                        "logs:DescribeLogStreams",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "ReadCloudWatchAlarms",
                    "Effect": "Allow",
                    "Action": [
                        "cloudwatch:DescribeAlarms",
                        "cloudwatch:GetMetricStatistics",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "ReadBudgets",
                    "Effect": "Allow",
                    "Action": [
                        "budgets:DescribeBudget",
                        "budgets:ViewBudget",
                    ],
                    "Resource": "*",
                },
            ],
        }
    ),
)
