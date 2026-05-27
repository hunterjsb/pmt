# pmtrader

Polymarket trading CLI + Python SDK. Routes through `pmproxy` (Cognito-authed Lambda in eu-west-1) to bypass the US geoblock, and through a locally-running `pmengine` for order writes when it's up.

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
pmt flip --token 14658... --buy-price 0.09 --sell-price 0.10 --size 850

pmt cancel 0xeb78787c2c55...
```

Every order command supports `--dry-run`.

### Reads

```bash
pmt orders                           # open resting orders w/ market labels
pmt positions --orders               # current positions + theme exposure + open orders
pmt positions --themes btc,eth       # filter themes
pmt pnl                              # realized 1d/7d/30d/all + unrealized (activity replay)
pmt rewards --days 7                 # REWARD + YIELD income (last N days)
```

### Discovery

```bash
pmt book   <token-id>                # depth chart
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
pmt scan cliff    --once             # ask-ladder gaps + thick wall
pmt scan expiring --once             # high-certainty markets expiring soon
```

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
├── engine.py           # pmengine control-plane client
└── polymarket/
    ├── __init__.py     # public exports
    ├── hosts.py        # single source of truth for API URLs + PMPROXY_URL
    ├── api.py          # PolymarketAPI — authenticated v2 client
    ├── clob_v2.py      # v2 ClobClient factory + Cognito monkey-patch
    ├── clob.py         # unauth read helpers (depth, sampling)
    ├── gamma.py        # Gamma API
    ├── cognito.py      # Cognito JWT auth
    ├── pnl.py          # activity-ledger replay
    └── models.py       # plain dataclasses
```

## Required env (`.env` at repo root)

```bash
PM_PRIVATE_KEY=0x...
PM_FUNDER_ADDRESS=0x...
PM_SIGNATURE_TYPE=1                  # 1 = Polymarket Proxy wallet

# pmproxy — required for placing orders from a geoblocked region
PMPROXY_URL=https://<...>.lambda-url.eu-west-1.on.aws
PMPROXY_USERNAME=...
PMPROXY_PASSWORD=...
PMPROXY_COGNITO_CLIENT_ID=...
PMPROXY_COGNITO_REGION=eu-west-1
```

## Tests

```bash
uv run pytest tests/                 # excludes @integration by default
uv run pytest tests/ -m integration  # post-deploy smoke against live Lambda
```
