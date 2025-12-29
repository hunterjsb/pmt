# pmt
polymarket trader

## client
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
