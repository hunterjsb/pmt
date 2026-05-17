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
    opts=pulumi.ResourceOptions(depends_on=[log_group]),
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

# Alarm: high 4xx rate suggests someone is brute-forcing tokens or scanning.
# Trips if >20 4xx responses in a 5-min window for 2 consecutive periods.
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
