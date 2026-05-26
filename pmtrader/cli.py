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
import math
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
@click.option("--ttl", default=None, help="Auto-cancel after this duration (e.g. '30m', '2h', '1h30m'). Requires running pmengine.")
@click.option("--dry-run", is_flag=True, help="Print spec, don't submit")
def buy(token: str, price: float, size: int, tick: str | None, ttl: str | None, dry_run: bool) -> None:
    """Place a BUY order. At-or-above best ask → taker; below → maker."""
    token = _resolve_token(token)
    ttl_seconds = _parse_ttl(ttl) if ttl else None
    notional = price * size
    msg = f"BUY {size} @ ${price}  notional ${notional:.4f}"
    if ttl_seconds:
        msg += f"  ttl={ttl} ({ttl_seconds}s)"
    console.print(msg)
    if dry_run:
        console.print("[dim]dry-run[/dim]")
        return
    resp = _place_or_direct("buy", token=token, price=price, size=size, tick=tick)
    if ttl_seconds:
        _schedule_ttl_cancel_if_live(resp, ttl_seconds=ttl_seconds)
    click.echo(json.dumps(resp, indent=2, default=str))


@cli.command()
@click.option("--token", required=True)
@click.option("--price", required=True, type=float)
@click.option("--size", required=True, type=int)
@click.option("--tick", default=None)
@click.option("--ttl", default=None, help="Auto-cancel after this duration (e.g. '30m', '2h', '1h30m'). Requires running pmengine.")
@click.option("--dry-run", is_flag=True)
def sell(token: str, price: float, size: int, tick: str | None, ttl: str | None, dry_run: bool) -> None:
    """Place a SELL order."""
    token = _resolve_token(token)
    ttl_seconds = _parse_ttl(ttl) if ttl else None
    notional = price * size
    msg = f"SELL {size} @ ${price}  notional ${notional:.4f}"
    if ttl_seconds:
        msg += f"  ttl={ttl} ({ttl_seconds}s)"
    console.print(msg)
    if dry_run:
        console.print("[dim]dry-run[/dim]")
        return
    resp = _place_or_direct("sell", token=token, price=price, size=size, tick=tick)
    if ttl_seconds:
        _schedule_ttl_cancel_if_live(resp, ttl_seconds=ttl_seconds)
    click.echo(json.dumps(resp, indent=2, default=str))


def _slug_from_url(url_or_slug: str) -> str:
    """Accept a polymarket.com event URL or bare slug and return the event slug."""
    s = url_or_slug.strip()
    if "polymarket.com/event/" in s:
        s = s.split("/event/", 1)[1]
    # Trim any trailing sub-market slug and query/fragment.
    return s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]


def _parse_amount(raw: str) -> float:
    return float(raw.lstrip("$").replace(",", "").strip())


def _pick_marketable_price(asks: list[dict], target_notional: float, tick: float) -> tuple[float | None, float, float]:
    """Find the smallest tick-aligned limit price whose strictly-lower asks
    cumulatively cover `target_notional`.

    Same-priced orders on Polymarket don't cross, so a limit of P consumes
    only asks priced < P. To take a level priced X, the limit must be ≥ X+tick.

    Returns (limit_price, expected_consumed_size, expected_consumed_cost).
    None price if the book is empty or too thin.
    """
    sorted_asks = sorted(asks, key=lambda a: float(a["price"]))
    cum_cost = 0.0
    cum_size = 0.0
    for a in sorted_asks:
        p = float(a["price"])
        s = float(a["size"])
        level_cost = p * s
        if cum_cost + level_cost >= target_notional:
            # Partial fill of this level finishes the order; limit = p + tick
            # so this level (and everything below) is taken.
            remaining_usd = target_notional - cum_cost
            partial_size = remaining_usd / p
            cum_size += partial_size
            cum_cost += remaining_usd
            limit = round(round((p + tick) / tick) * tick, 6)
            return (limit, cum_size, cum_cost)
        cum_cost += level_cost
        cum_size += s
    # Exhausted the book without filling — return whatever we found.
    if not sorted_asks:
        return (None, 0.0, 0.0)
    top = float(sorted_asks[-1]["price"])
    limit = round(round((top + tick) / tick) * tick, 6)
    return (limit, cum_size, cum_cost)


