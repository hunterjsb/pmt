# Polymarket Trading Strategies

This directory contains trading strategies and scanners for Polymarket.

## Market Scanner

The market scanner (`scanner.py`) continuously monitors Polymarket for trading opportunities based on outcome probabilities.

### Low-Probability Scanner

Scans for markets where one outcome is priced between 1-3% (or custom range). These low-probability events can represent:
- Potential mispricing opportunities
- Long-shot bets with favorable odds
- Arbitrage possibilities

### Usage

**Single scan:**
```bash
uv run python scan.py --once
```

**Continuous scanning:**
```bash
uv run python scan.py
```

**Custom parameters:**
```bash
# Scan for 2-5% outcomes every 60 seconds
uv run python scan.py --min 2.0 --max 5.0 --interval 60

# Run 10 scans then stop
uv run python scan.py --limit 10
```

### Command-Line Options

- `--min <float>`: Minimum price percentage (default: 1.0)
- `--max <float>`: Maximum price percentage (default: 3.0)
- `--interval <int>`: Seconds between scans (default: 30)
- `--once`: Run once and exit (don't loop)
- `--limit <int>`: Maximum number of scans (default: infinite)

### Example Output

```
🎯 Low-Probability Opportunities (1-3%)
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Market                                   ┃ Outcome ┃ Price ┃ Implied Odds ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━┩
│ Will UCLA win the 2026 NCAA Tournament?  │   Yes   │ 2.15% │      1 in 46 │
│ Will Trump deport 500,000-750,000 people? │   Yes   │ 2.05% │      1 in 48 │
│ Negative GDP growth in Q4 2025?          │   Yes   │ 1.45% │      1 in 68 │
└──────────────────────────────────────────┴─────────┴───────┴──────────────┘
```

### Programmatic Usage

```python
from strategies.scanner import scan_once, find_low_prob_markets

# Get markets with 1-3% outcomes
opportunities = scan_once(min_pct=1.0, max_pct=3.0)

for opp in opportunities:
    print(f"{opp.market.question}: {opp.token.outcome} at {opp.price_pct:.2f}%")
```

## Future Strategies

This directory will contain additional trading strategies such as:
- Arbitrage detection
- Momentum trading
- Mean reversion
- Multi-market correlation analysis