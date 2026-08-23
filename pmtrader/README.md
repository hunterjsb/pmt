# pmtrader

Polymarket trading CLI + Python SDK. Routes through `pmproxy` (SigV4/IAM-authed Lambda in eu-west-1) to bypass the US geoblock, and through a locally-running `pmengine` for order writes when it's up.

## `pmt` CLI

Installed as a console script via `uv sync`. Run from inside `pmtrader/` (or anywhere with the venv active):

```bash
pmt --help
```

### Orders

`buy` and `sell` share an identical option surface. `REF` is either a polymarket URL/slug or a numeric token id; `OUTCOME` (yes/no/up/down/...) is required for URL/slug refs and ignored for token refs.

```bash
# URL or slug + outcome + USD notional (marketable sweep)
pmt buy  https://polymarket.com/event/btc-updown-4h-1779825600 down --amount $910
pmt sell nobel-peace-prize-winner-2026 no --amount $50 --match Trump

# Numeric token id + explicit limit
pmt buy  14658893069672317885... --price 0.92 --size 217
pmt sell 14658893069672317885... --price 0.98 --size 50

# Disambiguate multi-market events (lists options if --match is missing)
pmt buy bitcoin-price-on-may-26-2026 no --amount $80 --match '78,000'

# Share-count sweep (--size alone, marketable)
pmt buy URL no --size 200

# Auto-cancel after 30m (requires running pmengine)
pmt buy URL no --amount $50 --ttl 30m

# Two-leg buy-then-sell (token-only, for niche maker-taker plays)
# [deprecated — candidate for removal, speak up if you use this] — see `pmt sweep --flip`
pmt flip --token 14658... --buy-price 0.09 --sell-price 0.10 --size 850

# Buy-side sweep: take every displayed ask ≤ --to with one GTC limit resting there
pmt sweep URL yes --to 0.95 --max-cost $150 --dry-run
pmt sweep 14658... --to 0.95 --flip 0.99     # on fill, resell the filled size at 0.99

pmt cancel 0xeb78787c2c55...
```

Every order command supports `--dry-run`.

### Reads

```bash
pmt balance                          # spendable USDC + cash locked in resting BUYs
pmt orders                           # open resting orders w/ market labels
pmt positions --orders               # current positions + theme exposure + open orders
pmt positions --themes btc,eth       # filter themes
pmt pnl                              # realized 1d/7d/30d/all + unrealized (activity replay)
pmt rewards --days 7                 # REWARD + YIELD income (last N days)
```

`balance`, `orders`, and `positions` take `--json` for raw output.

### Discovery

```bash
pmt book   REF [OUTCOME]             # depth chart w/ mid + spread (URL/slug/token; --depth N, --json)
pmt market <slug-or-condition-id>    # event metadata
pmt search pandemic                  # free-text active-market search
```

### Engine

`pmt engine` talks to a locally-running `pmengine` control plane (default `http://127.0.0.1:7531`; override with `PMENGINE_CONTROL_URL`):

```bash
pmt engine status                    # uptime, balance, open orders, P&L
pmt engine strategies                # registered strategies + cadence
pmt engine orders [--all]            # engine-tracked orders (or unified view)
pmt engine subscriptions             # tokens the engine is watching
pmt engine trades <token-id>         # recent trades from the rolling buffer
pmt engine alerts                    # pending strategy alerts awaiting approval
pmt engine approve <alert-id>
pmt engine reject  <alert-id>
```

When the engine is up, `pmt buy/sell/cancel` route through it so writes share the engine's account-wide rate-limit budget.

### Scanners

```bash
pmt scan REF                         # pre-trade due diligence on an event
pmt fit  REF                         # touch buckets vs realized vol  [deprecated]
pmt scan cliff    --once             # ask-ladder gaps + thick wall   [deprecated]
pmt scan expiring --once             # high-certainty expiring        [deprecated]
```

`[deprecated — candidate for removal, speak up if you use this]` — flagged in
`--help` too. Nothing is broken; these just look like one-offs whose moment passed.

### Sports

`pmt sports board|game|watch LEAGUE ...` — ESPN scores, win prob, and a live
game-vs-moneyline dashboard. Also `[deprecated — candidate for removal, speak
up if you use this]`; nothing outside `cli.py` references it.

## Python SDK

```python
from polymarket import PolymarketAPI, Gamma, get_order_book_depth, sampling_markets

api = PolymarketAPI()

# Orders
api.place("buy", token=TOKEN_ID, price=0.93, size=217)
# Thin typed wrappers for readability:
api.place_buy(token=TOKEN_ID, price=0.93, size=217)
api.place_sell(token=TOKEN_ID, price=0.10, size=862)

# Two-leg with settlement-lag retry
result = api.flip(token=TOKEN_ID, buy_price=0.09, sell_price=0.10, size=850)
print(result.potential_profit)

# Reads
api.get_positions()                  # data-api positions (no auth)
api.get_orders()                     # L2-authed open orders
api.get_portfolio_value()            # total $ value
api.get_activity(kind="REWARD")
api.get_full_activity()              # paginated; used by pnl replay
api.get_rewards_config(condition_id)
api.search_markets("pandemic")
api.get_book(token_id)

# Unauthenticated read modules
Gamma().events(closed=False)
get_order_book_depth(token_id)       # full ladder, not aggregated
```

## Package layout

```
pmtrader/
├── cli.py              # pmt CLI (click)
├── cli_crypto.py       # `pmt crypto ...` — command registration only; each
│                    #   command lives in a cli_crypto_<area>.py beside it
│                    #   (arm/stats/watch/data/fixture)
├── cli_common.py       # the one Rich console + lazy API loader shared by all
├── engine.py           # pmengine control-plane client
├── scanners/           # market scanners (expiring, order-book)
└── polymarket/
    ├── __init__.py     # public exports
    ├── hosts.py        # single source of truth for API URLs + PMPROXY_URL
    ├── sigv4.py        # SigV4 signing for the AWS_IAM Function URL
    ├── api.py          # PolymarketAPI — authenticated v2 client
    ├── clob_v2.py      # v2 ClobClient factory + SigV4 monkey-patch
    ├── clob.py         # unauth read helpers (depth, sampling)
    ├── gamma.py        # Gamma API
    ├── constants.py    # FEE_RATE / BASIS_NOISE_BP / taker_fee (one source)
    ├── crypto.py       # up/down semantics + one-shot pricing
    ├── chainlink.py    # Chainlink oracle rounds + measured per-arm GUARD_BP
    ├── tape.py         # decision-tape paths, event names, record iteration
    ├── shadow.py       # shadow P&L ledger (pure)
    ├── outcomes.py     # resolved-window corpus
    ├── pnl.py          # activity-ledger replay
    └── models.py       # plain dataclasses
```

## Required env (`.env` at repo root)

```bash
PM_PRIVATE_KEY=0x...
PM_FUNDER_ADDRESS=0x...
PM_SIGNATURE_TYPE=1                  # 1 = Polymarket Proxy wallet

# pmproxy — required for placing orders from a geoblocked region.
# Auth is SigV4 against the AWS_IAM Function URL: signed with your local
# AWS credentials, so no proxy secrets belong in .env.
PMPROXY_URL=https://<...>.lambda-url.eu-west-1.on.aws
```

## Tests

```bash
uv run pytest tests/                 # excludes @integration by default
uv run pytest tests/ -m integration  # post-deploy smoke against live Lambda
```
