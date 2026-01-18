# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**pmt** (Polymarket Trading Toolkit) is a prediction market trading ecosystem with four components:

- **pmtrader** (Python 3.14) - Client SDK, CLI, and Streamlit UI for Polymarket
- **pmproxy** (Rust) - HTTP reverse proxy for Polymarket APIs (EC2/Lambda deployment)
- **pmengine** (Rust) - HFT trading engine with order execution and risk management
- **pmstrat** (Python 3.14) - Strategy DSL and backtesting framework

## Build & Test Commands

### pmtrader (Python)
```bash
cd pmtrader
uv sync                          # Install dependencies
uv run pytest tests/ -v          # Run tests
uv run pytest tests/test_models.py::test_name -v  # Single test
uv run python main.py            # Demo: browse markets
uv run python trade.py           # Interactive trading
uv run pmtrader-ui               # Streamlit web UI
```

### pmproxy (Rust)
```bash
cd pmproxy
cargo build --features ec2       # Dev build
cargo build --release --features ec2  # Release build
cargo test                       # Run tests
cargo build --features lambda    # Lambda build
```

### pmengine (Rust)
```bash
cd pmengine
cargo build --features ec2       # Dev build
cargo build --release --features ec2  # Release build
cargo test                       # Run tests
./target/release/pmengine --dry-run  # Test without real orders
```

### pmstrat (Python)
```bash
cd pmstrat
uv sync
uv run pytest tests/               # Run tests
uv run pmstrat simulate --ticks 500  # Backtest on synthetic data
uv run pmstrat scan                  # Scan for live opportunities (needs pmtrader)
```

## Architecture

```
pmtrader (Python SDK)
    ↓
    ├─→ pmproxy (if PMPROXY_URL set)
    │        ↓
    │    Polymarket APIs
    │
    └─→ Direct to Polymarket (if no proxy)
        - CLOB: https://clob.polymarket.com
        - Gamma: https://gamma-api.polymarket.com
        - Polygon: https://polygon-rpc.com

pmengine (Rust HFT)
    ├─ Event loop (tokio::select! on tick/fill/shutdown)
    ├─ Strategy runtime with Signal enum (Buy/Sell/Cancel/Hold)
    ├─ Order & position management
    └─ Risk validation before execution
```

### Key Patterns

- **Proxy routes**: `/clob/*`, `/gamma/*`, `/chain/*` map to respective APIs
- **Client-handled auth**: pmproxy is stateless; Python client signs requests
- **Dry-run mode**: Both pmproxy and pmengine support `--dry-run`
- **Feature flags**: Rust binaries use `--features ec2` or `--features lambda`

## Environment Variables

### pmtrader (required for trading)
```
PM_PRIVATE_KEY=0x...
PM_FUNDER_ADDRESS=0x...
PM_SIGNATURE_TYPE=0|1|2  # 0=EOA, 1=Poly Proxy, 2=EIP-1271
PMPROXY_URL=http://localhost:8080  # Optional
```

### pmengine
```
PMENGINE_PRIVATE_KEY or PRIVATE_KEY  # Required
PMENGINE_CLOB_URL (default: https://clob.polymarket.com)
PMENGINE_WS_URL (default: wss://ws-subscriptions-clob.polymarket.com)
PMENGINE_MAX_POSITION_SIZE (default: 1000)
PMENGINE_MAX_TOTAL_EXPOSURE (default: 5000)
PMENGINE_TICK_INTERVAL_MS (default: 1000)
PMENGINE_LOG_LEVEL or RUST_LOG (default: info)
```

## Key Files

### pmtrader
- `polymarket/clob.py` - CLOB API client
- `polymarket/gamma.py` - Gamma API client (markets, events)
- `polymarket/models.py` - Data models (Token, Market, OrderBook)
- `trade.py` - Interactive trading with order placement
- `scan.py` - Market scanner (cliffs, expiring opportunities)

### pmproxy
- `src/lib.rs` - Core proxy logic with `build_router()` and `proxy_handler()`
- `src/main.rs` - EC2 binary
- `src/lambda.rs` - Lambda handler

### pmengine
- `src/engine.rs` - Main event loop with tokio::select!
- `src/strategy.rs` - Strategy trait, Signal enum, StrategyContext
- `src/order.rs` - Order lifecycle management
- `src/position.rs` - Position tracking & P&L
- `src/risk.rs` - Risk limits & RiskManager
- `src/config.rs` - Environment configuration loading

### pmstrat
- `pmstrat/signal.py` - Signal types (Buy, Sell, Cancel, Hold) with Urgency levels
- `pmstrat/context.py` - Context, OrderBookSnapshot, Position, MarketInfo
- `pmstrat/dsl.py` - `@strategy` decorator for defining strategies
- `pmstrat/rewards.py` - RewardsSimulator for Polymarket liquidity rewards
- `pmstrat/backtest.py` - Backtesting harness with synthetic tick generation
- `pmstrat/cli.py` - CLI commands (simulate, scan, backtest)
- `pmstrat/strategies/sure_bets.py` - Low-risk strategy for expiring markets

## Strategy Workflow

Strategies are written in Python using pmstrat's constrained DSL, then transpiled to Rust for production execution:

```
pmstrat (Python DSL) → transpile → Rust code → pmengine (execution)
```

This allows rapid strategy development/backtesting in Python while achieving HFT performance in production via the Rust engine.

## CI/CD

- **ci.yml**: Runs on push/PR - Python tests (uv sync + pytest) and Rust builds
- **publish-pmproxy.yml**: On tag `pmproxy-v*` - Cross-compile, GitHub release, CodeDeploy to EC2
- **publish-pmengine.yml**: On tag `pmengine-v*` - Same deployment flow
- **publish-pmtrader.yml**: On tag `pmtrader-v*` - Build wheel, GitHub release

## Deployment

Rust binaries deploy to EC2 via CodeDeploy (eu-west-1):
- `appspec.yml` defines lifecycle hooks
- `scripts/` contains install.sh, start.sh, stop.sh, validate.sh
- Cross-compiled for both x86_64 and aarch64
