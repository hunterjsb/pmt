"""pmt AWS infrastructure — eu-west-1."""

import pulumi

from lambda_proxy import alarm_topic, function_url, lambda_function, log_group
from github_oidc import ci_role, oidc_provider
from cognito import user_pool, user_pool_client

pulumi.export("pmproxy_lambda_arn", lambda_function.arn)
pulumi.export("pmproxy_url", function_url.function_url)
pulumi.export("pmproxy_log_group", log_group.name)
pulumi.export("pmproxy_alarm_topic_arn", alarm_topic.arn)
pulumi.export("github_oidc_provider_arn", oidc_provider.arn)
pulumi.export("ci_deploy_role_arn", ci_role.arn)
pulumi.export("cognito_user_pool_id", user_pool.id)
pulumi.export("cognito_user_pool_arn", user_pool.arn)
pulumi.export("cognito_user_pool_client_id", user_pool_client.id)
