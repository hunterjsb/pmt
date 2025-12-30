# pmt
polymarket trader

## Quick Start

### View Markets
```bash
uv run python main.py
```

### Scan for Opportunities

#### Volume Cliff Scanner
```bash
# Single scan for volume cliffs (85-99% outcomes)
uv run python scan.py cliff --once

# Continuous scanning every 30 seconds
uv run python scan.py cliff --interval 30

# Custom range and interval
uv run python scan.py cliff --min 85.0 --max 99.0 --interval 60
```

#### Expiring Markets Scanner
```bash
# Find 98%+ certain outcomes expiring within 2 hours
uv run python scan.py expiring --once

# Continuous scanning with custom criteria
uv run python scan.py expiring --min-price 95 --max-hours 24 --interval 60

# Verbose mode to see what's being scanned
uv run python scan.py expiring --once --verbose
```

## API Client
```python
from polymarket import clob, gamma

# CLOB API (trading data)
clob.sampling_markets(limit=10)  # Active markets with order books
clob.order_book(token_id)        # Full order book
clob.midpoint(token_id)          # {'mid': '0.123'}
clob.price(token_id, "BUY")      # {'price': '0.123'}
clob.spread(token_id)            # (bid_result, ask_result)

# Gamma API (market metadata)
gamma.events(limit=10)           # Get events
gamma.event_by_slug(slug)        # Specific event
gamma.markets(limit=10)          # Market data
gamma.tags()                     # Available categories
gamma.search(query)              # Search markets
```

## Trading Strategies

The scanner supports multiple trading strategies:

### Volume Cliff Scanner
Finds order book inefficiencies where thin ask levels are followed by thick levels. Buy at the thin levels and resell just below the cliff.

### Expiring Markets Scanner
Identifies high-certainty outcomes (98%+) on markets expiring soon (default: 2 hours). These offer quick, low-risk returns as "almost certain" outcomes resolve.

See [strategies/README.md](strategies/README.md) for more details.

## Testing

Run the test suite:
```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run specific test file
uv run pytest tests/test_models.py
```

See [tests/README.md](tests/README.md) for more details.

## CI/CD

GitHub Actions automatically runs tests on every push and pull request. See `.github/workflows/ci.yml` for configuration.

## Project Structure

```
pmt/
├── polymarket/          # API client modules
│   ├── models.py        # Domain models (Market, Event, OrderBook, etc.)
│   ├── clob.py          # CLOB API client
│   ├── gamma.py         # Gamma API client
│   └── __init__.py      # Public exports
├── strategies/          # Trading strategies
│   ├── scanner.py       # Volume cliff scanner
│   ├── expiring.py      # Expiring markets scanner
│   └── README.md        # Strategy documentation
├── tests/               # Unit tests
│   ├── test_models.py   # Business logic tests
│   └── README.md        # Testing documentation
├── .github/workflows/   # CI/CD configuration
├── formatting.py        # Console output utilities
├── main.py              # Market viewer demo
└── scan.py              # Scanner CLI
```
