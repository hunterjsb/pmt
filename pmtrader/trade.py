"""Trading script for placing orders on Polymarket.

This script demonstrates how to place trades using the authenticated CLOB client.

Setup:
1. Set environment variables in .env file:
   - PM_PRIVATE_KEY: Your Ethereum private key (with 0x prefix)
   - PM_FUNDER_ADDRESS: Your Ethereum address (with 0x prefix)
   - PM_SIGNATURE_TYPE: Signature type (0 for EOA, 1 for Poly Proxy, 2 for EIP-1271)

2. Ensure you have USDC on Polygon network in your wallet

3. Run: uv run python trade.py
"""

import sys

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from polymarket import clob

console = Console()


def show_balance(client):
    """Display current USDC balance and positions."""
    console.print("\n[bold cyan]💰 Account Overview[/bold cyan]")

    # USDC balance
    usdc_balance = client.usdc_balance()
    console.print(f"   USDC Balance: [green]${usdc_balance:,.2f}[/green]")

    # Open positions
    positions = client.positions()
    if positions:
        console.print(f"\n   [bold]Open Positions:[/bold]")
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Market", style="cyan", max_width=40)
        table.add_column("Outcome", style="yellow")
        table.add_column("Shares", style="green", justify="right")

        for pos in positions:
            market_preview = (
                pos["market"][:40] + "..." if len(pos["market"]) > 40 else pos["market"]
            )
            table.add_row(
                market_preview,
                pos["outcome"],
                f"{pos['shares']:,.2f}",
            )
        console.print(table)
    else:
        console.print("   [dim]No open positions[/dim]")


def show_order_book(token_id: str, outcome: str):
    """Display order book for a token."""
    console.print(f"\n[bold]📖 Order Book: {outcome}[/bold]")

    book = clob.order_book(token_id, outcome)

    # Show top 5 bids and asks
    console.print("\n   [green]Bids (Buy orders):[/green]")
    for bid in book.bids[:5]:
        console.print(f"     {bid.price:.1%} × {bid.size:,.0f} shares")

    console.print("\n   [red]Asks (Sell orders):[/red]")
    for ask in book.asks[:5]:
        console.print(f"     {ask.price:.1%} × {ask.size:,.0f} shares")

    # Show spread
    if book.bids and book.asks:
        spread = book.asks[0].price - book.bids[0].price
        console.print(f"\n   [yellow]Spread: {spread:.1%}[/yellow]")


