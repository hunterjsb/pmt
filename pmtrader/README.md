# pmtrader

Python SDK + CLI for Polymarket. Browse markets, place trades, monitor positions, and run scanners — all from the terminal. Optionally routes through `pmproxy` (Cognito-authed Lambda in eu-west-1) to bypass the US geoblock.

## CLI tools

All run from inside `pmtrader/` via `uv run python <name>.py`.

| Script | Purpose |
|---|---|
| `main.py` | Quick market browser — top sampling markets + an example order book |
| `scan.py` | Opportunity scanners (`scan.py cliff`, `scan.py expiring`) |
| `trade.py` | Interactive trade prompt — balances, positions, place an order |
| `redeem.py` | Redeem winning shares from resolved markets |
| **`portfolio.py`** | **Positions + P&L + exposure by side + theme correlation; `--orders` adds open resting orders** |
| **`rewards.py`** | **REWARD (maker liquidity) + YIELD (interest on cash) income history** |

### portfolio.py

```bash
uv run python portfolio.py                              # positions + theme exposure
uv run python portfolio.py --orders                     # also pull resting orders + locked capital
uv run python portfolio.py --themes hantavirus,vaccine  # filter themes
```

Themes are title-keyword regexes defined at the top of `portfolio.py` — edit `DEFAULT_THEMES` to add your own.

### rewards.py

```bash
uv run python rewards.py             # last 30 days
uv run python rewards.py --all       # full history
uv run python rewards.py --type yield
uv run python rewards.py --days 1    # check if today's REWARDs landed
```

## Package layout

```
pmtrader/
├── polymarket/           # SDK
│   ├── clob.py           # v1 ClobClient + AuthenticatedClob (positions, balances, RPC)
│   ├── clob_v2.py        # v2 authenticated client (orders) — used by strategies
│   ├── cognito.py        # Cognito JWT auth for pmproxy
│   ├── gamma.py          # gamma-api wrapper (events, market metadata, search)
│   ├── markets.py        # token-ID ↔ market-name lookup
│   └── models.py         # Market, Token, OrderBook, Event dataclasses
├── strategies/           # Trade entry scripts (place orders)
├── scanners/             # Opportunity scanners (read-only)
├── ui/                   # Streamlit dashboard (`uv run pmtrader-ui`)
└── tests/
```

## Routing through pmproxy

Polymarket geoblocks US IPs. Set `PMPROXY_URL` in `.env` and the CLOB/Gamma/RPC calls route through the eu-west-1 Lambda. Cognito creds (`PMPROXY_USERNAME`, `PMPROXY_PASSWORD`, etc.) are auto-attached as Bearer tokens.

See `../.infra/INFRA.md` (gitignored) for the live URL + current secrets.

## Required env (`.env` at repo root)

```bash
# Trading identity
PM_PRIVATE_KEY=0x...
PM_FUNDER_ADDRESS=0x...
PM_SIGNATURE_TYPE=1               # 1 = Polymarket Proxy wallet

# Proxy (optional but required for placing orders from US)
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
