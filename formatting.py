"""Console formatting utilities using rich."""

from rich.console import Console
from rich.panel import Panel

# from rich.table import Table

console = Console()


def header(text: str) -> None:
    """Print a main header."""
    console.print()
    console.print(f"[bold cyan]{text}[/bold cyan]")
    console.print("=" * 60)


def section(text: str) -> None:
    """Print a section header."""
    console.print()
    console.print("-" * 60)
    console.print(f"[bold]{text}[/bold]")
    console.print("-" * 60)


def info(label: str, value: str) -> None:
    """Print a labeled info line."""
    console.print(f"[green]✓[/green] {label}: {value}")


def usage_panel() -> None:
    """Print the usage information in a panel."""
    usage_text = """[bold]CLOB API[/bold] (trading data)
clob.sampling_markets(limit=10)  # Active markets with order books
clob.order_book(token_id)        # Full order book
clob.midpoint(token_id)          # {'mid': '0.123'}
clob.price(token_id, "BUY")      # {'price': '0.123'}
clob.spread(token_id)            # (bid_result, ask_result)

[bold]Gamma API[/bold] (market metadata)
gamma.events(limit=10)           # Get events
gamma.event_by_slug(slug)        # Specific event
gamma.markets(limit=10)          # Market data
gamma.tags()                     # Available categories
gamma.search(query)              # Search markets"""

    console.print()
    panel = Panel(usage_text, title="[bold]📚 USAGE[/bold]", border_style="cyan")
    console.print(panel)
