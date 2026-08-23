"""GitHub Actions OIDC trust for CI-driven Lambda deploys.

Creates an OIDC provider (one per AWS account) and a single IAM role that GitHub
Actions can assume to push new pmproxy Lambda code via
`aws lambda update-function-code`. Pulumi-managed infra changes stay manual.

Trust is scoped to this repo only, on master pushes and pmproxy-v* tags.

!! Both resources below EXIST IN THE ACCOUNT but are NOT in any Pulumi state —
   the state bucket is gone (see README.md). The OIDC provider was created
   2026-05-20; the role was recreated by hand via AWS CLI on 2026-08-23 after
   the teardown took it out. Values here were written to match the live
   resources exactly so a post-import `pulumi up` is a no-op.
"""

import json

import pulumi
import pulumi_aws as aws

# The repo moved to the pm-trade org; the old value here would have built a
# trust policy no GitHub token could ever satisfy.
REPO = "pm-trade/pmt"

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
# These are exactly the two `sub` claims deploy-pmproxy.yml can present:
#   push to master / workflow_dispatch on master → ref:refs/heads/master
#   release published (pmproxy-v*)               → ref:refs/tags/pmproxy-v*
# No `environment:` entry — the deploy job declares no GitHub environment, so an
# environment sub can never arrive and listing one only widens the trust.
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
    description="Role assumed by GitHub Actions (pm-trade/pmt) to deploy pmproxy Lambda code",
    max_session_duration=3600,
    assume_role_policy=trust_policy,
)

# Permissions for code-only CI deploys via `aws lambda update-function-code`.
# CI does NOT run Pulumi (config changes stay manual), so this role has no S3
# state-bucket access, no IAM PassRole, no IAM/Logs/Budget reads.
deploy_policy = aws.iam.RolePolicy(
    "pmproxy-ci-deploy-policy",
    # Explicit name — Pulumi would otherwise append a random suffix and no longer
    # match the hand-created inline policy of the same name.
    name="pmproxy-ci-deploy-policy",
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
                {
                    # The Function URL is AWS_IAM now, so the post-deploy smoke
                    # tests have to SigV4-sign — which needs an explicit invoke
                    # grant. Conditioned on AWS_IAM so this can never become a
                    # grant against a URL someone re-opened to AuthType=NONE.
                    "Sid": "PostDeploySmokeTest",
                    "Effect": "Allow",
                    "Action": "lambda:InvokeFunctionUrl",
                    "Resource": "arn:aws:lambda:eu-west-1:350985642081:function:pmproxy",
                    "Condition": {
                        "StringEquals": {"lambda:FunctionUrlAuthType": "AWS_IAM"}
                    },
                },
            ],
        }
    ),
)
