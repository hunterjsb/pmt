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
from collections import defaultdict
from datetime import datetime, timedelta, timezone

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


# ============================================================
# Scanners
# ============================================================


@cli.group()
def scan() -> None:
    """Market opportunity scanners."""


@scan.command("cliff")
@click.option("--min", "min_pct", default=85.0, type=float, help="Min outcome price %% (default 85)")
@click.option("--max", "max_pct", default=99.0, type=float, help="Max outcome price %% (default 99)")
@click.option("--volume-jump", default=2000.0, type=float, help="Min $ jump to call it a cliff (default 2000)")
@click.option("--price-gap", default=2.0, type=float, help="Min price gap in cents (default 2.0)")
@click.option("--interval", default=30, type=int, help="Seconds between scans in continuous mode")
@click.option("--once", is_flag=True, help="Run a single scan and exit")
@click.option("--limit", default=None, type=int, help="Max scans in continuous mode")
def scan_cliff(min_pct, max_pct, volume_jump, price_gap, interval, once, limit):
    """Find ask-ladder gaps followed by a thick volume wall."""
    from scanners.scanner import create_opportunities_table, scan_continuous, scan_once

    console.print("[bold cyan]🔍 Volume Cliff Scanner[/bold cyan]")
    console.print(f"  range {min_pct}%-{max_pct}%  min jump ${volume_jump:,.0f}  min gap {price_gap}¢")
    if once:
        opps = scan_once(min_pct=min_pct, max_pct=max_pct,
                         min_volume_jump=volume_jump, min_price_gap_cents=price_gap)
        console.print(create_opportunities_table(opps))
        console.print(f"\n[dim]Found {len(opps)} opportunities[/dim]")
    else:
        console.print(f"  every {interval}s — Ctrl+C to stop\n")
        scan_continuous(min_pct=min_pct, max_pct=max_pct,
                        min_volume_jump=volume_jump, min_price_gap_cents=price_gap,
                        interval=interval, max_iterations=limit)


@scan.command("expiring")
@click.option("--min-price", default=98.0, type=float, help="Min outcome price %% (default 98)")
@click.option("--max-hours", default=2.0, type=float, help="Max hours until expiry (default 2)")
@click.option("--interval", default=60, type=int, help="Seconds between scans in continuous mode")
@click.option("--once", is_flag=True, help="Run a single scan and exit")
@click.option("--verbose", "-v", is_flag=True, help="Show scanned-market counts")
def scan_expiring(min_price, max_hours, interval, once, verbose):
    """Find high-certainty markets resolving in the next few hours."""
    from scanners.expiring import calculate_max_return, find_expiring_opportunities

    def make_table(opps, label):
        if not opps:
            console.print("[yellow]No opportunities found.[/yellow]")
            return None
        t = Table(title=f"🕐 {label}", show_lines=True)
        t.add_column("Market", style="cyan", max_width=35)
        t.add_column("Outcome", style="yellow", justify="center")
        t.add_column("Price", style="magenta", justify="right")
        t.add_column("Expires", style="red", justify="right")
        t.add_column("Max Return", style="green bold", justify="right")
        t.add_column("Rate/hr", style="blue", justify="right")
        for o in opps:
            r = calculate_max_return(o.price_pct, o.hours_until_expiry)
            q = o.question if len(o.question) <= 35 else o.question[:32] + "..."
            t.add_row(q, o.outcome, f"{o.price_pct:.2f}%", f"{o.hours_until_expiry:.1f}h",
                      f"{r['max_return_pct']:.2f}%", f"{r['hourly_rate_pct']:.2f}%")
        return t

    console.print("[bold cyan]🕐 Expiring Markets Scanner[/bold cyan]")
    console.print(f"  min price {min_price}%  max expiry {max_hours}h")

    if once:
        opps = find_expiring_opportunities(min_price_pct=min_price, max_hours=max_hours)
        if verbose:
            from polymarket import clob, gamma

            m1 = clob.sampling_markets(limit=500)
            m2 = gamma.markets(limit=500, closed=False)
            console.print(f"  [dim]scanned {len(m1)} CLOB + {len(m2)} Gamma markets[/dim]")
        tbl = make_table(opps, f"{min_price}%+ certainty, <{max_hours}h")
        if tbl:
            console.print(tbl)
            console.print(f"\n[dim]Found {len(opps)} opportunities[/dim]")
    else:
        import time as _time

        from rich.live import Live

        console.print(f"  every {interval}s — Ctrl+C to stop\n")
        with Live(refresh_per_second=1) as live:
            i = 0
            while True:
                try:
                    opps = find_expiring_opportunities(min_price_pct=min_price, max_hours=max_hours)
                    tbl = make_table(opps, f"{min_price}%+, <{max_hours}h  (scan #{i+1})")
                    if tbl:
                        live.update(tbl)
                    i += 1
                    _time.sleep(interval)
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    console.print(f"\n[red]scan error: {e}[/red]")
                    _time.sleep(interval)


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


