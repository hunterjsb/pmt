# pmt infra (Pulumi)

Python Pulumi project managing all pmt AWS infra in **eu-west-1**.

## Backend

State stored in S3: `s3://pmt-pulumi-state-350985642081-euw1` (versioned, AES256, public access blocked).

## Stacks

- `prod` — the live one

## Bootstrap (one-time per machine)

```bash
cd infra/pulumi
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

pulumi login s3://pmt-pulumi-state-350985642081-euw1
pulumi stack select prod    # or 'pulumi stack init prod' first time
```

When prompted for a passphrase, set one and save it in `.infra/INFRA.md`. This encrypts secrets in the state file.

## Common ops

```bash
pulumi preview              # dry run
pulumi up                   # apply
pulumi stack output         # show URLs, IDs
pulumi refresh              # reconcile state with reality
```

## What's managed here

- Cognito User Pool (imported from CFN stack `pmproxy-cognito`)
- pmproxy Lambda + Function URL + execution role + log group
