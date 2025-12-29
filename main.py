from env import PM_BUILDER_NAME
from formatting import console, header, info, section, usage_panel
from polymarket import clob, gamma


def main() -> None:
    console.print(f"[bold]Hello {PM_BUILDER_NAME}![/bold]")
    header("🔍 Polymarket Market Viewer")

    # Check CLOB status
    console.print()
    info("CLOB Status", clob.ok())
    info("Server Time", str(clob.server_time()))

    # Get sampling markets (active markets with order books)
    section("📊 SAMPLING MARKETS (active, with order books)")

    markets = clob.sampling_markets(limit=5)
    console.print(f"Found {len(markets)} markets")

    for market in markets[:3]:
        console.print(market)

    # Show order book for first market
    section("📖 ORDER BOOK EXAMPLE")

    if markets and markets[0].tokens:
        market = markets[0]
        token = market.tokens[0]
        console.print(f"\nMarket: {market.question[:60]}...")

        book = clob.order_book(token.token_id, token.outcome)
        console.print(book)

    # Show events from Gamma API
    section("📈 RECENT EVENTS (from Gamma API)")

    events = gamma.events(limit=3)
    console.print(f"Found {len(events)} events")

    for event in events:
        console.print(event)

    # Show available functionality
    usage_panel()


if __name__ == "__main__":
    main()
