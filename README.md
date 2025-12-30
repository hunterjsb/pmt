# pmt
polymarket trader

## Quick Start

### View Markets
```bash
uv run python main.py
```

### Scan for Opportunities
```bash
# Single scan for 1-3% outcomes
uv run python scan.py --once

# Continuous scanning every 30 seconds
uv run python scan.py

# Custom range and interval
uv run python scan.py --min 2.0 --max 5.0 --interval 60
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

## Market Scanner

The scanner finds trading opportunities by monitoring outcome probabilities:

```bash
# Scan for low-probability outcomes (1-3%)
uv run python scan.py --once

# Run continuously
uv run python scan.py --interval 30
```

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
│   ├── scanner.py       # Market opportunity scanner
│   └── README.md        # Strategy documentation
├── tests/               # Unit tests
│   ├── test_models.py   # Model tests
│   ├── test_formatting.py  # Formatting tests
│   └── README.md        # Testing documentation
├── .github/workflows/   # CI/CD configuration
├── formatting.py        # Console output utilities
├── main.py              # Market viewer demo
└── scan.py              # Scanner CLI
```
