# pmt infra (Pulumi)

Python Pulumi project describing pmt AWS infra in **eu-west-1**.

---

## STOP — THE STATE IS GONE. DO NOT RUN `pulumi up`.

The backend bucket `s3://pmt-pulumi-state-350985642081-euw1` **no longer exists**
(verified 2026-08-23: `head-bucket` → 404; the account holds no `pmt-pulumi-*`
bucket at all). It was destroyed along with most of the pmt footprint sometime
between June and August 2026.

**This project therefore has no state, and every resource it declares already
exists in the account under something else's control.** The live infra was
rebuilt by hand with the AWS CLI on 2026-08-21 and 2026-08-23.

Consequences, in order of how badly they bite:

- `pulumi up` from empty state does not adopt anything. It tries to **create**
  `pmproxy`, `pmproxy-lambda-role`, the log group, the Cognito pool, and the
  Function URL from scratch. Most calls fail on "already exists"; the ones that
  succeed leave you with duplicates and a half-applied stack.
- `pulumi destroy` against empty state is a no-op today, but against a
  *partially* imported stack it is a live-outage button.
- Cognito's pool and client carry `protect=True`, which is the only reason a
  stray `destroy` would not silently revoke every user.

`pulumi preview` is safe and is the only Pulumi verb anyone should run here
until the import below is finished.

### The code here is reconciled, not authoritative

`lambda_proxy.py` and `github_oidc.py` were edited on 2026-08-23 to **match the
live account**, so that a post-import `up` is a no-op instead of a prod
incident. Two of those edits were live grenades:

- `PMPROXY_AUTH_ENABLED` said `"true"`. The live function runs `"false"` —
  Cognito Bearer is retired and the Function URL is `AWS_IAM`, so an applied
  `"true"` would have made the proxy demand a JWT on the same `Authorization`
  header SigV4 already owns. Every client breaks.
- The Function URL said `authorization_type="NONE"` with two `principal="*"`
  invoke permissions. The live URL is `AWS_IAM` with **no resource policy at
  all**. Applying that code would have handed the world an invoke grant.

Do not "clean up" these values back toward the pre-teardown design.

---

## What is actually live (2026-08-23)

| Resource | Live? | Note |
|---|---|---|
| Lambda `pmproxy` | yes | rebuilt by CLI from the `pmproxy-v1.0.1` release zip |
| Function URL | yes | **`AWS_IAM`**, new hostname; old `fbs5…` URL is dead |
| `pmproxy-lambda-role` | yes | basic exec policy only |
| Log group `/aws/lambda/pmproxy` | yes | **no retention set** — code says 7d, live never expires |
| GitHub OIDC provider | yes | created 2026-05-20, shared with mubs / xn-wordle |
| `pmproxy-ci-deploy` role | yes | recreated by CLI 2026-08-23 |
| Cognito pool + client | yes | survived the teardown; unused by the proxy now |
| Budget `pmproxy-lambda` | yes | $5/mo, matches code |
| SNS topic + 5 CloudWatch alarms | **no** | destroyed, never recreated — pmproxy runs unalarmed |

## Re-import sketch (NOT DONE — do not run this blind)

Re-adopting the account into Pulumi is a deliberate project, not a step in
someone else's task. Rough shape if and when it happens:

1. **Recreate a backend bucket** (versioned, AES256, public access blocked) and
   `pulumi login s3://<bucket>`, then `pulumi stack init prod` and restore the
   two config values (`pmt:cognitoPoolId`, `pmt:cognitoClientId` — still in
   `Pulumi.prod.yaml`).
2. **Import each live resource** with `pulumi import <type> <name> <id>`, using
   the Pulumi resource names already in this code so the URNs line up:

   ```bash
   pulumi import aws:iam/openIdConnectProvider:OpenIdConnectProvider github-actions-oidc \
       arn:aws:iam::350985642081:oidc-provider/token.actions.githubusercontent.com
   pulumi import aws:iam/role:Role pmproxy-ci-deploy pmproxy-ci-deploy
   pulumi import aws:iam/rolePolicy:RolePolicy pmproxy-ci-deploy-policy \
       pmproxy-ci-deploy:pmproxy-ci-deploy-policy
   pulumi import aws:iam/role:Role pmproxy-lambda-role pmproxy-lambda-role
   pulumi import aws:iam/rolePolicyAttachment:RolePolicyAttachment pmproxy-lambda-logs \
       pmproxy-lambda-role/arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
   pulumi import aws:cloudwatch/logGroup:LogGroup pmproxy-logs /aws/lambda/pmproxy
   pulumi import aws:lambda/function:Function pmproxy pmproxy
   pulumi import aws:lambda/functionUrl:FunctionUrl pmproxy-url pmproxy
   pulumi import aws:cognito/userPool:UserPool pmproxy-userpool eu-west-1_BCrjnvkqQ
   pulumi import aws:cognito/userPoolClient:UserPoolClient pmproxy-userpool-client \
       eu-west-1_BCrjnvkqQ/31qer0atnp30hccnma0ki3o7ri
   pulumi import aws:budgets/budget:Budget pmproxy-lambda-budget \
       350985642081:pmproxy-lambda
   ```

   The SNS topic, its email subscription, and the five `MetricAlarm`s have
   nothing to import — they do not exist. The first `up` after import would
   **create** them, which is the desired outcome but is a real change, not a
   no-op.

3. **`pulumi preview` until it is empty** except for those alarm creations and
   the log-group retention fix. Any other diff means the code drifted from the
   account again — fix the code, not the account.
4. Only then is `pulumi up` allowed, and the first one should be run by a human
   watching the diff line by line.

Until step 3 shows a clean preview, treat this directory as documentation.

## Bootstrap (historical — the backend it names is gone)

```bash
cd infra/pulumi
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## What this code describes

- Cognito User Pool + client (live, `protect=True`)
- pmproxy Lambda + Function URL + execution role + log group (live)
- GitHub Actions OIDC provider + `pmproxy-ci-deploy` role (live)
- SNS alarm topic + 5 CloudWatch alarms (**not live**)
- $5/mo Lambda budget (live)
