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

## Current State (Phase 1 Complete)

| Component   | Status | Description                                    |
|-------------|--------|------------------------------------------------|
| pmtrader    | ✅      | Python SDK + CLI + Streamlit UI                |
| pmproxy     | ✅      | Rust reverse proxy (EC2 deployed)              |
| pmengine    | ✅      | Rust HFT engine with WebSocket + strategies    |
| pmstrat     | ✅      | Python strategy DSL + transpiler               |

---

## Phase 1: pmplatform Foundation ✅

**Goal:** Stand up core infrastructure for strategy execution.

- [x] **Co-located server**
  - EC2 instance in eu-west-1 (near Polymarket infra)
  - pmproxy deployed via CodeDeploy
  - pmengine deployed via CodeDeploy

- [x] **Rust HFT engine (pmengine)**
  - Order execution layer (place/cancel via CLOB API)
  - Custom L2 auth for proxy compatibility
  - Strategy runtime interface
  - Position and risk tracking
  - WebSocket orderbook subscriptions
  - Event loop with tokio::select!

- [x] **pmstrat (Strategy DSL)**
  - @strategy decorator with tokens/subscriptions
  - Signal types: Buy, Sell, Cancel, Hold
  - Context API: ctx.book(), ctx.position(), ctx.mid()
  - Urgency levels for order priority

---

## Phase 2: Transpiler + Strategies (in progress)

**Goal:** Build the Python→Rust transpiler while developing strategies to validate it.

### Transpiler (pmplatform)

- [x] **Strategy DSL**
  - Constrained Python subset for strategies
  - Signal/indicator primitives (Buy, Sell, Cancel, Hold)
  - Order action primitives (limit orders)
  - Position/portfolio introspection via context

- [x] **Transpiler MVP**
  - Parse Python AST
  - Generate Rust code
  - Integrate with pmengine runtime
  - First strategy transpiled: spread_watcher

- [ ] **Transpiler Polish**
  - Handle Option types automatically
  - Mutability inference
  - Constant propagation
  - Better error messages

- [ ] **Testing & Validation**
  - Equivalence testing (Python vs generated Rust)
  - Performance benchmarks
  - CI integration for strategy compilation

### Strategies & Data (pmfinance)

- [ ] **Data aggregation**
  - External data source connectors (news, social, on-chain)
  - Normalized event/signal pipeline
  - Historical data storage and replay

- [x] **Internal strategies (dogfooding)**
  - spread_watcher: buys when spread > 50%
  - order_test: validates order placement
  - [ ] Sure-bet yield farming
  - [ ] Market making / LP

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

| Milestone                          | Phase | Status |
|------------------------------------|-------|--------|
| Co-located server operational      | 1     | ✅ Done |
| Rust engine MVP (order placement)  | 1     | ✅ Done |
| WebSocket orderbook integration    | 1     | ✅ Done |
| pmstrat DSL defined                | 2     | ✅ Done |
| Transpiler MVP                     | 2     | ✅ Done |
| First strategy compiled to Rust    | 2     | ✅ Done (spread_watcher) |
| Transpiler polish (Option handling)| 2     | 🔄 Next |
| Sure-bet strategy                  | 2     | 🔄 Next |
| Data aggregation pipeline live     | 2     | ⏳ Planned |
| Internal strategies profitable     | 2     | ⏳ Planned |
| Public strategy posts launch       | 3     | ⏳ Planned |
| pmplatform external beta           | 3     | ⏳ Planned |

---

*Last updated: 2026-01-18*
