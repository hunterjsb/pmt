"""pmt — unified CLI for placing trades, viewing positions, and tracking rewards.

Routes through the pmproxy Lambda (Cognito-authed) using PolymarketAPI under
the hood.

Subcommands:
    pmt buy --token TOKEN --price PRICE --size SIZE [--tick TICK]
    pmt sell --token TOKEN --price PRICE --size SIZE [--tick TICK]
    pmt flip --token TOKEN --buy-price BP --sell-price SP --size SIZE [--tick TICK]
    pmt cancel ORDER_ID
    pmt orders                              # open orders
    pmt positions [--orders] [--themes ...] # portfolio view
    pmt rewards [--days N] [--all] [--type ...]
    pmt market SLUG_OR_CONDITION_ID         # market lookup
    pmt search QUERY                        # free-text search
    pmt book TOKEN                          # order book
"""

from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import click
import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

console = Console()

# Theme keyword regexes for `pmt positions` — same set portfolio.py used.
DEFAULT_THEMES = {
    "hantavirus": r"\bhantavirus\b",
    "pandemic": r"\bpandemic\b",
    "vaccine": r"\bvaccine\b",
    "coronavirus": r"\bcoronavirus\b|\bcovid\b",
    "btc": r"\bbitcoin\b|\bBTC\b",
    "eth": r"\bethereum\b|\bETH\b|\bether\b",
    "ai": r"\bAGI\b|\bOpenAI\b|\bAnthropic\b|\bChatGPT\b",
    "bank-fail": r"\bfail by\b",
    "trump": r"\bTrump\b|\bDJT\b",
}


def _pnl_color(pnl: float) -> str:
    return "green" if pnl >= 0 else "red"


def _api():
    """Lazy-load PolymarketAPI so commands that don't need auth (search, market,
    book) can run without a configured proxy."""
    load_dotenv()
    from polymarket import PolymarketAPI

    return PolymarketAPI()


# ============================================================
# Order placement
# ============================================================


def _resolve_token(value: str) -> str:
    """Accept either a numeric token ID, or 'market:side' (e.g. 'hantavirus-pandemic:no').

    Markets are looked up in polymarket.markets (attributes like HANTAVIRUS_PANDEMIC).
    """
    if value.isdigit():
        return value
    if ":" in value:
        market_name, side = value.split(":", 1)
        from polymarket import markets as M

        attr = market_name.upper().replace("-", "_")
        m = getattr(M, attr, None)
        if m is None:
            available = [a for a in dir(M) if hasattr(getattr(M, a), "yes_token")]
            raise click.BadParameter(
                f"Unknown market '{market_name}'. Known: {', '.join(available)}"
            )
        return m.yes_token if side.lower().startswith("y") else m.no_token
    raise click.BadParameter(
        f"--token must be a numeric token ID or 'market:side' (e.g. hantavirus-pandemic:no)"
    )


@click.group()
def cli() -> None:
    """pmtrader unified CLI."""


@cli.command()
@click.option("--token", required=True, help="Token ID OR 'market-name:yes|no' (e.g. hantavirus-pandemic:no)")
@click.option("--price", required=True, type=float)
@click.option("--size", required=True, type=int)
@click.option("--tick", default=None, help="Tick size override (auto-detected if omitted)")
@click.option("--dry-run", is_flag=True, help="Print spec, don't submit")
def buy(token: str, price: float, size: int, tick: str | None, dry_run: bool) -> None:
    """Place a BUY order. At-or-above best ask → taker; below → maker."""
    token = _resolve_token(token)
    notional = price * size
    console.print(f"BUY {size} @ ${price}  notional ${notional:.4f}")
    if dry_run:
        console.print("[dim]dry-run[/dim]")
        return
    resp = _api().place_buy(token=token, price=price, size=size, tick_size=tick)
    click.echo(json.dumps(resp, indent=2, default=str))