@cli.group()
def engine() -> None:
    """Talk to a locally-running pmengine via its control plane.

    The engine binds its control plane to 127.0.0.1:7531 by default. Override
    with PMENGINE_CONTROL_URL.
    """
    pass


def _engine_get(path: str) -> dict | list:
    base = os.environ.get("PMENGINE_CONTROL_URL", "http://127.0.0.1:7531").rstrip("/")
    try:
        r = requests.get(f"{base}{path}", timeout=5)
        r.raise_for_status()
    except requests.ConnectionError:
        console.print(
            f"[red]Cannot reach pmengine at {base}.[/red] "
            "Is the engine running? Check `ps -ef | grep pmengine`."
        )
        sys.exit(1)
    except requests.HTTPError as e:
        console.print(f"[red]Engine returned {e.response.status_code}: {e.response.text}[/red]")
        sys.exit(1)
    return r.json()


@engine.command("status")
def engine_status() -> None:
    """One-line health snapshot of the running engine."""
    s = _engine_get("/status")
    t = Table(title="pmengine status", show_header=False)
    t.add_column("key", style="bold")
    t.add_column("value")
    uptime_h, rem = divmod(int(s["uptime_secs"]), 3600)
    uptime_m, uptime_s = divmod(rem, 60)
    t.add_row("uptime", f"{uptime_h}h {uptime_m}m {uptime_s}s")
    t.add_row("ticks", str(s["tick_count"]))
    t.add_row("dry_run", str(s["dry_run"]))
    t.add_row("balance", f"${float(s['balance_usdc']):,.2f} USDC")
    t.add_row("subscribed tokens", str(s["subscribed_tokens"]))
    t.add_row("strategies", str(s["strategies"]))
    t.add_row("open orders", str(s["open_orders"]))
    t.add_row("exposure", f"${float(s['total_exposure_usd']):,.2f}")
    pnl_r = float(s["realized_pnl"])
    pnl_u = float(s["unrealized_pnl"])
    t.add_row("realized P&L", f"[{_pnl_color(pnl_r)}]${pnl_r:,.2f}[/{_pnl_color(pnl_r)}]")
    t.add_row("unrealized P&L", f"[{_pnl_color(pnl_u)}]${pnl_u:,.2f}[/{_pnl_color(pnl_u)}]")
    t.add_row("status", "[red bold]HALTED[/red bold]" if s["halted"] else "[green]running[/green]")
    console.print(t)


@engine.command("strategies")
def engine_strategies() -> None:
    """List registered strategies with cadence + last-tick timestamps."""
    rows = _engine_get("/strategies")
    if not rows:
        console.print("[yellow]No strategies registered.[/yellow]")
        return
    t = Table(title="strategies")
    for col in ("id", "tick interval", "tokens", "last tick"):
        t.add_column(col)
    for r in rows:
        last = r.get("last_tick_at")
        if last:
            try:
                ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - ts).total_seconds()
                last_disp = f"{age:.0f}s ago"
            except ValueError:
                last_disp = last
        else:
            last_disp = "never"
        tokens = r["subscribed_tokens"]
        token_disp = tokens[0][:12] + "…" if len(tokens) == 1 else f"{len(tokens)} tokens"
        t.add_row(r["id"], f"{r['tick_interval_ms']}ms", token_disp, last_disp)
    console.print(t)


@engine.command("orders")
def engine_orders() -> None:
    """Open orders the engine is currently managing."""
    rows = _engine_get("/orders")
    if not rows:
        console.print("[yellow]No active orders.[/yellow]")
        return
    t = Table(title="open orders")
    for col in ("id", "token", "side", "price", "size", "filled", "status", "age"):
        t.add_column(col, justify="right" if col not in ("id", "token", "side", "status") else "left")
    now = datetime.now(timezone.utc)
    for o in rows:
        created = datetime.fromisoformat(o["created_at"].replace("Z", "+00:00"))
        age_s = (now - created).total_seconds()
        age_disp = f"{age_s:.0f}s" if age_s < 120 else f"{age_s/60:.1f}m"
        side_col = "green" if o["side"] == "buy" else "red"
        t.add_row(
            o["id"][:10] + "…",
            o["token_id"][:8] + "…",
            f"[{side_col}]{o['side'].upper()}[/{side_col}]",
            f"${float(o['price']):.4f}",
            f"{float(o['size']):.2f}",
            f"{float(o['filled']):.2f}",
            o["status"],
            age_disp,
        )
    console.print(t)


@engine.command("alerts")
def engine_alerts() -> None:
    """Pending strategy alerts awaiting human approval (Phase 5 — empty for now)."""
    rows = _engine_get("/alerts")
    if not rows:
        console.print("[dim]No pending alerts.[/dim]")
        return
    t = Table(title="pending alerts")
    for col in ("id", "reason", "created", "expires"):
        t.add_column(col)
    for a in rows:
        t.add_row(a["id"][:10], a["reason"], a["created_at"], a.get("expires_at", ""))
    console.print(t)


if __name__ == "__main__":
    cli()