def place_limit_order(client, token_id: str, side: str):
    """Interactive limit order placement."""
    console.print(f"\n[bold cyan]📝 Place {side} Limit Order[/bold cyan]")

    # Get price
    price_str = Prompt.ask("   Enter price (e.g., 0.65 for 65¢)")
    try:
        price = float(price_str)
        if not 0 < price < 1:
            console.print("[red]Price must be between 0 and 1[/red]")
            return
    except ValueError:
        console.print("[red]Invalid price[/red]")
        return

    # Get size
    size_str = Prompt.ask("   Enter number of shares")
    try:
        size = float(size_str)
        if size <= 0:
            console.print("[red]Size must be positive[/red]")
            return
    except ValueError:
        console.print("[red]Invalid size[/red]")
        return

    # Calculate cost
    cost = price * size
    console.print(f"\n   [yellow]Order Summary:[/yellow]")
    console.print(f"     Side: {side}")
    console.print(f"     Price: {price:.1%}")
    console.print(f"     Size: {size:,.2f} shares")
    console.print(f"     Total: ${cost:,.2f}")

    # Confirm
    if not Confirm.ask("\n   Place this order?"):
        console.print("[yellow]Order cancelled[/yellow]")
        return

    # Place order
    try:
        console.print("\n   [dim]Placing order...[/dim]")
        result = client.post_order(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        console.print(f"   [green]✓ Order placed successfully![/green]")
        console.print(f"   [dim]Order ID: {result.get('orderID', 'N/A')}[/dim]")
    except Exception as e:
        console.print(f"   [red]✗ Error placing order: {e}[/red]")


def place_market_order(client, token_id: str, side: str):
    """Interactive market order placement."""
    console.print(f"\n[bold cyan]⚡ Place {side} Market Order[/bold cyan]")

    if side == "BUY":
        amount_str = Prompt.ask("   Enter dollar amount to spend")
    else:
        amount_str = Prompt.ask("   Enter number of shares to sell")

    try:
        amount = float(amount_str)
        if amount <= 0:
            console.print("[red]Amount must be positive[/red]")
            return
    except ValueError:
        console.print("[red]Invalid amount[/red]")
        return

    # Show warning
    console.print(
        "\n   [yellow]⚠️  Market orders execute immediately at best available price[/yellow]"
    )

    # Confirm
    if not Confirm.ask("\n   Place this market order?"):
        console.print("[yellow]Order cancelled[/yellow]")
        return

    # Place order
    try:
        console.print("\n   [dim]Placing market order...[/dim]")
        result = client.market_order(
            token_id=token_id,
            amount=amount,
            side=side,
        )
        console.print(f"   [green]✓ Market order executed![/green]")
        console.print(f"   [dim]Order ID: {result.get('orderID', 'N/A')}[/dim]")
    except Exception as e:
        console.print(f"   [red]✗ Error placing order: {e}[/red]")


def show_open_orders(client):
    """Display and manage open orders."""
    orders = client.open_orders()

    if not orders:
        console.print("\n[dim]No open orders[/dim]")
        return

    console.print(f"\n[bold]📋 Open Orders ({len(orders)})[/bold]")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=3)
    table.add_column("Market", style="cyan", max_width=30)
    table.add_column("Side", style="yellow")
    table.add_column("Price", style="green", justify="right")
    table.add_column("Size", style="blue", justify="right")

    for idx, order in enumerate(orders[:10], 1):
        market = order.get("market", "Unknown")[:30]
        side = order.get("side", "?")
        price = float(order.get("price", 0))
        size = float(order.get("size", 0))

        table.add_row(
            str(idx),
            market,
            side,
            f"{price:.1%}",
            f"{size:,.0f}",
        )

    console.print(table)

    # Option to cancel
    if Confirm.ask("\nCancel an order?"):
        choice = Prompt.ask("Enter order number to cancel (or 'all' for all orders)")

        if choice.lower() == "all":
            if Confirm.ask("Cancel ALL orders?", default=False):
                try:
                    client.cancel_all()
                    console.print("[green]✓ All orders cancelled[/green]")
                except Exception as e:
                    console.print(f"[red]✗ Error: {e}[/red]")
        else:
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(orders):
                    order_id = orders[idx].get("id")
                    client.cancel(order_id)
                    console.print(f"[green]✓ Order cancelled[/green]")
                else:
                    console.print("[red]Invalid order number[/red]")
            except (ValueError, Exception) as e:
                console.print(f"[red]Error: {e}[/red]")


def main():
    """Main trading interface."""
    console.print("[bold cyan]🎯 Polymarket Trading Interface[/bold cyan]")

    # Create authenticated client
    from polymarket.clob import create_authenticated_clob

    client = create_authenticated_clob()

    if not client:
        console.print(
            "[red]Error: Could not create authenticated client.[/red]\n"
            "Please ensure PM_PRIVATE_KEY and PM_FUNDER_ADDRESS are set in your .env file."
        )
        sys.exit(1)

    console.print("[green]✓ Connected to Polymarket[/green]")

    # Show balance
    try:
        show_balance(client)
    except Exception as e:
        console.print(f"[red]Error fetching balance: {e}[/red]")

    console.print("\n[bold]Available Actions:[/bold]")
    console.print("   1. View order book for a token")
    console.print("   2. Place limit order")
    console.print("   3. Place market order")
    console.print("   4. View/cancel open orders")
    console.print("   5. Refresh balance")
    console.print("   0. Exit")

    while True:
        console.print()
        choice = Prompt.ask(
            "[bold]Select action[/bold]", choices=["0", "1", "2", "3", "4", "5"]
        )

        if choice == "0":
            console.print("[cyan]Goodbye! 👋[/cyan]")
            break

        elif choice == "1":
            token_id = Prompt.ask("Enter token ID")
            outcome = Prompt.ask("Enter outcome name", default="Yes")
            try:
                show_order_book(token_id, outcome)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

        elif choice == "2":
            token_id = Prompt.ask("Enter token ID")
            side = Prompt.ask("Side", choices=["BUY", "SELL"])
            place_limit_order(client, token_id, side)

        elif choice == "3":
            token_id = Prompt.ask("Enter token ID")
            side = Prompt.ask("Side", choices=["BUY", "SELL"])
            place_market_order(client, token_id, side)

        elif choice == "4":
            try:
                show_open_orders(client)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")

        elif choice == "5":
            try:
                show_balance(client)
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")


if __name__ == "__main__":
    main()