@cli.command()
@click.argument("url_or_slug")
@click.argument("side")
@click.argument("amount")
@click.option("--match", default=None, help="Filter sub-markets by keyword (e.g. '78,000', 'NVIDIA').")
@click.option("--dry-run", is_flag=True, help="Resolve + plan, don't submit.")
def bet(url_or_slug: str, side: str, amount: str, match: str | None, dry_run: bool) -> None:
    """Place $AMOUNT on SIDE of a polymarket event by URL or slug.

    \b
    Examples:
      pmt bet https://polymarket.com/event/btc-updown-4h-1779825600 down $910
      pmt bet bitcoin-price-on-may-26-2026 no $80 --match '78,000'
      pmt bet nobel-peace-prize-winner-2026 no $100 --match Trump

    Walks the order book and prices the order to guarantee a sweep that
    covers the notional. Routes through engine via existing `buy` flow.
    """
    slug = _slug_from_url(url_or_slug)
    target = _parse_amount(amount)
    if target <= 0:
        console.print(f"[red]Bad amount: {amount!r}[/red]")
        sys.exit(1)

    api = _api()
    ev = api.get_market(slug)
    markets = [m for m in (ev.get("markets") or [])
               if not m.get("closed") and not m.get("archived")]
    if not markets:
        console.print(f"[red]No open sub-markets at slug '{slug}'[/red]")
        sys.exit(1)

    if match:
        m_low = match.lower()
        matched = [m for m in markets
                   if m_low in (m.get("question") or "").lower()
                   or m_low in (m.get("groupItemTitle") or "").lower()]
        if not matched:
            console.print(f"[red]No sub-market matches '{match}'[/red]. Available:")
            for m in markets:
                console.print(f"  - {m.get('groupItemTitle') or m.get('question')}")
            sys.exit(1)
        markets = matched

    if len(markets) > 1:
        console.print(f"[yellow]{len(markets)} sub-markets — add --match KEYWORD to pick one:[/yellow]")
        for m in markets:
            raw_o = m.get("outcomes") or "[]"
            raw_p = m.get("outcomePrices") or "[]"
            outcomes = json.loads(raw_o) if isinstance(raw_o, str) else raw_o
            prices = json.loads(raw_p) if isinstance(raw_p, str) else raw_p
            label = m.get("groupItemTitle") or m.get("question") or "(unnamed)"
            px = "  ".join(f"{o}=${p}" for o, p in zip(outcomes, prices)) or "[no quotes]"
            console.print(f"  • {label}   [dim]{px}[/dim]")
        sys.exit(1)

    m = markets[0]
    outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]

    side_low = side.lower()
    idx = next((i for i, o in enumerate(outcomes) if o.lower() == side_low), None)
    if idx is None:
        idx = next((i for i, o in enumerate(outcomes) if o.lower().startswith(side_low)), None)
    if idx is None:
        console.print(f"[red]Side '{side}' not in outcomes {outcomes}[/red]")
        sys.exit(1)
    token = tokens[idx]

    book = api.get_book(token)
    tick = float(api.get_tick_size(token))
    limit, exp_size, exp_cost = _pick_marketable_price(book.get("asks") or [], target, tick)
    if limit is None:
        console.print(f"[red]Empty {outcomes[idx]} ask book — nothing to take.[/red]")
        sys.exit(1)
    if exp_cost < target * 0.9:
        console.print(
            f"[yellow]warn:[/yellow] book only depths to ${exp_cost:.2f} of target ${target:.2f}. "
            f"Order will partially fill and rest."
        )

    size = max(int(math.ceil(target / limit)), 1)

    label = m.get("groupItemTitle") or m.get("question")
    console.print(f"Event:   [bold]{ev.get('title')}[/bold]")
    console.print(f"Market:  {label}  →  BUY [bold]{outcomes[idx]}[/bold]")
    console.print(
        f"Sweep:   ~{exp_size:.1f} sh @ avg ${exp_cost/exp_size:.4f} "
        f"= ${exp_cost:.2f}  (tick {tick})"
    )
    console.print(f"Order:   BUY {size} @ ${limit:.4f}  notional ${size*limit:.2f}")

    if dry_run:
        console.print("[dim]dry-run[/dim]")
        return

    ctx = click.get_current_context()
    ctx.invoke(buy, token=token, price=limit, size=size, tick=None, ttl=None, dry_run=False)


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
    """Cancel an open order by ID.

    Tries the engine's `/orders/:id/cancel` endpoint first so the engine
    stays authoritative for state. Falls back to a direct CLOB cancel if
    the engine is unreachable; in that case it also notifies the engine
    after the fact (no-op if still down).
    """
    via_engine = _engine_notify(f"/orders/{order_id}/cancel")
    if via_engine is not None:
        console.print(f"[green]cancelled[/green] via engine: {order_id}")
        return
    # Engine unreachable — direct CLOB cancel, then a best-effort notify.
    resp = _api().cancel(order_id)
    _engine_notify(f"/orders/external/{order_id}/cancelled")
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


