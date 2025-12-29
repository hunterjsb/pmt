from env import PM_BUILDER_NAME
from polymarket import clob, gamma


def main() -> None:
    print(f"Hello {PM_BUILDER_NAME}!")
    print("\n" + "=" * 60)
    print("🔍 Polymarket Market Viewer")
    print("=" * 60)

    # Check CLOB status
    print(f"\n✅ CLOB Status: {clob.ok()}")
    print(f"⏰ Server Time: {clob.server_time()}")

    # Get sampling markets (active markets with order books)
    print("\n" + "-" * 60)
    print("📊 SAMPLING MARKETS (active, with order books)")
    print("-" * 60)

    markets = clob.sampling_markets(limit=5)
    print(f"Found {len(markets)} markets")

    for market in markets[:3]:
        print(market)

    # Show order book for first market
    print("\n" + "-" * 60)
    print("📖 ORDER BOOK EXAMPLE")
    print("-" * 60)

    if markets and markets[0].tokens:
        market = markets[0]
        token = market.tokens[0]
        print(f"\nMarket: {market.question[:60]}...")

        book = clob.order_book(token.token_id, token.outcome)
        print(book)

    # Show events from Gamma API
    print("\n" + "-" * 60)
    print("📈 RECENT EVENTS (from Gamma API)")
    print("-" * 60)

    events = gamma.events(limit=3)
    print(f"Found {len(events)} events")

    for event in events:
        print(event)

    # Show available functionality
    print("\n\n" + "=" * 60)
    print("📚 USAGE")
    print("=" * 60)
    print("""
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
    """)


if __name__ == "__main__":
    main()
