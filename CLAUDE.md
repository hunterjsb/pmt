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

## CI/CD & Deployment

- Tags trigger deployment: `pmproxy-v*`, `pmengine-v*`, `pmtrader-v*`
- Rust binaries deploy via CodeDeploy to EC2 (eu-west-1)

## EC2 Instance

Production server in eu-west-1 (Ireland), near Polymarket infra.

```bash
.infra/ssh-connect.sh                    # Connect
ssh -i .infra/pmt-kp.pem ec2-user@34.250.56.199  # Manual
```

Services: `pmproxy` (port 8080), `pmengine` (systemd units)
Public URL: `https://pmt.xandaris.space`

## Known Issues (pmengine v0.1.7)

### CRITICAL: WebSocket Order Book Price Discrepancy
- **Symptom**: Strategy sees `ask_price=0.99`, but orders fill at `0.93-0.94`
- **Impact**: 5-6 cent price difference - getting better fills than expected prices
- **Root Cause**: Unknown - WebSocket order book doesn't reflect true market depth
- **Workaround**: None yet
- **TODO**: Investigate REST order book vs WebSocket; add price sanity checks

### MEDIUM: Sports Market Filtering
- Expanded keyword filtering: "vs", "fc", "o/u X.5", "esports", etc.
- May still miss some sports markets with unusual naming
- **TODO**: Consider regex-based filtering or Gamma API category exclusion
