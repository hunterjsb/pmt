# pmproxy

HTTP + WebSocket reverse proxy for Polymarket APIs. Deployed close to Polymarket servers (eu-west-1) to bypass US geoblocks and centralize per-tenant rate limiting.

**Stability:** the 1.x route surface (`/clob/*`, `/gamma/*`, `/chain/*`, `/clob/ws/{channel}`, `/health`, `/badge`, `/metrics`) and the JWT claim model are frozen. Breaking changes require a major version bump. See [CHANGELOG.md](CHANGELOG.md).

```
Python client → pmproxy → Polymarket APIs
```

The proxy forwards Polymarket auth headers unchanged — the upstream signing is handled client-side. **The deployed Lambda authenticates with SigV4/IAM** (`AuthType=AWS_IAM` on the Function URL, `PMPROXY_AUTH_ENABLED=false`); the in-process Cognito JWT gate below is a dormant option for a multi-tenant deployment, not the live path.

## Routes

| Prefix | Upstream |
| --- | --- |
| `/clob/*`         | `https://clob.polymarket.com/*` |
| `/clob/ws/{chan}` | `wss://ws-subscriptions-clob.polymarket.com/ws/{chan}` (EC2 only — Lambda can't WS) |
| `/gamma/*`        | `https://gamma-api.polymarket.com/*` |
| `/chain/*`        | `https://polygon-bor-rpc.publicnode.com` |
| `/health`         | `{"status":"healthy"}` |
| `/badge`          | shields.io schema for the README status badge |

Only `market` and `user` are accepted as WS channels.

## Deploy

### Lambda (live, eu-west-1)

The Lambda is deployed automatically by GitHub Actions
(`.github/workflows/deploy-pmproxy.yml`) when `pmproxy/Cargo.toml` version is
bumped on master. The workflow uses the OIDC-federated role
`pmproxy-ci-deploy` to `aws lambda update-function-code` against the
function URL, then runs integration tests and creates a `pmproxy-v<x.y.z>`
GitHub release with the bootstrap zip attached.

Manual trigger: `workflow_dispatch` from the Actions tab.

**Config changes** (env vars, memory, role, etc.) go through `pulumi up`
locally — CI only swaps the binary, not the infra.

### EC2 / local

Use this build for the WebSocket proxy or for local dev:

```bash
cargo build --release --features ec2  # ec2 = clap + ws
./target/release/pmproxy               # binds 0.0.0.0:8080 by default
```

## CLI options

```
pmproxy [OPTIONS]

  -H, --host <HOST>       Host to bind [default: 0.0.0.0]
  -p, --port <PORT>       Port [default: 8080]
  -l, --log-level <LEVEL> Log level [default: info]
```

## Environment

Optional multi-tenant Cognito gate — **off in the live deployment**, which
relies on the Function URL's IAM auth instead. Leave `PMPROXY_AUTH_ENABLED`
unset unless you are running a multi-tenant proxy of your own:

```
PMPROXY_AUTH_ENABLED=true              # default: false — live Lambda leaves it false
PMPROXY_COGNITO_REGION=us-east-1       # AWS region
PMPROXY_COGNITO_POOL_ID=us-east-1_xxx  # User Pool ID
PMPROXY_COGNITO_APP_CLIENT_ID=xxx      # optional: validate audience claim
PMPROXY_RATE_LIMIT_RPM=60              # default per-tenant rpm
PMPROXY_RATE_LIMIT_BURST=10            # default burst allowance
```

Optional `/chain/*` JSON-RPC method allowlist (default: pass-through):

```
PMPROXY_CHAIN_METHOD_ALLOWLIST=eth_chainId,eth_blockNumber,eth_call,eth_getBalance
```

When unset, `/chain/*` forwards request bodies to the upstream RPC unchanged
— correct for a single-tenant deployment where the JWT holder is the
operator. When set, every request body is parsed as JSON-RPC and any method
outside the list returns 403. Batched requests (`[{...}, {...}]`) are
allowed only if every method in the batch is allowlisted.

Tier-based limits (from the `custom:tenant_tier` JWT claim):
- `free`       → 60 rpm / 10 burst
- `pro`        → 300 rpm / 50 burst
- `enterprise` → 1000 rpm / 100 burst

## Layout

```
src/
├── lib.rs       core proxy + router (shared by both binaries)
├── main.rs      EC2 server binary (tokio)
├── lambda.rs    Lambda handler binary
├── ws.rs        WebSocket bridge (ec2 only)
├── auth.rs      Cognito JWKS fetch + JWT validation (dormant; off in the live Lambda)
├── ratelimit.rs governor-based per-tenant rate limiting
├── config.rs    ProxyConfig from env
└── error.rs     AuthError + axum IntoResponse
```

## Test

```bash
cargo test --lib                    # default features
cargo test --lib --features lambda  # lambda binary feature set
cargo test --lib --features ws      # WebSocket bridge

# Live endpoints once running
curl http://localhost:8080/health
curl http://localhost:8080/gamma/events?limit=5
```
