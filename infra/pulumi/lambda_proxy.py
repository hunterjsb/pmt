"""pmproxy Lambda + Function URL (eu-west-1).

Build artifact: pmproxy/target/lambda/pmproxy-lambda/bootstrap.zip
Built via: cd pmproxy && cargo lambda build --release --features lambda --bin pmproxy-lambda --output-format zip
"""

import os

import pulumi
import pulumi_aws as aws

cfg = pulumi.Config("pmt")
cognito_pool_id = cfg.require("cognitoPoolId")
cognito_client_id = cfg.require("cognitoClientId")
budget_email = cfg.get("budgetEmail") or "hunterjsb@gmail.com"

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LAMBDA_ZIP = os.path.join(
    REPO_ROOT, "pmproxy", "target", "lambda", "pmproxy-lambda", "bootstrap.zip"
)

exec_role = aws.iam.Role(
    "pmproxy-lambda-role",
    name="pmproxy-lambda-role",
    assume_role_policy="""{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }""",
)

aws.iam.RolePolicyAttachment(
    "pmproxy-lambda-logs",
    role=exec_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
)

log_group = aws.cloudwatch.LogGroup(
    "pmproxy-logs",
    name="/aws/lambda/pmproxy",
    retention_in_days=7,
)

lambda_function = aws.lambda_.Function(
    "pmproxy",
    name="pmproxy",
    role=exec_role.arn,
    runtime="provided.al2023",
    handler="bootstrap",
    architectures=["x86_64"],
    code=pulumi.FileArchive(LAMBDA_ZIP),
    memory_size=256,
    timeout=30,
    # Hard cost cap: even if abused, can't exceed 10 concurrent invocations.
    # At 256MB / 30s max, worst case ~$0.0001 per concurrent-second = ~$2.60/mo if pegged 24/7.
    reserved_concurrent_executions=10,
    environment=aws.lambda_.FunctionEnvironmentArgs(
        variables={
            "PMPROXY_AUTH_ENABLED": "true",  # require Cognito JWT on /clob /gamma /chain
            "PMPROXY_COGNITO_REGION": "eu-west-1",
            "PMPROXY_COGNITO_POOL_ID": cognito_pool_id,
            "PMPROXY_COGNITO_APP_CLIENT_ID": cognito_client_id,
            "RUST_LOG": "info",
        },
    ),
    # CI deploys (.github/workflows/deploy-pmproxy.yml) own the function code via
    # `aws lambda update-function-code`. Pulumi owns the configuration only — telling
    # it to ignore "code" prevents `pulumi up` from clobbering a fresh CI deploy with
    # whatever zip happens to be on the local disk.
    opts=pulumi.ResourceOptions(depends_on=[log_group], ignore_changes=["code"]),
)

function_url = aws.lambda_.FunctionUrl(
    "pmproxy-url",
    function_name=lambda_function.name,
    authorization_type="NONE",  # pmproxy validates Cognito JWT itself in-process
    cors=aws.lambda_.FunctionUrlCorsArgs(
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        max_age=86400,
    ),
)

aws.lambda_.Permission(
    "pmproxy-url-public-invoke",
    action="lambda:InvokeFunctionUrl",
    function=lambda_function.name,
    principal="*",
    function_url_auth_type="NONE",
    statement_id="FunctionURLAllowPublicAccess",
)

# Since October 2025, public Function URLs also require lambda:InvokeFunction
# scoped via the InvokedViaFunctionUrl context key. Without this, all
# anonymous calls return 403 AccessDeniedException. (Docs: urls-auth.html)
aws.lambda_.Permission(
    "pmproxy-url-public-invoke-function",
    action="lambda:InvokeFunction",
    function=lambda_function.name,
    principal="*",
    statement_id="FunctionURLAllowPublicInvokeFunction",
)

# SNS topic for all pmproxy ops alarms. One subscription = one place to look
# when something pages. Budget alarms (below) hit email directly without SNS,
# but CloudWatch alarms need a topic to publish to.
alarm_topic = aws.sns.Topic(
    "pmproxy-alarms",
    name="pmproxy-alarms",
    display_name="pmproxy ops alarms",
)

aws.sns.TopicSubscription(
    "pmproxy-alarms-email",
    topic=alarm_topic.arn,
    protocol="email",
    endpoint=budget_email,
)

# --- alarms ---
# All follow the same shape: notBreaching when there's no data (avoids
# alarming during idle hours), 2 consecutive evaluation periods to ride
# out transient spikes, and a description that names the playbook step.

