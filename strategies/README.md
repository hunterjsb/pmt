# Polymarket Trading Strategies

This directory contains trading strategies and scanners for Polymarket.

## Available Strategies

### 1. Volume Cliff Scanner (`scanner.py`)

Finds order book inefficiencies where thin ask levels are followed by significantly thicker levels, creating a "volume cliff."

**Strategy:** Buy at the thin levels and resell just below the cliff for quick arbitrage profits.

**Example:**
- $100 @ 93¢ (thin)
- $150 @ 94¢ (thin)  
- $3,000 @ 96¢ (CLIFF!)
- → Buy at 93-94¢, resell at 95¢

**Usage:**
```bash
# Single scan for volume cliffs
uv run python scan.py cliff --once

# Continuous scanning every 30 seconds
uv run python scan.py cliff --interval 30

# Custom parameters
uv run python scan.py cliff --min 85 --max 99 --volume-jump 2000 --price-gap 2.0
```

**Parameters:**
- `--min <float>`: Minimum outcome price % (default: 85.0)
- `--max <float>`: Maximum outcome price % (default: 99.0)
- `--volume-jump <float>`: Min dollar value jump for a cliff (default: 2000.0)
- `--price-gap <float>`: Min price gap in cents (default: 2.0)
- `--interval <int>`: Seconds between scans (default: 30)
- `--once`: Run once and exit
- `--limit <int>`: Max number of scans

---

### 2. Expiring Markets Scanner (`expiring.py`)

Finds high-certainty outcomes (98%+) on markets expiring soon (default: 2 hours or less).

**Strategy:** Bet on "almost certain" outcomes that will resolve quickly, offering low-risk returns with fast capital turnover.

**Example:**
- Market: "Will Bitcoin be above $90K on Dec 31?"
- Current price: $95K, expires in 1.5 hours
- Outcome "Yes" trading at 98.5%
- Max return: 1.52% in 1.5 hours (≈1.01%/hour)

**Usage:**
```bash
# Find 98%+ outcomes expiring within 2 hours
uv run python scan.py expiring --once

# Continuous scanning with custom criteria
uv run python scan.py expiring --min-price 95 --max-hours 24 --interval 60

# Verbose mode to see scanning details
uv run python scan.py expiring --once --verbose
```

**Parameters:**
- `--min-price <float>`: Minimum outcome price % (default: 98.0)
- `--max-hours <float>`: Maximum hours until expiry (default: 2.0)
- `--interval <int>`: Seconds between scans (default: 60)
- `--once`: Run once and exit
- `--verbose`: Show detailed scanning info

**Key Metrics:**
- **Max Return**: Potential profit if outcome resolves to 100%
- **Hourly Rate**: Annualized return per hour
- **Break-even Drop**: How much price can drop before losing money

---

## Programmatic Usage

### Volume Cliff Scanner

```python
from strategies.scanner import find_volume_cliff_opportunities, scan_once
from polymarket import clob

# Get active markets
markets = clob.sampling_markets(limit=100)

# Find volume cliff opportunities
opportunities = find_volume_cliff_opportunities(
    markets,
    min_pct=85.0,
    max_pct=99.0,
    min_volume_jump=2000.0,
    min_price_gap_cents=2.0
)

for opp in opportunities:
    print(f"{opp.market.question}")
    print(f"  Buy levels: {opp.buy_levels}")
    print(f"  Cliff at: {opp.cliff_level}")
    print(f"  Resale price: {opp.potential_resale_price:.1%}")
```

### Expiring Markets Scanner

```python
from strategies.expiring import find_expiring_opportunities, calculate_max_return

# Find high-certainty expiring markets
opportunities = find_expiring_opportunities(
    min_price_pct=98.0,
    max_hours=2.0
)

for opp in opportunities:
    returns = calculate_max_return(opp.price_pct, opp.hours_until_expiry)
    
    print(f"{opp.market.question}")
    print(f"  {opp.token.outcome} @ {opp.price_pct:.2f}%")
    print(f"  Expires in: {opp.hours_until_expiry:.1f} hours")
    print(f"  Max return: {returns['max_return_pct']:.2f}%")
    print(f"  Hourly rate: {returns['hourly_rate_pct']:.2f}%/hr")
```

---

## Strategy Selection Guide

| Strategy | Risk Level | Time Horizon | Capital Required | Best For |
|----------|-----------|--------------|------------------|----------|
| **Volume Cliff** | Medium | Minutes-Hours | Medium-High | Spotting order book inefficiencies |
| **Expiring Markets** | Low | Minutes-Hours | Low-High | Quick, near-certain returns |

---

## Future Strategies

Potential additions:
- Arbitrage detection (cross-market opportunities)
- Momentum trading (trending markets)
- Mean reversion (contrarian plays)
- Multi-market correlation analysis
- Whale watching (large order detection)