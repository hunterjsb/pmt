# pmtrader

Polymarket trading CLI + Python SDK. Routes through `pmproxy` (Cognito-authed Lambda in eu-west-1) to bypass the US geoblock.

## `pmt` CLI

Installed as a console script via `uv sync`. Run from inside `pmtrader/` (or anywhere if the venv is active):

```bash
pmt --help                                        # list subcommands

# Orders
pmt buy   --token hantavirus-pandemic:no --price 0.93 --size 217
pmt sell  --token hantavirus-vaccine:yes --price 0.10 --size 862
pmt flip  --token hantavirus-vaccine:yes --buy-price 0.09 --sell-price 0.10 --size 850
pmt cancel 0xeb78787c2c55...

# Reads (no auth needed for these except `orders`)
pmt orders                                        # open resting orders w/ market labels
pmt positions --orders                            # full portfolio + open orders + theme exposure
pmt positions --themes hantavirus,vaccine         # filter themes
pmt rewards --days 7                              # REWARD + YIELD income (last N days)
pmt rewards --all --type reward                   # full history, rewards only

# Discovery
pmt book   hantavirus-pandemic:no                 # depth chart
pmt market hantavirus-pandemic-in-2026            # event metadata by slug or condition_id
pmt search pandemic                               # free-text active-market search

# Scanners
pmt scan cliff --once                             # ask-ladder gaps + thick wall
pmt scan expiring --once                          # high-certainty markets expiring soon
```

The `--token` arg accepts either a raw numeric token ID or `market-name:yes|no`. Market names come from `polymarket/markets.py` — add new markets there.

Every command has `--dry-run` (for orders) and `--help`.

## Python SDK

The `PolymarketAPI` class is the main entry point:

```python
from polymarket import PolymarketAPI
from polymarket.markets import HANTAVIRUS_PANDEMIC

api = PolymarketAPI()

# Orders
api.place_buy(token=HANTAVIRUS_PANDEMIC.no_token, price=0.93, size=217)
result = api.flip(
    token=HANTAVIRUS_PANDEMIC.no_token,
    buy_price=0.933, sell_price=0.934, size=215,
)
print(result.potential_profit)

# Reads
api.get_positions()       # data-api positions (no auth)
api.get_orders()          # L2-authed open orders
api.get_portfolio_value() # total $ value
api.get_activity(kind="REWARD")
api.get_rewards_config(condition_id)
api.search_markets("pandemic")
api.get_book(token_id)
```

## Package layout

```
pmtrader/
├── cli.py                 # pmt CLI (click)
├── scan.py                # opportunity scanners CLI (volume cliff, expiring)
├── polymarket/            # SDK
│   ├── api.py             # PolymarketAPI — high-level authenticated client
│   ├── clob.py            # Clob — read-only CLOB wrapper
│   ├── clob_v2.py         # v2 client setup + Cognito monkey-patch
│   ├── cognito.py         # Cognito JWT auth
│   ├── gamma.py           # Gamma API wrapper (events, search)
│   ├── markets.py         # named market constants — add yours here
│   └── models.py          # Market/Token/OrderBook dataclasses
├── scanners/              # opportunity scanners (read-only)
└── tests/
```

## Required env (`.env` at repo root)

```bash
PM_PRIVATE_KEY=0x...
PM_FUNDER_ADDRESS=0x...
PM_SIGNATURE_TYPE=1                # 1 = Polymarket Proxy wallet

# Proxy — required for placing orders from a geoblocked region
PMPROXY_URL=https://<...>.lambda-url.eu-west-1.on.aws
PMPROXY_USERNAME=...
PMPROXY_PASSWORD=...
PMPROXY_COGNITO_CLIENT_ID=...
PMPROXY_COGNITO_REGION=eu-west-1
```

## Tests

```bash
uv run pytest tests/
```