@cli.command()
@click.option("--top", default=10, type=int, help="Show top N realized winners and losers (all-time)")
def pnl(top: int) -> None:
    """Realized P&L over 1d/7d/30d/all-time + current unrealized.

    Replays the full activity stream into a per-asset cost-basis ledger and
    emits realized events on SELL/REDEEM disposals. Positions held in the
    ledger but no longer in the current portfolio are written off as expired
    (timestamp unknown → all-time bucket only)."""
    import time
    from polymarket.pnl import (
        replay_activity, reconcile_expired, bucket_by_window,
    )

    api = _api()
    with console.status("[dim]paginating /activity…[/dim]"):
        acts = api.get_full_activity()
    res = replay_activity(acts)
    positions = api.get_positions()
    reconcile_expired(res, positions)

    now = int(time.time())
    windows: dict[str, int | None] = {
        "1d": 86400,
        "7d": 7 * 86400,
        "30d": 30 * 86400,
        "all": None,
    }

    sells     = [e for e in res.realized if e.kind == "SELL"]
    redeems   = [e for e in res.realized if e.kind == "REDEEM"]
    expired   = [e for e in res.realized if e.kind == "EXPIRED"]
    rewards   = [e for e in res.income   if e.kind == "REWARD"]
    yields    = [e for e in res.income   if e.kind == "YIELD"]

    rows = [
        ("Trade SELL",  bucket_by_window(sells,   now, windows)),
        ("Redemptions", bucket_by_window(redeems, now, windows)),
        ("Expired (worthless)", bucket_by_window(expired, now, windows)),
        ("Rewards",     bucket_by_window(rewards, now, windows)),
        ("Yield",       bucket_by_window(yields,  now, windows)),
    ]

    unrealized = sum(
        p.get("cashPnl", p.get("currentValue", 0) - p["size"] * p["avgPrice"])
        for p in positions
    )

    t = Table(title=f"Realized P&L  ({len(acts)} activity events)")
    t.add_column("Source", justify="left")
    for w in windows:
        t.add_column(w, justify="right")
    for label, bucket in rows:
        row = [label]
        for w in windows:
            v = bucket[w]
            c = _pnl_color(v)
            row.append(f"[{c}]${v:+.2f}[/]")
        t.add_row(*row)
    t.add_section()
    subtotal_per_window = {w: sum(b[w] for _, b in rows) for w in windows}
    sub_row = ["[bold]Realized subtotal[/bold]"]
    for w in windows:
        v = subtotal_per_window[w]
        sub_row.append(f"[bold {_pnl_color(v)}]${v:+.2f}[/]")
    t.add_row(*sub_row)
    console.print(t)

    console.print()
    c = _pnl_color(unrealized)
    console.print(f"Current [bold]unrealized[/bold] (mark - cost): [{c}]${unrealized:+.2f}[/]")
    grand = subtotal_per_window["all"] + unrealized
    c = _pnl_color(grand)
    console.print(f"[bold]Grand total[/bold] (all-time realized + current unrealized): [{c}]${grand:+.2f}[/]")

    console.print()
    console.print("[dim]Note: window P&L is realized cash flow only. "
                  "Polymarket UI also reflects mark-to-market changes within "
                  "the window, which requires historical price snapshots we don't store.[/dim]")

    # Top movers (all-time realized only — windowed views would need history)
    if top > 0:
        wins   = sorted(res.realized, key=lambda e: e.pnl, reverse=True)[:top]
        losses = sorted(res.realized, key=lambda e: e.pnl)[:top]
        for title, events in (("Top realized winners", wins), ("Top realized losers", losses)):
            tt = Table(title=title)
            tt.add_column("Market"); tt.add_column("Kind"); tt.add_column("PnL", justify="right")
            for e in events:
                tt.add_row(
                    (e.title or e.condition_id)[:50],
                    e.kind,
                    f"[{_pnl_color(e.pnl)}]${e.pnl:+.2f}[/]",
                )
            console.print()
            console.print(tt)


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
            from polymarket import Gamma, sampling_markets

            m1 = sampling_markets(limit=500)
            m2 = Gamma().markets(limit=500, closed=False)
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
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Include externally-registered orders (from pmt buy/sell, web, etc.)",
)
def engine_orders(show_all: bool) -> None:
    """Open orders the engine is tracking.

    Default view = orders the engine placed itself.
    With --all = unified view incl. orders the CLI registered after placing.
    """
    rows = _engine_get("/orders/all" if show_all else "/orders")
    if not rows:
        console.print("[yellow]No active orders.[/yellow]")
        return
    t = Table(title="open orders" + (" (all sources)" if show_all else ""))
    cols = ("id", "token", "side", "price", "size", "filled", "status", "age")
    if show_all:
        cols = ("id", "token", "side", "price", "size", "filled", "status", "source", "age")
    for col in cols:
        t.add_column(
            col,
            justify="right"
            if col not in ("id", "token", "side", "status", "source")
            else "left",
        )
    now = datetime.now(timezone.utc)
    for o in rows:
        created = datetime.fromisoformat(o["created_at"].replace("Z", "+00:00"))
        age_s = (now - created).total_seconds()
        age_disp = f"{age_s:.0f}s" if age_s < 120 else f"{age_s/60:.1f}m"
        side_col = "green" if o["side"] == "buy" else "red"
        row = [
            o["id"][:10] + "…",
            o["token_id"][:8] + "…",
            f"[{side_col}]{o['side'].upper()}[/{side_col}]",
            f"${float(o['price']):.4f}",
            f"{float(o['size']):.2f}",
            f"{float(o['filled']):.2f}",
            o["status"],
        ]
        if show_all:
            row.append(o.get("source", "engine"))
        row.append(age_disp)
        t.add_row(*row)
    console.print(t)