@cli.command()
@click.option("--token", required=True)
@click.option("--price", required=True, type=float)
@click.option("--size", required=True, type=int)
@click.option("--tick", default=None)
@click.option("--dry-run", is_flag=True)
def sell(token: str, price: float, size: int, tick: str | None, dry_run: bool) -> None:
    """Place a SELL order."""
    token = _resolve_token(token)
    notional = price * size
    console.print(f"SELL {size} @ ${price}  notional ${notional:.4f}")
    if dry_run:
        console.print("[dim]dry-run[/dim]")
        return
    resp = _api().place_sell(token=token, price=price, size=size, tick_size=tick)
    click.echo(json.dumps(resp, indent=2, default=str))


@cli.command()
@click.option("--token", required=True)
@click.option("--buy-price", required=True, type=float)
@click.option("--sell-price", required=True, type=float)
@click.option("--size", required=True, type=int)
@click.option("--tick", default=None)
@click.option("--dry-run", is_flag=True)
def flip(
    token: str, buy_price: float, sell_price: float, size: int, tick: str | None, dry_run: bool
) -> None:
    """Two-leg trade: taker BUY, then maker SELL with settlement-lag retry."""
    token = _resolve_token(token)
    console.print(
        f"FLIP  buy {size}@${buy_price}  →  sell @${sell_price}  "
        f"max profit ${size*(sell_price-buy_price):.2f}"
    )
    if dry_run:
        console.print("[dim]dry-run[/dim]")
        return
    r = _api().flip(
        token=token, buy_price=buy_price, sell_price=sell_price, size=size, tick_size=tick
    )
    click.echo(
        json.dumps(
            {
                "buy_id": r.buy_id,
                "sell_id": r.sell_id,
                "buy_filled": r.buy_filled,
                "cost": r.cost,
                "sell_status": r.sell_status,
                "potential_profit": r.potential_profit,
            },
            indent=2,
            default=str,
        )
    )


@cli.command()
@click.argument("order_id")
def cancel(order_id: str) -> None:
    """Cancel an open order by ID."""
    resp = _api().cancel(order_id)
    click.echo(json.dumps(resp, indent=2, default=str))


# ============================================================
# Reads — orders, positions, rewards
# ============================================================


def _known_token_labels() -> dict[str, str]:
    """Map token_id → "Market name (YES|NO)" using polymarket.markets."""
    from polymarket import markets as M

    labels: dict[str, str] = {}
    for attr in dir(M):
        obj = getattr(M, attr)
        if not hasattr(obj, "yes_token"):
            continue
        labels[obj.yes_token] = f"{obj.name[:30]} YES"
        labels[obj.no_token] = f"{obj.name[:30]} NO"
    return labels


def _label_for_order(order: dict, known: dict[str, str]) -> str:
    """Best-effort market label using static map, then dynamic CLOB lookup."""
    asset = str(order.get("asset_id", ""))
    if asset in known:
        return known[asset]
    cid = order.get("market")
    if cid:
        from polymarket.api import lookup_market_name

        name = lookup_market_name(cid)
        if name:
            outcome = (order.get("outcome", "") or "").upper()
            return f"{name[:30]} {outcome}"
    return "…" + asset[-10:]


@cli.command()
def orders() -> None:
    """List open resting orders."""
    rows = _api().get_orders()
    labels = _known_token_labels()
    if not rows:
        console.print("[dim]No open orders.[/dim]")
        return
    table = Table(title="Open Orders")
    for col in ("Side", "Size", "Price", "Notional", "Market", "ID"):
        table.add_column(col)
    locked_cash = 0.0
    for d in rows:
        side = d.get("side", "?")
        size = float(d.get("original_size", 0))
        price = float(d.get("price", 0))
        notional = size * price
        if side == "BUY":
            locked_cash += notional
        market = _label_for_order(d, labels)
        table.add_row(
            side,
            f"{size:.0f}",
            f"${price:.4f}",
            f"${notional:.2f}",
            market[:34],
            str(d.get("id", ""))[:12],
        )
    table.add_section()
    table.add_row("", "", "[bold]Locked[/bold]", f"[bold]${locked_cash:.2f}[/bold]", "", "")
    console.print(table)


