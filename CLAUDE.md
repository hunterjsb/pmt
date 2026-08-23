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
(cd pmtrader && uv sync && uv run pytest tests/ -v)
(cd pmstrat && uv sync && uv run pytest tests/ -v)

# Rust (pmproxy, pmengine)
(cd pmproxy && cargo test)
(cd pmengine && cargo build --features ec2 && cargo test)
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

## Live trading ops (crypto up/down trigger)

- `pmt` works from ANY cwd — shim at `~/.local/bin/pmt` cds into pmtrader first (kills the "Failed to spawn pmt" class of error).
- Engine lifecycle: `pmt engine start|kill|restart|logs` — detached, pidfile + timestamped logs in `~/.pmt/engine/`, prefers `target/release`. Never launch it piped into `head` (SIGPIPE kills it).
- Two different "stops", two different exposure semantics: `pmt crypto disarm` retires the arms *inside* a still-running `updown` strategy (orders pulled next tick, roll chains dead, strategy stays loaded and re-armable). `pmt engine stop <strategy>` removes the strategy from the runtime entirely and now **unsubscribes its tokens too**, which releases the risk manager's exposure ledger — skipping that release is what used to freeze ghost exposure on a dead strategy. Neither one sells a position that already filled.
- Flow: `pmt crypto updown <url|slug>` prices a market (semantics auto-parsed from the description — TWAP vs close-vs-open); `pmt crypto arm <url> --size N [--side up|down]` hands params to the resident `updown` strategy; `pmt crypto trigger` shows its live eval (incl. committed/budget); `pmt crypto disarm` stops it and pulls its orders next tick.
- Arms ROLL by default: at window close the strategy re-arms the next window in the series itself (fresh budget, same gates; token ids fetched from gamma, retries hop windows on outage). `--no-roll` for one-shot. **Stopping the fleet**: `pmt crypto disarm` kills all arms AND their roll chains (orders pulled next tick); `pmt engine kill` stops the process (check `pmt orders` for strays after a hard kill).
- **What a poweroff actually leaves exposed**: the engine handles SIGINT *and* SIGTERM, so `pmt engine kill` and the nightly systemd poweroff both run graceful shutdown — resting orders get cancelled rather than left on the book. What that does NOT undo is **already-filled inventory**: any position the engine bought rides to resolution unmanaged, with no exits and no evacuation, because the process that would have run them is gone. Ceiling per window is the arm's `--size` (100–400 typical) built from `--clip`-sized fires (10–50); the exposure is whatever the arms had actually filled at the moment the box went down, not one clip. `pmt crypto activity` / `pmt crypto window <slug>` after a restart is how you find out.
- Manual momentum override: `--min-elapsed 0 --min-fair 0 --min-edge 0.005 --side X`.
- Durable eval/fire tape: `~/.pmt/engine/updown-tape.jsonl` — cross-session calibration data. Session scratchpads are tmpfs and die on the nightly poweroff; never leave data you want there.
- Engine gotcha: realtime taker fills are MISSED (`on_fill` doesn't run); the ~5s position reconcile is the truth. Any budget/PnL logic must read the position tracker, never fill events alone.
