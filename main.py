from env import PM_BUILDER_NAME
from polymarket import clob, gamma


def display_market(m: dict) -> None:
    """Display a market with its tokens."""
    question = m.get("question", "Unknown")
    print(f"\n  • {question[:60]}{'...' if len(question) > 60 else ''}")
    for t in m.get("tokens", []):
        price = t.get("price")
        price_str = f"{price:.1%}" if price is not None else "N/A"
        token_id = t.get("token_id", "")
        print(f"    [{t.get('outcome', '?')}] {price_str} | {token_id[:30]}...")


def display_order_book(book, name: str = "Token") -> None:
    """Display order book summary."""
    print(f"\n  📈 {name} Order Book:")

    bids = book.bids or []
    asks = book.asks or []
    print(f"     Depth: {len(bids)} bids, {len(asks)} asks")

    if bids:
        top = bids[0]
        print(f"     Top Bid: {float(top.price):.1%} x {float(top.size):,.0f} shares")
    if asks:
        top = asks[0]
        print(f"     Top Ask: {float(top.price):.1%} x {float(top.size):,.0f} shares")


def display_event(e: dict) -> None:
    """Display an event summary."""
    print(f"\n{'=' * 60}")
    print(f"📊 {e.get('title', 'Unknown')}")
    print(f"   Slug: {e.get('slug', 'N/A')}")
    print(f"   End Date: {e.get('endDate', 'N/A')}")
    liquidity = e.get("liquidity")
    volume = e.get("volume")
    if liquidity:
        print(f"   Liquidity: ${float(liquidity):,.0f}")
    if volume:
        print(f"   Volume: ${float(volume):,.0f}")


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

    for m in markets[:3]:
        display_market(m)

    # Show order book for first market
    print("\n" + "-" * 60)
    print("📖 ORDER BOOK EXAMPLE")
    print("-" * 60)

    if markets and markets[0].get("tokens"):
        m = markets[0]
        t = m["tokens"][0]
        print(f"\nMarket: {m.get('question', 'Unknown')[:60]}...")

        book = clob.order_book(t["token_id"])
        display_order_book(book, t.get("outcome", "Token"))

    # Show events from Gamma API
    print("\n" + "-" * 60)
    print("📈 RECENT EVENTS (from Gamma API)")
    print("-" * 60)

    events = gamma.events(limit=3)
    print(f"Found {len(events)} events")

    for e in events:
        display_event(e)

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