@cli.command()
@click.option("--orders/--no-orders", "with_orders", default=False, help="Include open orders")
@click.option("--themes", default=None, help="Comma-separated theme names")
def positions(with_orders: bool, themes: str | None) -> None:
    """Portfolio view: positions, exposure, theme correlation."""
    api = _api()
    raw = api.get_positions()
    if not raw:
        console.print("[dim]No positions.[/dim]")
        return

    table = Table(title="Positions")
    for col in ("Market", "Side", "Size", "Avg", "Cur", "Cost", "Value", "PnL"):
        table.add_column(col, justify="right" if col not in ("Market", "Side") else "left")
    total_cost = total_value = total_pnl = 0.0
    for p in sorted(raw, key=lambda x: x.get("cashPnl", 0)):
        cost = p["size"] * p["avgPrice"]
        pnl = p.get("cashPnl", p["currentValue"] - cost)
        c = _pnl_color(pnl)
        total_cost += cost
        total_value += p["currentValue"]
        total_pnl += pnl
        table.add_row(
            p["title"][:42],
            p["outcome"],
            f"{p['size']:.0f}",
            f"${p['avgPrice']:.4f}",
            f"${p['curPrice']:.4f}",
            f"${cost:.2f}",
            f"${p['currentValue']:.2f}",
            f"[{c}]${pnl:+.2f}[/{c}]",
        )
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]", "", "", "", "",
        f"[bold]${total_cost:.2f}[/bold]",
        f"[bold]${total_value:.2f}[/bold]",
        f"[bold {_pnl_color(total_pnl)}]${total_pnl:+.2f}[/]",
    )
    console.print(table)

    # Exposure by side
    by_side: dict = defaultdict(lambda: {"cost": 0.0, "value": 0.0})
    for p in raw:
        cost = p["size"] * p["avgPrice"]
        by_side[p["outcome"]]["cost"] += cost
        by_side[p["outcome"]]["value"] += p["currentValue"]
    sidetable = Table(title="Exposure by Side")
    for col in ("Side", "Cost", "Value", "PnL"):
        sidetable.add_column(col, justify="right" if col != "Side" else "left")
    for side, x in sorted(by_side.items()):
        pnl = x["value"] - x["cost"]
        sidetable.add_row(side, f"${x['cost']:.2f}", f"${x['value']:.2f}",
                          f"[{_pnl_color(pnl)}]${pnl:+.2f}[/]")
    console.print()
    console.print(sidetable)

    # Theme groups
    chosen = DEFAULT_THEMES
    if themes:
        wanted = {t.strip() for t in themes.split(",")}
        chosen = {k: v for k, v in DEFAULT_THEMES.items() if k in wanted}
    th = Table(title="Exposure by Theme")
    for col in ("Theme", "Positions", "YES cost", "NO cost", "Net value", "PnL"):
        th.add_column(col, justify="right" if col not in ("Theme",) else "left")
    for name, pattern in chosen.items():
        rx = re.compile(pattern, re.IGNORECASE)
        matching = [p for p in raw if rx.search(p["title"])]
        if not matching:
            continue
        yes_cost = sum(p["size"] * p["avgPrice"] for p in matching if p["outcome"] == "Yes")
        no_cost = sum(p["size"] * p["avgPrice"] for p in matching if p["outcome"] == "No")
        value = sum(p["currentValue"] for p in matching)
        pnl = value - (yes_cost + no_cost)
        th.add_row(
            name, str(len(matching)),
            f"${yes_cost:.2f}", f"${no_cost:.2f}",
            f"${value:.2f}", f"[{_pnl_color(pnl)}]${pnl:+.2f}[/]",
        )
    console.print()
    console.print(th)

    if with_orders:
        console.print()
        ctx = click.get_current_context()
        ctx.invoke(orders)


