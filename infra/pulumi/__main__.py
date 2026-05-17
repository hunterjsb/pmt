"""pmt AWS infrastructure — eu-west-1."""

import pulumi

from lambda_proxy import lambda_function, function_url, log_group

pulumi.export("pmproxy_lambda_arn", lambda_function.arn)
pulumi.export("pmproxy_url", function_url.function_url)
pulumi.export("pmproxy_log_group", log_group.name)
