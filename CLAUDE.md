# CLAUDE.md

## Project Overview

**pmt** (Polymarket Trading Toolkit) - prediction market trading ecosystem:

- **pmtrader** (Python) - Client SDK, CLI, and Streamlit UI
- **pmproxy** (Rust) - Reverse proxy for Polymarket APIs
- **pmengine** (Rust) - HFT trading engine
- **pmstrat** (Python) - Strategy DSL and transpiler

## Build & Test

Use `uv` for Python (not pip). Use `uv run` to execute, `uv sync` to install.

```bash
# Python (pmtrader, pmstrat)
uv sync && uv run pytest tests/ -v

# Rust (pmproxy, pmengine)
cargo build --features ec2
cargo test
```

## Architecture

```
pmtrader → pmproxy (optional) → Polymarket APIs
                                 ├─ clob.polymarket.com
                                 ├─ gamma-api.polymarket.com
                                 └─ polygon-rpc.com

pmstrat (Python) → transpile → pmengine (Rust) → execute
```

## Environment Variables

Stored in `.env` at repo root. Key variables:
```
PM_PRIVATE_KEY=0x...
PM_FUNDER_ADDRESS=0x...
PM_SIGNATURE_TYPE=0|1|2  # 0=EOA, 1=Poly Proxy, 2=GnosisSafe
```

## Strategy Workflow

```
pmstrat (Python DSL) → transpile → Rust code → pmengine (execution)
```

Write strategies in Python, transpile to Rust for HFT performance.

## Infra & Deployment

All AWS infra is in `infra/pulumi/` (Python Pulumi, S3 backend, eu-west-1 pinned).
Current live state and decisions are tracked in `.infra/INFRA.md` (gitignored).

- **pmproxy** — Lambda + Function URL in eu-west-1. Code deploys via GitHub Actions (`.github/workflows/deploy-pmproxy.yml`) using OIDC-federated role `pmproxy-ci-deploy`: builds the Lambda zip, then `aws lambda update-function-code` to push the new binary. Trigger: `workflow_dispatch` or auto on `pmproxy-v*` release publish. **Config changes** (env vars, memory, role, etc.) still go through `pulumi up` locally — CI only swaps the binary, not the infra.
- **pmengine** — no live host. Releases via tag `pmengine-v*` produce GH artifacts only.
- **pmtrader** — Python package, no AWS deployment.

Tag-triggered workflows (`publish-pmproxy.yml`, `publish-pmengine.yml`, `publish-pmtrader.yml`) build + cut GitHub releases. `deploy-pmproxy.yml` handles Lambda deploys via Pulumi.