@cli.command()
@click.option("--days", default=30, type=int, help="Lookback window (default 30)")
@click.option("--all", "all_history", is_flag=True, help="No date filter")
@click.option("--type", "kind", default="all", type=click.Choice(["reward", "yield", "all"]))
def rewards(days: int, all_history: bool, kind: str) -> None:
    """Show REWARD (maker liquidity) and YIELD (interest on cash) income."""
    api = _api()
    since = None if all_history else (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp()

    def filt(events):
        return [e for e in events if since is None or e["timestamp"] >= since]

    if kind in ("reward", "all"):
        events = filt(api.get_activity(kind="REWARD", limit=500))
        total = sum(e["usdcSize"] for e in events)
        t = Table(title=f"REWARD events ({len(events)}, ${total:.4f})")
        for col in ("Date (UTC)", "Amount", "TX"):
            t.add_column(col)
        for e in events:
            ts = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc)
            t.add_row(ts.strftime("%Y-%m-%d %H:%M"), f"${e['usdcSize']:.4f}", e["transactionHash"][:14])
        console.print(t)

    if kind in ("yield", "all"):
        events = filt(api.get_activity(kind="YIELD", limit=500))
        by_month: dict[str, float] = defaultdict(float)
        for e in events:
            ts = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc)
            by_month[ts.strftime("%Y-%m")] += e["usdcSize"]
        total = sum(by_month.values())
        t = Table(title=f"YIELD by month ({len(events)}, ${total:.4f})")
        for col in ("Month", "USDC"):
            t.add_column(col)
        for m in sorted(by_month):
            t.add_row(m, f"${by_month[m]:.4f}")
        console.print(t)


# ============================================================
# Market discovery
# ============================================================


@cli.command()
@click.argument("slug_or_cid")
def market(slug_or_cid: str) -> None:
    """Look up a market by event slug or condition_id."""
    api = _api()
    m = api.get_market(slug_or_cid)
    if not m:
        console.print(f"[red]No market found for '{slug_or_cid}'[/red]")
        sys.exit(1)
    click.echo(json.dumps(m, indent=2, default=str))


@cli.command()
@click.argument("query")
@click.option("--keyword", default=None, help="Filter results to title keyword")
def search(query: str, keyword: str | None) -> None:
    """Free-text search across active events."""
    api = _api()
    events = api.search_markets(query)
    rows = []
    for e in events:
        if e.get("closed") or e.get("archived"):
            continue
        for m in e.get("markets", []) or []:
            if m.get("closed") or m.get("archived"):
                continue
            try:
                outcomes = json.loads(m.get("outcomes", "[]"))
                prices = json.loads(m.get("outcomePrices", "[]"))
                yes_p = float(prices[outcomes.index("Yes")]) if "Yes" in outcomes else None
            except (ValueError, KeyError):
                yes_p = None
            if yes_p is None:
                continue
            title = m.get("question", "")
            if keyword and keyword.lower() not in title.lower():
                continue
            rows.append({
                "title": title[:55],
                "yes_p": yes_p,
                "no_p": 1 - yes_p,
                "liq": float(m.get("liquidity", 0) or 0),
                "end": (m.get("endDate", "") or "")[:10],
                "cid": m.get("conditionId", ""),
            })
    rows.sort(key=lambda r: r["yes_p"])
    t = Table(title=f"Search: {query}")
    for col in ("YES", "NO", "Liq", "End", "Market"):
        t.add_column(col)
    for r in rows[:30]:
        t.add_row(f"${r['yes_p']:.3f}", f"${r['no_p']:.3f}", f"${r['liq']:.0f}", r["end"], r["title"])
    console.print(t)


