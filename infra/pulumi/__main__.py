"""pmt AWS infrastructure — eu-west-1."""

import pulumi

from lambda_proxy import lambda_function, function_url, log_group
from github_oidc import ci_role, oidc_provider

pulumi.export("pmproxy_lambda_arn", lambda_function.arn)
pulumi.export("pmproxy_url", function_url.function_url)
pulumi.export("pmproxy_log_group", log_group.name)
pulumi.export("github_oidc_provider_arn", oidc_provider.arn)
pulumi.export("ci_deploy_role_arn", ci_role.arn)