@engine.command("subscriptions")
def engine_subscriptions() -> None:
    """List token IDs the engine is currently watching."""
    tokens = _engine_get("/subscriptions")
    if not tokens:
        console.print("[yellow]No tokens subscribed.[/yellow]")
        return
    t = Table(title="subscribed tokens")
    t.add_column("#", justify="right")
    t.add_column("token id")
    for i, tid in enumerate(tokens, 1):
        t.add_row(str(i), tid)
    console.print(t)


@engine.command("trades")
@click.argument("token")
@click.option("--since", type=int, default=None, help="Only trades with timestamp ≥ this unix-seconds value")
@click.option("--window", type=int, default=None, help="Trades in the last N seconds (alternative to --since)")
@click.option("--limit", type=int, default=30, help="Max rows to print (default 30)")
def engine_trades(token: str, since: int | None, window: int | None, limit: int) -> None:
    """Recent public trades for a token from the engine's rolling buffer."""
    token = _resolve_token(token)
    params = ""
    if window is not None:
        from time import time as _now
        since = int(_now()) - window
    if since is not None:
        params = f"?since={since}"
    rows = _engine_get(f"/trades/{token}{params}")
    if not rows:
        console.print("[dim]No trades in buffer for that token / window.[/dim]")
        return
    # Newest first
    rows.sort(key=lambda r: r["timestamp"], reverse=True)
    rows = rows[:limit]
    t = Table(title=f"recent trades for {token[:12]}…")
    for col in ("when", "side", "price", "size", "notional"):
        t.add_column(col, justify="right" if col != "side" else "left")
    now_ts = datetime.now(timezone.utc).timestamp()
    for r in rows:
        age = now_ts - r["timestamp"]
        when = f"{age:.0f}s ago" if age < 120 else f"{age/60:.1f}m ago"
        side_col = "green" if r["side"] == "BUY" else "red"
        price = float(r["price"])
        size = float(r["size"])
        t.add_row(
            when,
            f"[{side_col}]{r['side']}[/{side_col}]",
            f"${price:.4f}",
            f"{size:.2f}",
            f"${price*size:,.2f}",
        )
    console.print(t)