# 4xx rate: brute-force tokens / scanning. >20 in 5min for 2 periods.
alarm_4xx = aws.cloudwatch.MetricAlarm(
    "pmproxy-4xx-alarm",
    name="pmproxy-high-4xx",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=2,
    metric_name="Url4xxCount",
    namespace="AWS/Lambda",
    period=300,
    statistic="Sum",
    threshold=20,
    alarm_description="pmproxy Function URL is returning >20 4xx/5min — possible auth abuse",
    dimensions={"FunctionName": lambda_function.name},
    treat_missing_data="notBreaching",
    alarm_actions=[alarm_topic.arn],
    ok_actions=[alarm_topic.arn],
)

# 5xx rate: Lambda or upstream failures. Any sustained 5xx is a P1.
alarm_5xx = aws.cloudwatch.MetricAlarm(
    "pmproxy-5xx-alarm",
    name="pmproxy-high-5xx",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=2,
    metric_name="Url5xxCount",
    namespace="AWS/Lambda",
    period=300,
    statistic="Sum",
    threshold=5,  # 5 5xx in 5min sustained = something's wrong
    alarm_description="pmproxy returning >5 5xx/5min — see CloudWatch logs",
    dimensions={"FunctionName": lambda_function.name},
    treat_missing_data="notBreaching",
    alarm_actions=[alarm_topic.arn],
    ok_actions=[alarm_topic.arn],
)

# Lambda function errors (uncaught panics, init failures, etc.).
alarm_errors = aws.cloudwatch.MetricAlarm(
    "pmproxy-errors-alarm",
    name="pmproxy-function-errors",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="Errors",
    namespace="AWS/Lambda",
    period=300,
    statistic="Sum",
    threshold=3,
    alarm_description="pmproxy Lambda crashed >3 times/5min — investigate runtime errors",
    dimensions={"FunctionName": lambda_function.name},
    treat_missing_data="notBreaching",
    alarm_actions=[alarm_topic.arn],
    ok_actions=[alarm_topic.arn],
)

# Throttles: reserved concurrency cap hit. If this fires we either have
# legitimate traffic growth (raise the cap) or runaway invocations (find them).
alarm_throttles = aws.cloudwatch.MetricAlarm(
    "pmproxy-throttles-alarm",
    name="pmproxy-throttles",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=1,
    metric_name="Throttles",
    namespace="AWS/Lambda",
    period=300,
    statistic="Sum",
    threshold=10,  # 10 throttles/5min = either growth or attack
    alarm_description="pmproxy throttled >10 times/5min — at reserved concurrency cap",
    dimensions={"FunctionName": lambda_function.name},
    treat_missing_data="notBreaching",
    alarm_actions=[alarm_topic.arn],
    ok_actions=[alarm_topic.arn],
)

# Duration p99: latency regression. Lambda emits per-invocation duration;
# we alarm on p99 > 5s (proxy passes are usually <1s).
alarm_latency = aws.cloudwatch.MetricAlarm(
    "pmproxy-latency-alarm",
    name="pmproxy-p99-latency",
    comparison_operator="GreaterThanThreshold",
    evaluation_periods=3,
    metric_name="Duration",
    namespace="AWS/Lambda",
    period=300,
    extended_statistic="p99",
    threshold=5000,  # ms
    alarm_description="pmproxy p99 latency >5s for 15min — check upstream / cold-start storms",
    dimensions={"FunctionName": lambda_function.name},
    treat_missing_data="notBreaching",
    alarm_actions=[alarm_topic.arn],
    ok_actions=[alarm_topic.arn],
)

# Budget: alert at $5/mo of Lambda spend. AWS Budgets emails directly, no SNS needed.
budget = aws.budgets.Budget(
    "pmproxy-lambda-budget",
    name="pmproxy-lambda",
    budget_type="COST",
    limit_amount="5",
    limit_unit="USD",
    time_unit="MONTHLY",
    cost_filters=[
        aws.budgets.BudgetCostFilterArgs(
            name="Service",
            values=["AWS Lambda"],
        ),
    ],
    notifications=[
        aws.budgets.BudgetNotificationArgs(
            comparison_operator="GREATER_THAN",
            threshold=80,  # 80% of budget = $4
            threshold_type="PERCENTAGE",
            notification_type="ACTUAL",
            subscriber_email_addresses=[budget_email],
        ),
        aws.budgets.BudgetNotificationArgs(
            comparison_operator="GREATER_THAN",
            threshold=100,  # hit budget
            threshold_type="PERCENTAGE",
            notification_type="ACTUAL",
            subscriber_email_addresses=[budget_email],
        ),
    ],
)
