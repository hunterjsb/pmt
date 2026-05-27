# pmproxy Runbook

Operational reference for the Lambda deployment in `eu-west-1`.

## Quick health check

```bash
curl https://<function-url>/health   # → {"status":"healthy"}
curl https://<function-url>/badge    # → {"schemaVersion":1,...,"message":"online",...}
```

If `/health` doesn't return 200 within 2s, treat as a P1 incident.

## Common problems

### `/clob/*` or `/gamma/*` returning 401

The Lambda thinks your JWT is bad. Most likely causes:

1. Your Cognito access token expired (1 hr TTL). Re-auth:
   ```bash
   from polymarket.cognito import CognitoAuth
   CognitoAuth().get_token()   # forces a fresh fetch
   ```
2. The JWKS cache in the Lambda needs a rotation cycle. Cold-start the
   Lambda — bump `pmproxy/Cargo.toml` patch version, push, let
   deploy-pmproxy.yml fire, or `workflow_dispatch` the deploy.
3. Cognito Pool ID / region mis-configured in the Lambda env. Compare
   `PMPROXY_COGNITO_POOL_ID` / `PMPROXY_COGNITO_REGION` against
   `infra/pulumi/`.

### All routes returning 5xx

Check CloudWatch logs first:

```bash
aws logs tail /aws/lambda/pmproxy --since 10m --follow
```

Typical causes:
- Upstream Polymarket outage → wait it out, nothing to do
- `cargo update`-related ABI mismatch → rollback (see below)
- Lambda function-URL config drift → check `infra/pulumi/`

### `/chain/*` returning errors

The Polygon RPC upstream is `polygon-bor-rpc.publicnode.com` (a public
free-tier RPC). If it's throttling us we need a paid endpoint — the
upstream URL is hardcoded in `pmproxy/src/upstream.rs`.

## Rollback

The deploy workflow attaches `bootstrap.zip` to each GitHub release. To
roll back to the previous version:

```bash
# 1. Identify the prior release tag
gh release list --repo hunterjsb/pmt | grep pmproxy

# 2. Download the prior bootstrap.zip
gh release download <prior-tag> --pattern bootstrap.zip --repo hunterjsb/pmt

# 3. Push it to the Lambda directly (skips the version-bump deploy path)
aws lambda update-function-code \
  --function-name pmproxy \
  --region eu-west-1 \
  --zip-file fileb://bootstrap.zip

# 4. Verify
curl https://<function-url>/health
```

After rollback, open a tracking issue noting which release was rolled out
of, so the bad version doesn't sneak back via a forward-merge.

## Deploy mechanics

`deploy-pmproxy.yml` triggers on:
- **push** to master whose diff bumps `pmproxy/Cargo.toml` version
- **workflow_dispatch** (manual)
- **release** of a `pmproxy-v*` tag

Pipeline: build Lambda zip → `aws lambda update-function-code` →
integration tests against the live function URL → `gh release create`
with the zip attached.

Env-var or memory changes go through `pulumi up` locally — CI only swaps
the binary.

## Where to look

| Question | Where |
|---|---|
| Function URL, role, env vars | `infra/pulumi/` |
| Current deployed version | `gh release view pmproxy-v$(grep '^version' pmproxy/Cargo.toml | head -1 | cut -d'"' -f2)` |
| Recent invocations | CloudWatch Logs `/aws/lambda/pmproxy` |
| Recent deploys | `gh run list --workflow=deploy-pmproxy.yml` |
| Live metrics | `curl $PMPROXY_URL/metrics` (Prometheus text; no auth) |

## Deep testing

`pmproxy/tests/test_deep.py` is the heavyweight verification suite — it
exercises end-to-end JWT failure paths, latency percentiles, metric
counter accuracy, concurrency-within-burst, failure injection, and an
intentional rate-limit-tripping burst. **Not run in CI** (it intentionally
trips the rate limiter and takes 2+ minutes).

Run after any non-trivial change:

```bash
cd pmproxy/tests
pip install -r requirements.txt
PMPROXY_URL=$PMPROXY_URL \
PMPROXY_COGNITO_CLIENT_ID=$PMPROXY_COGNITO_CLIENT_ID \
PMPROXY_COGNITO_REGION=$PMPROXY_COGNITO_REGION \
PMPROXY_USERNAME=$PMPROXY_USERNAME \
PMPROXY_PASSWORD=$PMPROXY_PASSWORD \
pytest test_deep.py -v
```

After running, the rate limiter is depleted — wait ~80 seconds before
running normal client traffic.

### Manual WS bridge verification

The Lambda can't WebSocket. To verify the WS bridge end-to-end:

```bash
cd pmproxy
cargo build --release --features ec2
PMPROXY_AUTH_ENABLED=false ./target/release/pmproxy --port 18080 &

# Connect through proxy to real Polymarket WS
python -c "
import asyncio, json, websockets
async def main():
    async with websockets.connect('ws://127.0.0.1:18080/clob/ws/market') as ws:
        await ws.send(json.dumps({'type': 'Market', 'assets_ids': ['<token-id>']}))
        async for msg in ws:
            print(msg)
            break
asyncio.run(main())
"
```

### Manual `/chain` allowlist verification

```bash
PMPROXY_AUTH_ENABLED=false \
PMPROXY_CHAIN_METHOD_ALLOWLIST="eth_chainId,eth_blockNumber" \
  ./target/release/pmproxy --port 18080 &

curl -X POST http://127.0.0.1:18080/chain/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"eth_sendRawTransaction","params":[],"id":1}'
# → 403 {"error":"method_not_allowed","method":"eth_sendRawTransaction"}
```