@engine.command("alerts")
def engine_alerts() -> None:
    """Pending strategy alerts awaiting human approval."""
    rows = _engine_get("/alerts")
    if not rows:
        console.print("[dim]No pending alerts.[/dim]")
        return
    t = Table(title="pending alerts")
    for col in ("id", "side", "token", "price", "size", "reason", "expires in"):
        t.add_column(col, justify="right" if col in ("price", "size") else "left")
    now = datetime.now(timezone.utc)
    for a in rows:
        sug = a["suggested"]
        side_col = "green" if sug["side"] == "buy" else "red"
        try:
            expires = datetime.fromisoformat(a["expires_at"].replace("Z", "+00:00"))
            rem = (expires - now).total_seconds()
            exp_disp = f"{rem:.0f}s" if rem < 120 else f"{rem/60:.1f}m"
        except (KeyError, ValueError):
            exp_disp = "?"
        t.add_row(
            a["id"],
            f"[{side_col}]{sug['side'].upper()}[/{side_col}]",
            sug["token_id"][:10] + "…",
            f"${float(sug['price']):.4f}",
            f"{float(sug['size']):.2f}",
            a["reason"][:60],
            exp_disp,
        )
    console.print(t)
    console.print(
        "\n[dim]approve: `pmt engine approve <id>`   reject: `pmt engine reject <id>`[/dim]"
    )


def _engine_post(path: str, body: dict | None = None) -> dict:
    base = os.environ.get("PMENGINE_CONTROL_URL", "http://127.0.0.1:7531").rstrip("/")
    try:
        r = requests.post(f"{base}{path}", json=body, timeout=10)
    except requests.ConnectionError:
        console.print(f"[red]Cannot reach pmengine at {base}.[/red]")
        sys.exit(1)
    if r.status_code >= 400:
        try:
            msg = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        except Exception:
            msg = r.text
        console.print(f"[red]HTTP {r.status_code}: {msg}[/red]")
        sys.exit(1)
    return r.json()


def _engine_notify(path: str, body: dict | None = None) -> dict | None:
    """Fire-and-forget POST to the engine — does NOT exit on failure.

    Returns the JSON response on success, or None if the engine is
    unreachable / responds with an error. Use this for cooperative
    notifications from CLI commands that must still succeed standalone.
    """
    base = os.environ.get("PMENGINE_CONTROL_URL", "http://127.0.0.1:7531").rstrip("/")
    try:
        r = requests.post(f"{base}{path}", json=body, timeout=3)
        if r.status_code >= 400:
            return None
        return r.json()
    except (requests.ConnectionError, requests.Timeout):
        return None


def _place_via_engine(side: str, token: str, price: float, size: int) -> dict | None:
    """Ask the running engine to place the order on our behalf.

    Routing CLI writes through the engine puts every account-touching call
    on a single queue, so the engine's pollers/strategies and the CLI no
    longer race for the account-wide ~5 req/sec budget. The engine also
    handles tick rounding (cached) so this path skips the CLI's separate
    tick_size REST lookup.

    Returns:
        - dict shaped like place_buy/place_sell on success
        - None if the engine is unreachable (caller falls back to direct)

    On engine-side rejection (4xx, e.g. risk limit, validation error) this
    prints the error and exits — a deliberate rejection is NOT a reason to
    silently retry against direct CLOB and bypass whatever the engine was
    enforcing.
    """
    base = os.environ.get("PMENGINE_CONTROL_URL", "http://127.0.0.1:7531").rstrip("/")
    try:
        r = requests.post(
            f"{base}/trade/place",
            json={"token_id": token, "side": side, "price": str(price), "size": str(size)},
            timeout=20,
        )
    except (requests.ConnectionError, requests.Timeout):
        return None
    if r.status_code >= 400:
        try:
            msg = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        except Exception:
            msg = r.text
        console.print(f"[red]engine rejected order: {msg}[/red]")
        sys.exit(1)
    body = r.json()
    # Shape the response so downstream code (TTL scheduling, json echo) can
    # pull order_id the same way it does from py-clob-client responses.
    return {"success": True, "orderID": body.get("order_id"), "via_engine": True}


