# pmt Roadmap

## Vision

**pmt core** is a prediction market trading ecosystem with two pillars:

- **pmfinance** — Trading strategies, market intelligence, and data aggregation
- **pmplatform** — Low-latency infrastructure for prediction market traders

Core principle: **dogfooding**. We run our own strategies on our own infra.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                            pmt core                                 │
├─────────────────────────────────┬───────────────────────────────────┤
│          pmfinance              │           pmplatform              │
│   (Strategies & Intelligence)   │    (Infrastructure Provider)      │
├─────────────────────────────────┼───────────────────────────────────┤
│  • Internal trading strategies  │  • Co-located server (low latency)│
│    - Arbitrage                  │  • pmproxy (reverse proxy)        │
│    - Sure-bet yield chasing     │  • Rust HFT engine                │
│    - MM/LP (order flow based)   │  • pmtrader SDK (FOSS)            │
│  • External data aggregation    │    └─ Dev: Lambda (cheap/slow)    │
│  • Public strategy posts        │    └─ Prod: Rust engine (fast)    │
│                                 │  • Python→Rust transpiler (prop.) │
└─────────────────────────────────┴───────────────────────────────────┘
```

---

## Current State (Phase 0)

| Component   | Status | Description                                    |
|-------------|--------|------------------------------------------------|
| pmtrader    | ✅      | Python SDK + CLI + Streamlit UI                |
| pmproxy     | ✅      | Rust reverse proxy (EC2/Lambda deployable)     |

---

## Phase 1: pmplatform Foundation

**Goal:** Stand up core infrastructure for strategy execution.

- [ ] **Co-located server**
  - Provision server near Polymarket infrastructure
  - Deploy pmproxy on co-located hardware
  - Establish baseline latency metrics

- [ ] **Rust HFT engine (core)**
  - Order execution layer (place/cancel/amend)
  - Strategy runtime interface
  - Position and risk tracking
  - Event loop with sub-millisecond tick processing

- [ ] **pmtrader SDK extensions**
  - Strategy definition API (extends existing pmtrader)
  - Dev mode: AWS Lambda execution (cheap, slow)
  - Local backtesting harness
  - Seamless switch between dev/prod execution targets

---

## Phase 2: Transpiler + Strategies (concurrent)

**Goal:** Build the Python→Rust transpiler while developing strategies to validate it.

These workstreams run in parallel — strategies provide real-world test cases for the transpiler.

### Transpiler (pmplatform)

- [ ] **Strategy DSL**
  - Define constrained Python subset for strategies
  - Signal/indicator primitives
  - Order action primitives (market, limit, cancel)
  - Position/portfolio introspection

- [ ] **Transpiler**
  - Parse Python AST
  - Generate idiomatic Rust code
  - Integrate with Rust HFT engine runtime
  - Compile-time validation and error reporting

- [ ] **Testing & Validation**
  - Equivalence testing (Python vs generated Rust)
  - Performance benchmarks
  - CI integration for strategy compilation

### Strategies & Data (pmfinance)

- [ ] **Data aggregation**
  - External data source connectors (news, social, on-chain)
  - Normalized event/signal pipeline
  - Historical data storage and replay

- [ ] **Internal strategies (dogfooding)**
  - Basic arbitrage (cross-market, cross-platform)
  - Sure-bet yield farming (low-risk, consistent returns)
  - Market making / LP (order flow and spread analytics)

- [ ] **Analytics & order flow**
  - Real-time order book analysis
  - Trade flow classification
  - Strategy performance dashboards

---

## Phase 3: Public Launch

**Goal:** Open pmplatform to external users and launch public strategy content.

- [ ] **Public strategy posts**
  - Platform for sharing strategy ideas
  - Backtested performance reports
  - Community engagement

- [ ] **pmplatform for external traders**
  - Onboarding and documentation
  - Billing and usage metering
  - SLAs and support

---

## Future Considerations

- **Multi-exchange support** — Extend beyond Polymarket
- **Strategy marketplace** — Users deploy strategies on pmplatform
- **Risk management layer** — Portfolio-level limits and circuit breakers
- **Institutional features** — Sub-accounts, audit logs, compliance tools

---

## Milestones

| Milestone                          | Phase | Target |
|------------------------------------|-------|--------|
| Co-located server operational      | 1     | TBD    |
| Rust engine MVP (manual orders)    | 1     | TBD    |
| pmtrader strategy API (dev mode)   | 1     | TBD    |
| First strategy running on Lambda   | 2     | TBD    |
| Transpiler MVP                     | 2     | TBD    |
| First strategy compiled to Rust    | 2     | TBD    |
| Data aggregation pipeline live     | 2     | TBD    |
| Internal strategies profitable     | 2     | TBD    |
| Public strategy posts launch       | 3     | TBD    |
| pmplatform external beta           | 3     | TBD    |

---

*Last updated: 2025-01-16*