@cli.command()
@click.argument("token")
def book(token: str) -> None:
    """Show order book for a token."""
    token = _resolve_token(token)
    api = _api()
    b = api.get_book(token)

    def _level(x):
        if hasattr(x, "price"):
            return float(x.price), float(x.size)
        return float(x["price"]), float(x["size"])

    raw_bids = (b.bids if hasattr(b, "bids") else b.get("bids", [])) or []
    raw_asks = (b.asks if hasattr(b, "asks") else b.get("asks", [])) or []
    bids = sorted([_level(x) for x in raw_bids], key=lambda x: x[0], reverse=True)[:8]
    asks = sorted([_level(x) for x in raw_asks], key=lambda x: x[0])[:8]

    t = Table(title=f"Book for {token[:12]}…")
    for col in ("Side", "Price", "Size", "Notional"):
        t.add_column(col, justify="right" if col != "Side" else "left")
    for p, sz in reversed(asks):
        t.add_row("ASK", f"${p:.4f}", f"{sz:.2f}", f"${p*sz:.2f}")
    t.add_section()
    for p, sz in bids:
        t.add_row("BID", f"${p:.4f}", f"{sz:.2f}", f"${p*sz:.2f}")
    console.print(t)


_WHO_DON_RSS = "https://www.who.int/feeds/entity/csr/don/en/rss.xml"
_DEFAULT_KEYWORDS = ["hantavirus", "pandemic", "pheic", "outbreak"]


@cli.command()
@click.option("--keywords", default=None, help="Comma-separated keywords to filter (default: hantavirus,pandemic,pheic,outbreak)")
@click.option("--days", default=30, type=int, help="Only show items from last N days (default 30)")
@click.option("--all", "show_all", is_flag=True, help="Show all items, no keyword filter")
def who(keywords: str | None, days: int, show_all: bool) -> None:
    """Check WHO Disease Outbreak News for hantavirus / pandemic alerts.

    Fetches the WHO DON RSS feed and filters for relevant items. Run this
    manually as a sanity check; for real-time alerts wire the feed to a
    webhook service (Zapier, Make, etc.) pointed at POST /alerts/who on pmproxy.
    """
    kws = (
        [k.strip().lower() for k in keywords.split(",")]
        if keywords
        else _DEFAULT_KEYWORDS
    )
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    try:
        r = requests.get(_WHO_DON_RSS, headers=UA, timeout=10)
        r.raise_for_status()
    except Exception as exc:
        console.print(f"[red]Failed to fetch WHO RSS: {exc}[/red]")
        sys.exit(1)

    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as exc:
        console.print(f"[red]Failed to parse WHO RSS: {exc}[/red]")
        sys.exit(1)

    items = root.findall(".//item")
    hits = []
    for item in items:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_str = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()

        try:
            pub = parsedate_to_datetime(pub_str).astimezone(timezone.utc) if pub_str else None
        except Exception:
            pub = None

        if pub and pub < cutoff:
            continue

        text = f"{title} {desc}".lower()
        matched = show_all or any(kw in text for kw in kws)
        if matched:
            hits.append({"title": title, "link": link, "pub": pub, "desc": desc[:120]})

    if not hits:
        console.print(f"[dim]No WHO DON items matching {kws} in the last {days} days.[/dim]")
        return

    t = Table(title=f"WHO Disease Outbreak News — last {days}d", show_lines=True)
    for col in ("Date", "Title", "Link"):
        t.add_column(col)
    for h in hits:
        date_str = h["pub"].strftime("%Y-%m-%d") if h["pub"] else "?"
        t.add_row(date_str, h["title"], h["link"])
    console.print(t)
    if any("hantavirus" in h["title"].lower() for h in hits):
        console.print("[bold red]HANTAVIRUS MENTION — review pandemic NO position immediately.[/bold red]")


if __name__ == "__main__":
    cli()