def _place_or_direct(
    side: str, *, token: str, price: float, size: int, tick: str | None
) -> dict:
    """Try the engine first; on engine unreachable fall back to direct CLOB.

    The engine path serializes against the account-wide budget and skips the
    CLI's tick_size lookup; the direct path keeps `pmt buy/sell` working when
    the engine isn't running. `--tick` short-circuits to direct since the
    engine ignores caller-supplied ticks (it always uses its cached lookup).
    """
    if tick is None:
        resp = _place_via_engine(side, token, price, size)
        if resp is not None:
            return resp
        console.print("[dim](engine unreachable; placing direct)[/dim]")
    place_fn = _api().place_buy if side == "buy" else _api().place_sell
    resp = place_fn(token=token, price=price, size=size, tick_size=tick)
    _register_with_engine_if_live(resp, token=token, side=side, price=price, size=size)
    return resp


def _register_with_engine_if_live(
    resp: dict, *, token: str, side: str, price: float, size: int
) -> None:
    """After a successful place_buy/place_sell, tell the engine about the
    order so its `/orders/all` view stays unified. Silent on engine offline.
    """
    if not isinstance(resp, dict) or not resp.get("success"):
        return
    order_id = resp.get("orderID") or resp.get("order_id")
    if not order_id:
        return
    _engine_notify(
        "/orders/external",
        {
            "id": order_id,
            "token_id": token,
            "side": side,
            "price": str(price),
            "size": str(size),
            "source": "pmt-cli",
        },
    )


_TTL_TOKEN = re.compile(r"(\d+)([dhms])", re.IGNORECASE)
_TTL_UNIT_SECS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _parse_ttl(value: str) -> int:
    """Parse '30m', '2h', '1h30m', '2d' → seconds.

    Polymarket orders are GTC, so TTL is enforced client-side by asking the
    engine to schedule a cancel — see `_schedule_ttl_cancel_if_live`.
    """
    parts = _TTL_TOKEN.findall(value)
    if not parts or "".join(f"{n}{u}" for n, u in parts).lower() != value.lower():
        raise click.BadParameter(
            f"--ttl: invalid duration '{value}' (use forms like '30m', '2h', '1h30m', '2d')"
        )
    return sum(int(n) * _TTL_UNIT_SECS[u.lower()] for n, u in parts)


def _schedule_ttl_cancel_if_live(resp: dict, *, ttl_seconds: int) -> None:
    """After a successful place, ask the engine to cancel after `ttl_seconds`.

    The order itself is already on the book whether or not the engine is up;
    if the engine is unreachable, warn loudly because the TTL will not be
    honored and the order will rest until manually cancelled.
    """
    if not isinstance(resp, dict) or not resp.get("success"):
        return
    order_id = resp.get("orderID") or resp.get("order_id")
    if not order_id:
        return
    result = _engine_notify(
        f"/orders/{order_id}/schedule-cancel",
        {"after_seconds": ttl_seconds},
    )
    if result is None:
        console.print(
            f"[red]WARNING: engine unreachable — TTL of {ttl_seconds}s NOT registered. "
            f"Order is placed but will rest until manually cancelled.[/red]"
        )
    else:
        console.print(f"[dim]TTL: cancel scheduled at {result.get('at')}[/dim]")


@engine.command("approve")
@click.argument("alert_id")
def engine_approve(alert_id: str) -> None:
    """Approve a pending alert; the engine executes the suggested order."""
    res = _engine_post(f"/alerts/{alert_id}/approve")
    console.print(f"[green]approved[/green] → order_id: {res.get('order_id')}")


@engine.command("reject")
@click.argument("alert_id")
def engine_reject(alert_id: str) -> None:
    """Reject a pending alert; the engine drops it without executing."""
    _engine_post(f"/alerts/{alert_id}/reject")
    console.print(f"[yellow]rejected[/yellow] alert {alert_id}")


if __name__ == "__main__":
    cli()
