"""pmt — Polymarket trading CLI.

Routes through PolymarketAPI (and the pmproxy Lambda, when configured)
for writes; falls through the local pmengine control plane when it's
running so account-wide rate limits are shared with strategies.

Order primitives (symmetric):

    pmt buy  REF [OUTCOME] [--amount $X | --size N] [--price P] ...
    pmt sell REF [OUTCOME] [--amount $X | --size N] [--price P] ...

REF is either a polymarket.com URL/slug OR a numeric token id. OUTCOME
(yes/no/up/down/...) is required for URL/slug refs and ignored for token
refs (the token already encodes the outcome).

Pricing:
    --amount $X          marketable sweep: walks book, sets limit so the
                         intended notional is fully consumed
    --size N             marketable sweep sized in shares
    --price P + --size N explicit limit order at P
    --price P + --amount explicit limit at P, size = ceil(amount/P)

Other commands:
    pmt sweep  REF [OUTCOME] --to P [--max-cost $X] [--flip P]
    pmt flip   TOKEN --buy-price BP --sell-price SP --size N
    pmt cancel ORDER_ID
    pmt orders | positions | pnl | rewards | balance
    pmt market | search | book
    pmt engine status | strategies | orders | trades | alerts | approve | reject
    pmt scan REF | cliff | expiring
    pmt fit REF
    pmt sports board LEAGUE | game LEAGUE REF | watch LEAGUE REF
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import click
from rich.console import Console
from rich.table import Table

# Imported with aliases so the `pmt engine` CLI group below doesn't shadow it.
from engine import (
    get as _engine_get,
    post as _engine_post,
    notify as _engine_notify,
    place_or_direct as _place_or_direct,
    parse_ttl as _parse_ttl,
    schedule_cancel as _schedule_ttl_cancel_if_live,
)

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
    from polymarket import PolymarketAPI

    return PolymarketAPI()


# ============================================================
# Order placement
# ============================================================


@click.group()
def cli() -> None:
    """pmtrader unified CLI."""
    # Repo-root .env discovery so pmt works from any cwd.
    from polymarket.env import load_project_env

    load_project_env()


# ---------- ref / book / amount helpers ----------

def _slug_from_url(url_or_slug: str) -> str:
    """Strip a polymarket.com URL down to the event slug; pass slugs through."""
    s = url_or_slug.strip()
    if "polymarket.com/event/" in s:
        s = s.split("/event/", 1)[1]
    return s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]


def _parse_amount(raw: str) -> float:
    """Parse '$200', '200', '$1,000' → float."""
    return float(raw.lstrip("$").replace(",", "").strip())


def _resolve_ref(
    api, ref: str, outcome: str | None, match: str | None,
) -> tuple[str, dict | None, dict | None, str | None]:
    """Resolve REF + OUTCOME → (token_id, event, market, outcome_name).

    - Numeric REF → token-id passthrough; event/market are None.
    - URL/slug REF → gamma event lookup, sub-market filter, outcome match.
    """
    if ref.isdigit():
        return ref, None, None, None

    slug = _slug_from_url(ref)
    ev = api.get_market(slug)
    markets = [m for m in (ev.get("markets") or [])
               if not m.get("closed") and not m.get("archived")]
    if not markets:
        raise click.UsageError(f"No open sub-markets at slug '{slug}'")

    if match:
        m_low = match.lower()
        matched = [m for m in markets
                   if m_low in (m.get("question") or "").lower()
                   or m_low in (m.get("groupItemTitle") or "").lower()]
        if not matched:
            avail = "\n  ".join(_market_label(m) or "(unnamed)" for m in markets)
            raise click.UsageError(f"No sub-market matches '{match}'. Available:\n  {avail}")
        markets = matched

    if len(markets) > 1:
        lines = [f"{len(markets)} sub-markets — add --match KEYWORD to pick one:"]
        for m in markets:
            outs = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else (m["outcomes"] or [])
            raw_p = m.get("outcomePrices") or "[]"
            prices = json.loads(raw_p) if isinstance(raw_p, str) else raw_p
            label = _market_label(m) or "(unnamed)"
            px = "  ".join(f"{o}=${p}" for o, p in zip(outs, prices)) or "(no quotes)"
            lines.append(f"  • {label}   {px}")
        raise click.UsageError("\n".join(lines))

    m = markets[0]
    outs = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]

    if not outcome:
        raise click.UsageError(
            f"OUTCOME required for URL/slug refs. Available: {', '.join(outs)}"
        )
    o_low = outcome.lower()
    idx = next((i for i, o in enumerate(outs) if o.lower() == o_low), None)
    if idx is None:
        idx = next((i for i, o in enumerate(outs) if o.lower().startswith(o_low)), None)
    if idx is None:
        raise click.UsageError(f"OUTCOME '{outcome}' not in {outs}")
    return tokens[idx], ev, m, outs[idx]


def _market_label(market: dict | None) -> str | None:
    """Display label for a gamma sub-market record."""
    return (market.get("groupItemTitle") or market.get("question")) if market else None


def _print_market_header(ev, market, side, outcome_name) -> None:
    """Event:/Market: header shared by the order commands."""
    if ev:
        console.print(f"Event:   [bold]{ev.get('title')}[/bold]")
    label = _market_label(market)
    if label:
        console.print(f"Market:  {label}  →  {side.upper()} [bold]{outcome_name}[/bold]")


def _sweep_book(
    levels: list[dict], side: str, target_notional: float, tick: float,
) -> tuple[float | None, float, float]:
    """Walk the book in the direction the order takes liquidity.

    BUY  → ascending asks; limit = highest_taken + tick.
    SELL → descending bids; limit = lowest_taken  - tick.

    Same-priced orders on Polymarket don't cross, so a limit at P only
    consumes the opposite side strictly inside P. ±tick guarantees crossing.

    Polymarket rejects limit prices outside [tick, 1 - tick], so the result
    is clamped — if the book runs that thin, the order partial-fills at the
    clamped edge and rests there.

    Returns (limit_price, expected_size, expected_cost). limit is None on
    empty book.
    """
    if side == "buy":
        ordered = sorted(levels, key=lambda x: float(x["price"]))
    else:
        ordered = sorted(levels, key=lambda x: float(x["price"]), reverse=True)
    if not ordered:
        return (None, 0.0, 0.0)

    def clamp(p: float) -> float:
        return max(tick, min(1.0 - tick, round(round(p / tick) * tick, 6)))

    cum_size = cum_cost = 0.0
    for lvl in ordered:
        p = float(lvl["price"])
        s = float(lvl["size"])
        level_cost = p * s
        if cum_cost + level_cost >= target_notional:
            remaining_usd = target_notional - cum_cost
            cum_size += remaining_usd / p
            cum_cost += remaining_usd
            edge = p + tick if side == "buy" else p - tick
            return (clamp(edge), cum_size, cum_cost)
        cum_cost += level_cost
        cum_size += s

    worst = float(ordered[-1]["price"])
    edge = worst + tick if side == "buy" else worst - tick
    return (clamp(edge), cum_size, cum_cost)


def _place(side, ref, outcome, *, match, amount, size, price, tick, ttl, dry_run):
    """The order-placement flow shared by `buy` and `sell`."""
    have_explicit = price is not None and size is not None
    if amount is None and size is None and not have_explicit:
        raise click.UsageError(
            "Sizing required: --amount $X, --size N, or --price P --size N"
        )

    ttl_seconds = _parse_ttl(ttl) if ttl else None
    api = _api()
    token, ev, market, outcome_name = _resolve_ref(api, ref, outcome, match)

    swept: tuple[float | None, float, float] | None = None
    actual_tick = None

    if have_explicit and amount is None:
        # Explicit limit; no book needed.
        limit_price, order_size = price, size
    else:
        book = api.get_book(token)
        actual_tick = float(api.get_tick_size(token))
        levels = (book.get("asks") if side == "buy" else book.get("bids")) or []
        if amount is not None:
            target = _parse_amount(amount)
        else:
            # --size alone: estimate target from top-of-book.
            if not levels:
                raise click.UsageError("Empty book — can't sweep.")
            target = float(levels[0]["price"]) * size
        if target <= 0:
            raise click.UsageError(f"Bad amount: {amount!r}")

        swept = _sweep_book(levels, side, target, actual_tick)
        sweep_limit, exp_size, exp_cost = swept
        if sweep_limit is None:
            raise click.UsageError(f"Empty {side} book.")
        if amount is not None and exp_cost < target * 0.9:
            console.print(
                f"[yellow]warn:[/yellow] book only depths to ${exp_cost:.2f} "
                f"of target ${target:.2f} — order will partially fill and rest."
            )
        limit_price = price if price is not None else sweep_limit
        order_size = size if size is not None else max(int(math.ceil(target / limit_price)), 1)

    # Display block
    _print_market_header(ev, market, side, outcome_name)
    if swept and swept[1] > 0:
        _, exp_size, exp_cost = swept
        console.print(
            f"Sweep:   ~{exp_size:.1f} sh @ avg ${exp_cost/exp_size:.4f} "
            f"= ${exp_cost:.2f}  (tick {actual_tick})"
        )
    order_msg = f"Order:   {side.upper()} {order_size} @ ${limit_price:.4f}  notional ${order_size*limit_price:.2f}"
    if ttl_seconds:
        order_msg += f"  ttl={ttl} ({ttl_seconds}s)"
    console.print(order_msg)

    if dry_run:
        console.print("[dim]dry-run[/dim]")
        return

    resp = _place_or_direct(side, token=token, price=limit_price, size=order_size, tick=tick)
    if ttl_seconds:
        _schedule_ttl_cancel_if_live(resp, ttl_seconds=ttl_seconds)
    click.echo(json.dumps(resp, indent=2, default=str))


def _order_opts(f):
    """Shared option surface for buy + sell. Decorators apply bottom-up."""
    f = click.option("--dry-run", is_flag=True, help="Resolve + plan, don't submit")(f)
    f = click.option("--ttl", default=None, help="Auto-cancel after duration (e.g. '30m', '1h30m'). Needs running pmengine.")(f)
    f = click.option("--tick", default=None, help="Tick override; setting this skips engine routing.")(f)
    f = click.option("--price", default=None, type=float, help="Explicit limit price (omit for marketable sweep).")(f)
    f = click.option("--size", default=None, type=float, help="Order size in shares.")(f)
    f = click.option("--amount", default=None, help="Order size in USD notional (e.g. '$200').")(f)
    f = click.option("--match", default=None, help="Disambiguate sub-markets by keyword.")(f)
    return f


@cli.command()
@click.argument("ref")
@click.argument("outcome", required=False)
@_order_opts
def buy(ref, outcome, match, amount, size, price, tick, ttl, dry_run):
    """Place a BUY order.

    \b
    REF      polymarket URL/slug OR numeric token id
    OUTCOME  yes/no/up/down/... (required for URL/slug, ignored for tokens)

    \b
    Examples:
      pmt buy https://polymarket.com/event/btc-updown-4h-1779825600 down --amount $910
      pmt buy nobel-peace-prize-winner-2026 no --amount $100 --match Trump
      pmt buy 14658893069672317885... --price 0.92 --size 217
    """
    _place("buy", ref, outcome, match=match, amount=amount, size=size,
           price=price, tick=tick, ttl=ttl, dry_run=dry_run)


@cli.command()
@click.argument("ref")
@click.argument("outcome", required=False)
@_order_opts
def sell(ref, outcome, match, amount, size, price, tick, ttl, dry_run):
    """Place a SELL order. Same surface as buy; sweeps the BID side."""
    _place("sell", ref, outcome, match=match, amount=amount, size=size,
           price=price, tick=tick, ttl=ttl, dry_run=dry_run)


@cli.command()
@click.argument("ref")
@click.argument("outcome", required=False)
@click.option("--match", default=None, help="Disambiguate sub-markets by keyword.")
@click.option("--to", "to_price", required=True, type=float, help="Take asks priced <= this; the order is a GTC limit resting here.")
@click.option("--max-cost", default=None, help="Budget cap in USD (e.g. '$150'); trims the plan from the worst level down.")
@click.option("--flip", "flip_price", default=None, type=float, help="After the sweep fills, resell the filled size at this price.")
@click.option("--dry-run", is_flag=True, help="Print the plan, never place.")
@click.option("--json", "as_json", is_flag=True, help="Emit the sweep result (+ flip) as JSON.")
def sweep(ref, outcome, match, to_price, max_cost, flip_price, dry_run, as_json):
    """Buy-side sweep: take displayed asks up to --to with one GTC limit there.

    \b
    REF      polymarket URL/slug OR numeric token id
    OUTCOME  yes/no/up/down/... (defaults to yes; ignored for token refs)

    Never places when the plan is under the 5-share exchange minimum.
    """
    api = _api()
    token, ev, market, outcome_name = _resolve_ref(api, ref, outcome or "yes", match)
    budget = _parse_amount(max_cost) if max_cost else None

    res = api.sweep("buy", token=token, to_price=to_price, max_cost=budget, dry_run=dry_run)
    plan, placed = res["plan"], res["placed"]
    filled = float((placed or {}).get("takingAmount") or 0)
    cost = float((placed or {}).get("makingAmount") or 0)

    # Flip once, pre-branch, so JSON and rich output share one result.
    flip_res = None
    if flip_price is not None and placed is not None and filled >= 1:
        flip_res = api._sell_with_settlement_retry(
            token=token, price=flip_price, size=int(filled),
            tick_size=api.get_tick_size(token),
        )
        res["flip"] = flip_res

    if as_json:
        click.echo(json.dumps(res, indent=2, default=str))
        return

    _print_market_header(ev, market, "buy", outcome_name)

    if not plan["levels"]:
        console.print(f"[dim]No asks at or under ${to_price} — nothing to sweep.[/dim]")
        return
    t = Table(title=f"sweep plan — asks ≤ ${to_price}")
    for col in ("Price", "Size", "Notional"):
        t.add_column(col, justify="right")
    for p, sz in plan["levels"]:
        t.add_row(f"${p:.4f}", f"{sz:.2f}", f"${p*sz:.2f}")
    t.add_section()
    avg = plan["est_cost"] / plan["size"] if plan["size"] else 0.0
    t.add_row(
        f"[bold]avg ${avg:.4f}[/bold]",
        f"[bold]{plan['size']:.2f}[/bold]",
        f"[bold]${plan['est_cost']:.2f}[/bold]",
    )
    console.print(t)

    # The GTC rests at to_price: worst case every share fills there, not at the
    # snapshot prices — surface the true commitment (and any budget cap).
    place_size = plan.get("place_size", int(plan["size"]))
    order_msg = f"Order:   GTC {place_size} @ ${to_price}  commits up to ${place_size*to_price:.2f}"
    if place_size < int(plan["size"]):
        order_msg += f"  [dim](budget-capped from {int(plan['size'])} sh)[/dim]"
    console.print(order_msg)

    if dry_run:
        console.print("[dim]dry-run[/dim]")
        return
    if placed is None:
        console.print("[yellow]not placed[/yellow] — order under the 5-share exchange minimum")
        return

    status = placed.get("status", "?")
    if filled > 0:
        console.print(
            f"Fill:    {filled:.0f} sh @ avg ${cost/filled:.4f} = ${cost:.2f}  "
            f"({status})  [dim]{placed.get('orderID', '')}[/dim]"
        )
    else:
        console.print(f"Fill:    none yet — order resting ({status})  [dim]{placed.get('orderID', '')}[/dim]")

    if flip_price is not None:
        if flip_res is None:
            console.print("[yellow]flip skipped[/yellow] — nothing filled to resell")
            return
        console.print(f"Flip:    SELL {int(filled)} @ ${flip_price}")
        console.print(f"         {flip_res.get('status', '?')}  [dim]{flip_res.get('orderID', '')}[/dim]")


def _resolve_token(value: str) -> str:
    """Lightweight token validator used by `flip`, `book`, `engine trades`."""
    if not value.isdigit():
        raise click.BadParameter(f"Token must be numeric (got {value!r})")
    return value


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


def _label_for_order(order: dict) -> str:
    """Resolve a market label for display via the cached CLOB lookup."""
    from polymarket import lookup_market_name

    asset = str(order.get("asset_id", ""))
    cid = order.get("market")
    if cid:
        name = lookup_market_name(cid)
        if name:
            outcome = (order.get("outcome", "") or "").upper()
            return f"{name[:30]} {outcome}"
    return "…" + asset[-10:]


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Emit raw open orders as JSON")
def orders(as_json: bool = False) -> None:
    """List open resting orders."""
    from polymarket import locked_buy_cash

    rows = _api().get_orders()
    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return
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
        # Locked counts only the unfilled remainder, matching `pmt balance`.
        locked_cash += locked_buy_cash(d)
        market = _label_for_order(d)
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
@click.option("--json", "as_json", is_flag=True, help="Emit as JSON")
def balance(as_json: bool) -> None:
    """Cash view: total USDC, cash locked in resting BUY orders, spendable rest."""
    b = _api().get_usdc_balance()
    if as_json:
        click.echo(json.dumps(b, indent=2))
        return
    t = Table(title="USDC balance", show_header=False)
    t.add_column("key", style="bold")
    t.add_column("value", justify="right")
    t.add_row("total", f"${b['total']:,.2f}")
    t.add_row("locked", f"${b['locked']:,.2f}")
    t.add_row("available", f"[bold]${b['available']:,.2f}[/bold]")
    console.print(t)


@cli.command()
@click.option("--orders/--no-orders", "with_orders", default=False, help="Include open orders")
@click.option("--themes", default=None, help="Comma-separated theme names")
@click.option("--json", "as_json", is_flag=True, help="Emit raw positions as JSON")
def positions(with_orders: bool, themes: str | None, as_json: bool = False) -> None:
    """Portfolio view: positions, exposure, theme correlation."""
    api = _api()
    raw = api.get_positions()
    if as_json:
        click.echo(json.dumps(raw, indent=2, default=str))
        return
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
@click.option("--json", "as_json", is_flag=True, help="Emit ui + realized P&L as JSON")
def pnl(top: int, as_json: bool) -> None:
    """P&L over 1d/7d/30d/all-time — headline matches the polymarket.com profile.

    The headline row is the site's own user-pnl series (mark-to-market:
    realized + open positions marked at current odds). The breakdown below
    replays the activity stream into a cost-basis ledger and counts realized
    cash flow only — the two legitimately differ while positions are open."""
    import time
    from polymarket.pnl import (
        replay_activity, reconcile_expired, bucket_by_window,
    )

    api = _api()
    with console.status("[dim]fetching profile P/L + paginating /activity…[/dim]"):
        ui = api.get_ui_pnl()
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
        ("sell",    "Trade SELL",  bucket_by_window(sells,   now, windows)),
        ("redeem",  "Redemptions", bucket_by_window(redeems, now, windows)),
        ("expired", "Expired (worthless)", bucket_by_window(expired, now, windows)),
        ("reward",  "Rewards",     bucket_by_window(rewards, now, windows)),
        ("yield",   "Yield",       bucket_by_window(yields,  now, windows)),
    ]
    subtotal_per_window = {w: sum(b[w] for _, _, b in rows) for w in windows}

    unrealized = sum(
        p.get("cashPnl", p.get("currentValue", 0) - p["size"] * p["avgPrice"])
        for p in positions
    )
    grand = subtotal_per_window["all"] + unrealized

    if as_json:
        click.echo(json.dumps({
            "ui": ui,
            "realized": subtotal_per_window,
            "realized_breakdown": {key: bucket for key, _, bucket in rows},
            "unrealized_snapshot": unrealized,
            "grand_total": grand,
            "activity_events": len(acts),
        }, indent=2))
        return

    t0 = Table(title="P&L — polymarket.com profile (mark-to-market)")
    for w in windows:
        t0.add_column(w, justify="right")
    hrow = []
    for w in windows:
        v = ui.get(w)
        hrow.append("[dim]—[/dim]" if v is None else f"[bold {_pnl_color(v)}]${v:+.2f}[/]")
    t0.add_row(*hrow)
    console.print(t0)
    console.print()

    t = Table(title=f"Realized-only breakdown  ({len(acts)} activity events)")
    t.add_column("Source", justify="left")
    for w in windows:
        t.add_column(w, justify="right")
    for _, label, bucket in rows:
        row = [label]
        for w in windows:
            v = bucket[w]
            c = _pnl_color(v)
            row.append(f"[{c}]${v:+.2f}[/]")
        t.add_row(*row)
    t.add_section()
    sub_row = ["[bold]Realized subtotal[/bold]"]
    for w in windows:
        v = subtotal_per_window[w]
        sub_row.append(f"[bold {_pnl_color(v)}]${v:+.2f}[/]")
    t.add_row(*sub_row)
    console.print(t)

    console.print()
    c = _pnl_color(unrealized)
    console.print(f"Current [bold]unrealized[/bold] (mark - cost): [{c}]${unrealized:+.2f}[/]")
    c = _pnl_color(grand)
    console.print(f"[bold]Grand total[/bold] (all-time realized + current unrealized): [{c}]${grand:+.2f}[/]")
    if ui.get("all") is not None:
        resid = ui["all"] - grand
        console.print(f"[dim]vs profile all-time ${ui['all']:+.2f} — Δ ${resid:+.2f} ledger reconstruction error[/dim]")

    console.print()
    console.print("[dim]Headline = the site's own P/L series (realized + open-position "
                  "drift at current odds). Breakdown = realized cash flow only.[/dim]")

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
                idx = outcomes.index("Yes") if "Yes" in outcomes else -1
                yes_p = float(prices[idx]) if idx != -1 and idx < len(prices) else None
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


@cli.command("fit")
@click.argument("ref")
@click.option("--lookback", default=90, show_default=True, help="Vol lookback in daily candles.")
@click.option("--json", "as_json", is_flag=True, help="Emit the fit as JSON")
def fit_cmd(ref: str, lookback: int, as_json: bool) -> None:
    """Compare an event's touch buckets against the realized distribution.

    Computes realized vol from the resolution exchange's own candles, prices
    every ↓/↑ barrier bucket analytically (2Φ(-z)) and empirically (rolling
    same-length windows), and flags buckets whose market price doesn't fit.
    Buckets that already touched since market creation are reported as settled.

    \b
    REF is a polymarket.com event URL or bare slug.

    \b
    Examples:
      pmt fit what-price-will-bitcoin-hit-in-august-2026
      pmt fit some-event --lookback 180 --json
    """
    from polymarket.fit import fit_event
    from polymarket.scanner import parse_event_ref

    try:
        data = fit_event(parse_event_ref(ref), lookback)
    except ValueError as e:
        raise click.UsageError(str(e))

    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
        return

    console.print(f"[bold]{data['event_title']}[/bold]")
    console.print(
        f"  {data['symbol']}  spot ${data['spot']:,.0f}   "
        f"daily σ {data['sigma_daily'] * 100:.2f}% ({data['lookback_days']}d lookback)"
    )

    t = Table(title="DISTRIBUTION FIT")
    for col, j in (("Bucket", "left"), ("Dist", "right"), ("σ req", "right"), ("P model", "right"),
                   ("P emp", "right"), ("Mkt YES", "right"), ("Edge", "right"), ("Flag", "left")):
        t.add_column(col, justify=j)

    def _pct(p: float | None) -> str:
        return f"{p * 100:.1f}%" if p is not None else "-"

    for b in data["buckets"]:
        if b["status"] in ("unsupported", "no end date", "expired"):
            t.add_row(b["title"], "-", "-", "-", "-", "-", "-", f"[dim]{b['status']}[/dim]")
            continue
        edge = b["edge"]
        if edge is None:
            edge_s = "-"
        else:
            col = "green" if edge > 0 else "red"
            edge_s = f"[{col}]{edge * 100:+.1f}%[/{col}]"
        flag = b["flag"]
        if "check price" in flag:
            flag = f"[bold red]{flag}[/bold red]"
        elif "cheap" in flag:
            flag = f"[yellow]{flag}[/yellow]"
        t.add_row(
            b["title"],
            f"{b['distance_pct']:+.1f}%",
            f"{b['sigma_required']:.2f}σ" if b["status"] == "ok" else "-",
            _pct(b["p_model"]),
            _pct(b["p_empirical"]),
            _pct(b["market_yes"]),
            edge_s,
            flag,
        )
    console.print(t)
    console.print(
        "[dim]P emp = share of rolling same-length historical windows that touched; "
        "edge = P emp − market YES; TOUCHED = already hit since market creation.[/dim]"
    )


class _ScanGroup(click.Group):
    """Bare `pmt scan REF` runs the due-diligence scan; `cliff`/`expiring` still resolve as named subcommands."""

    def resolve_command(self, ctx, args):
        implicit = bool(args) and not args[0].startswith("-") and args[0] not in self.commands
        if implicit:
            args = ["diligence", *args]
        name, cmd, rest = super().resolve_command(ctx, args)
        if implicit:
            # user typed `pmt scan REF`, not `pmt scan diligence` — don't leak the internal
            # routing name into usage/error banners (blanking it costs a cosmetic extra space)
            name = ""
        return name, cmd, rest


@cli.group(cls=_ScanGroup)
def scan() -> None:
    """Market opportunity scanners. Bare `pmt scan REF` runs due-diligence; see also cliff/expiring."""


@scan.command("diligence")
@click.argument("ref")
@click.option("--match", default=None, help="Target sub-market by keyword (default: highest-volume open one).")
@click.option("--json", "as_json", is_flag=True, help="Emit the scan as JSON")
def scan_diligence(ref: str, match: str | None, as_json: bool) -> None:
    """Pre-trade due-diligence scan for an event's sub-markets.

    Checks thinness (low volume at a non-extreme price), resolution-risk
    chatter in comments, smart-money side asymmetry on the target
    sub-market, and how identically-shaped sibling sub-markets already
    resolved.

    \b
    REF is a polymarket.com event URL or bare slug.

    \b
    Examples:
      pmt scan israel-x-hamas-ceasefire-cancelled-by-october-31
      pmt scan some-event --match "December" --json
    """
    from polymarket.scanner import scan_event

    try:
        data = scan_event(ref, match)
    except ValueError as e:
        raise click.UsageError(str(e))

    if as_json:
        click.echo(json.dumps(data, indent=2, default=str))
        return

    ev = data["event"]
    console.print(f"[bold]{ev['title']}[/bold]")
    console.print(
        f"  volume ${ev['volume']:,.0f}   liquidity ${ev['liquidity']:,.0f}   comments {ev['commentCount']}"
    )

    if data["markets"]:
        t = Table(title="MARKETS")
        t.add_column("Sub-market", justify="left")
        t.add_column("Yes", justify="right")
        t.add_column("Volume", justify="right")
        t.add_column("Flag", justify="left")
        for m in data["markets"]:
            px = f"{m['yes_price']:.3f}" if m["yes_price"] is not None else "-"
            flag = "[yellow]thin[/yellow]" if m["thin"] else ""
            t.add_row(m["title"], px, f"${m['volume']:,.0f}", flag)
        console.print(t)
    else:
        console.print("[dim]No open sub-markets.[/dim]")

    cm_data = data["comments"]
    pct = cm_data["ratio"] * 100
    console.print(
        f"\n[bold]RESOLUTION RISK[/bold]  {len(cm_data['flagged'])}/{cm_data['total']} flagged  ({pct:.0f}%)"
    )
    for c in cm_data["flagged"][:3]:
        author = (c.get("profile") or {}).get("name") or "?"
        created = c.get("createdAt") or ""  # explicit null on deleted/legacy comments, not just missing
        try:
            date = datetime.fromisoformat(created.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            date = created[:10]
        body = (c.get("body") or "").strip().replace("\n", " ")
        snippet = body[:160] + ("..." if len(body) > 160 else "")
        console.print(f"  [dim]{date}[/dim] [cyan]{author}[/cyan]: {snippet}")

    console.print("\n[bold]SIBLING PRECEDENT[/bold]")
    if data["siblings"]:
        for s in data["siblings"]:
            console.print(f"  {s['title']}  →  [bold]{s['outcome']}[/bold]   (${s['volume']:,.0f} vol)")
    else:
        console.print("[dim]No resolved sibling sub-markets.[/dim]")

    tgt = data["target"]
    console.print(f"\n[bold]SMART MONEY[/bold]  — {tgt['title'] or '(no target sub-market)'}")

    def holder_table(title: str, holders: list[dict]) -> Table:
        ht = Table(title=title)
        ht.add_column("Name", justify="left")
        ht.add_column("Shares", justify="right")
        ht.add_column("Lifetime PnL", justify="right")
        ht.add_column("Lifetime Vol", justify="right")
        ht.add_column("Markets", justify="right")
        ht.add_column("Score", justify="right")
        for h in sorted(holders, key=lambda x: -x.get("amount", 0)):
            col = _pnl_color(h.get("pnl", 0))
            ht.add_row(
                h.get("name") or h.get("pseudonym") or "?",
                f"{h.get('amount', 0):,.1f}",
                f"[{col}]${h.get('pnl', 0):+,.0f}[/{col}]",
                f"${h.get('volume', 0):,.0f}",
                f"{h.get('markets', 0)}",
                f"{h.get('score', 0):.2f}",
            )
        return ht

    if data["holders_yes"]:
        console.print(holder_table("YES holders", data["holders_yes"]))
    else:
        console.print("[dim]No YES holders.[/dim]")
    if data["holders_no"]:
        console.print(holder_table("NO holders", data["holders_no"]))
    else:
        console.print("[dim]No NO holders.[/dim]")
    console.print(f"  side score — YES {data['score_yes']:.2f}   NO {data['score_no']:.2f}")

    console.print(f"\n[bold]VERDICT[/bold]  {data['verdict']}")


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
@click.argument("ref")
@click.argument("outcome", required=False)
@click.option("--match", default=None, help="Disambiguate sub-markets by keyword.")
@click.option("--depth", default=10, type=int, help="Levels per side (default 10)")
@click.option("--json", "as_json", is_flag=True, help="Emit the normalized book as JSON")
def book(ref, outcome, match, depth, as_json):
    """Show the order book for REF (URL/slug/token). OUTCOME defaults to yes."""
    api = _api()
    token, ev, market, outcome_name = _resolve_ref(api, ref, outcome or "yes", match)
    b = api.book_levels(token, depth=depth)
    if as_json:
        click.echo(json.dumps(b, indent=2, default=str))
        return

    bids, asks = b["bids"], b["asks"]
    if not bids and not asks:
        console.print("[dim]Empty book.[/dim]")
        return

    label = _market_label(market)
    title = f"{label} — {outcome_name}" if label else f"Book for {token[:12]}…"
    t = Table(title=title)
    for col in ("Side", "Price", "Size", "Cum $"):
        t.add_column(col, justify="right" if col != "Side" else "left")
    # Cum $ accumulates outward from the touch on each side.
    cum = 0.0
    ask_rows = []
    for p, sz in asks:  # best-ask first
        cum += p * sz
        ask_rows.append((p, sz, cum))
    for p, sz, c in reversed(ask_rows):
        t.add_row("ASK", f"${p:.4f}", f"{sz:.2f}", f"${c:,.2f}")
    t.add_section()
    mid = f"${b['mid']:.4f}" if b["mid"] is not None else "—"
    spr = f"spread ${b['spread']:.4f}" if b["spread"] is not None else ""
    t.add_row("[bold]MID[/bold]", f"[bold]{mid}[/bold]", "", f"[dim]{spr}[/dim]")
    t.add_section()
    cum = 0.0
    for p, sz in bids:  # best-bid first
        cum += p * sz
        t.add_row("BID", f"${p:.4f}", f"{sz:.2f}", f"${cum:,.2f}")
    console.print(t)


@cli.group()
def engine() -> None:
    """Talk to a locally-running pmengine via its control plane.

    The engine binds its control plane to 127.0.0.1:7531 by default. Override
    with PMENGINE_CONTROL_URL.
    """
    pass


_ENGINE_ROOT = "/var/home/hunter/Desktop/code/pmt/pmengine"
_ENGINE_STATE = "/var/home/hunter/.pmt/engine"


def _engine_bin() -> str:
    release = f"{_ENGINE_ROOT}/target/release/pmengine"
    debug = f"{_ENGINE_ROOT}/target/debug/pmengine"
    import os

    return release if os.path.exists(release) else debug


@engine.command("start")
@click.argument("strategies", nargs=-1)
@click.option("--tick-ms", default=50, show_default=True)
def engine_start(strategies: tuple[str, ...], tick_ms: int) -> None:
    """Start pmengine detached (default strategy: updown). Logs + pidfile in ~/.pmt/engine."""
    import os
    import subprocess
    import time as _time

    os.makedirs(_ENGINE_STATE, exist_ok=True)
    pidfile = f"{_ENGINE_STATE}/pmengine.pid"
    log = f"{_ENGINE_STATE}/engine-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    env = {**os.environ, "PMENGINE_TICK_INTERVAL_MS": str(tick_ms)}
    cmd = [_engine_bin(), "--env-file", "/var/home/hunter/Desktop/code/pmt/.env",
           "run", *(strategies or ("updown",)), "--skip-warmup"]
    with open(log, "ab") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, env=env, start_new_session=True)
    with open(pidfile, "w") as pf:
        pf.write(str(proc.pid))
    _time.sleep(2)
    try:
        s = _engine_get("/status")
        console.print(f"[green]engine up[/green] pid {proc.pid} · dry_run {s['dry_run']} · "
                      f"bin {_engine_bin().rsplit('/target/', 1)[-1]} · log {log}")
    except SystemExit:
        console.print(f"[red]engine did not come up — check {log}[/red]")


@engine.command("kill")
def engine_kill() -> None:
    """Stop the running pmengine process (`engine stop` stops a strategy)."""
    import os
    import signal as _signal
    import subprocess

    pidfile = f"{_ENGINE_STATE}/pmengine.pid"
    stopped = False
    if os.path.exists(pidfile):
        try:
            os.kill(int(open(pidfile).read().strip()), _signal.SIGTERM)
            stopped = True
        except (ProcessLookupError, ValueError):
            pass
        os.remove(pidfile)
    if not stopped:  # fallback for engines started outside `pmt engine start`
        stopped = subprocess.run(["pkill", "-f", "pmengine.*run"], capture_output=True).returncode == 0
    console.print("[green]stopped[/green]" if stopped else "no engine running")


@engine.command("restart")
@click.pass_context
def engine_restart(ctx: click.Context) -> None:
    """Stop then start pmengine (picks up a freshly-built binary)."""
    ctx.invoke(engine_kill)
    import time as _time

    _time.sleep(1)
    ctx.invoke(engine_start, strategies=(), tick_ms=50)


@engine.command("logs")
@click.option("-n", default=40, show_default=True)
@click.option("-f", "--follow", is_flag=True, help="Stream live (Tick spam filtered)")
def engine_logs(n: int, follow: bool) -> None:
    """Tail the most recent engine log."""
    import glob
    import subprocess

    logs = sorted(glob.glob(f"{_ENGINE_STATE}/engine-*.log"))
    if not logs:
        console.print("no engine logs yet")
        return
    if follow:
        # 50ms ticks make the raw stream unreadable — drop the heartbeat.
        subprocess.run(["bash", "-c", f"tail -n {n} -f '{logs[-1]}' | grep -v --line-buffered ' Tick '"])
    else:
        subprocess.run(["tail", "-n", str(n), logs[-1]])


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
    for col in ("id", "state", "tick interval", "tokens", "last tick"):
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
        state = "[yellow]paused[/yellow]" if r.get("paused") else "[green]active[/green]"
        t.add_row(r["id"], state, f"{r['tick_interval_ms']}ms", token_disp, last_disp)
    console.print(t)


@engine.command("pause")
@click.argument("strategy")
def engine_pause(strategy: str) -> None:
    """Pause a strategy: stop ticking it + pull its resting orders. No restart."""
    _engine_post(f"/strategies/{strategy}/pause")
    console.print(f"[yellow]paused[/yellow] {strategy} (orders pulled; resume with `pmt engine resume {strategy}`)")


@engine.command("resume")
@click.argument("strategy")
def engine_resume(strategy: str) -> None:
    """Resume a paused strategy; it quotes again on its next tick."""
    _engine_post(f"/strategies/{strategy}/resume")
    console.print(f"[green]resumed[/green] {strategy}")


@engine.command("stop")
@click.argument("strategy")
def engine_stop(strategy: str) -> None:
    """Stop + remove a strategy (runs on_shutdown, pulls orders). Needs restart to re-add."""
    _engine_post(f"/strategies/{strategy}/stop")
    console.print(f"[red]stopped[/red] {strategy} (removed from runtime)")


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


# ============================================================
# Sports data (ESPN reference for pricing sports markets)
# ============================================================


@cli.group("crypto")
def crypto_group() -> None:
    """Crypto up/down market pricing against Binance/Chainlink data."""


@crypto_group.command("spot")
@click.argument("symbol", default="BTCUSDT")
def crypto_spot(symbol: str) -> None:
    """Live Binance spot price (default BTCUSDT)."""
    from polymarket.crypto import spot_price

    console.print(f"{symbol.upper()}: {spot_price(symbol.upper()):,.2f}")


@crypto_group.command("updown")
@click.argument("ref")
@click.option("--json", "as_json", is_flag=True, help="Emit the full eval as JSON")
def crypto_updown(ref: str, as_json: bool) -> None:
    """Price an up/down market: semantics, fair value, book edge, verdict.

    REF is a polymarket.com event URL or bare slug. Detects TWAP vs
    close-vs-open resolution from the description and models accordingly.
    """
    from polymarket.crypto import eval_updown

    try:
        r = eval_updown(ref)
    except ValueError as e:
        raise click.UsageError(str(e))
    if as_json:
        console.print_json(json.dumps(r))
        return

    m = r["model"]
    console.print(f"[bold]{r['title']}[/bold]  [dim]{r['kind']} · {r['symbol']} · {r['rem_s']:.0f}s left[/dim]")
    if m.get("pending"):
        console.print(f"spot {r['spot']:,.0f}  σ {r['sigma_bp_per_min']:.2f}bp/min")
    else:
        if r["kind"] == "twap":
            line = (f"spot {r['spot']:,.0f}  ref {m['ref']:,.0f}  banked({m.get('banked_s', 0):.0f}s) "
                    f"{m['banked']:,.0f}")
            if not m.get("expired"):
                line += f"  proj {m['proj']:,.0f}  breakeven {m['breakeven']:,.0f}"
        else:
            line = f"spot {r['spot']:,.0f}  open {m['open']:,.0f}"
        console.print(f"{line}  margin {m['margin_bp']:+.1f}bp  σ {r['sigma_bp_per_min']:.2f}bp/min")
        if not m.get("expired"):
            console.print(f"P(UP) [bold]{m['p_up']:.3f}[/bold]  [dim]vol x0.5: {m['p_up_lowvol']:.3f} · x1.5: {m['p_up_highvol']:.3f}[/dim]")

    t = Table(show_header=True)
    for col in ("side", "fair", "bid", "ask", "cost w/fee", "net edge"):
        t.add_column(col, justify="right")
    for side in ("up", "down"):
        e = r["edges"][side]
        fmt = lambda v, p="{:.3f}": p.format(v) if v is not None else "—"
        edge = f"{e['net_edge'] * 100:+.1f}¢" if e["net_edge"] is not None else "—"
        t.add_row(side.upper(), f"{e['fair']:.3f}", fmt(e["bid"]), fmt(e["ask"]),
                  fmt(e["taker_cost"]), edge)
    console.print(t)
    console.print(f"[bold]{r['verdict']}[/bold]")


@crypto_group.command("arm")
@click.argument("ref")
@click.option("--size", type=float, required=True, help="Max notional (USDC) the trigger may spend")
@click.option("--min-edge", type=float, default=0.015, show_default=True, help="Min net-of-fee edge to fire")
@click.option("--max-price", type=float, default=0.985, show_default=True, help="Never pay above this")
@click.option("--side", type=click.Choice(["up", "down"]), default=None, help="Restrict to one side")
@click.option("--quiesce", type=float, default=20.0, show_default=True, help="No orders in the final N seconds")
@click.option("--min-fair", type=float, default=0.97, show_default=True,
              help="Only buy a side the model prices at least this high (the safety gate)")
@click.option("--min-elapsed", type=float, default=0.5, show_default=True,
              help="No fires before this fraction of the window has elapsed")
@click.option("--roll/--no-roll", default=True, show_default=True,
              help="Auto-rearm the next window in the series at close (same budget)")
@click.option("--clip", type=float, default=25.0, show_default=True,
              help="Max notional per individual fire (position builds in clips)")
def crypto_arm(ref: str, size: float, min_edge: float, max_price: float,
               side: str | None, quiesce: float, min_fair: float, min_elapsed: float,
               roll: bool, clip: float) -> None:
    """Arm the pmengine updown trigger on a market.

    Prices the market (semantics, vol, fee) and hands the parameters to the
    running engine's `updown` strategy, which watches Binance + the book and
    takes the ask only while every gate holds. Engine must be running with
    the strategy loaded: `pmengine run updown`.
    """
    from polymarket.crypto import eval_updown

    try:
        r = eval_updown(ref)
    except ValueError as e:
        raise click.UsageError(str(e))
    if r["rem_s"] <= 0:
        raise click.UsageError("window already over")
    payload = {
        "action": "arm", "slug": r["slug"], "kind": r["kind"], "symbol": r["symbol"],
        "token_up": r["tokens"]["up"], "token_down": r["tokens"]["down"],
        "start": r["start"], "end": r["end"],
        "sigma_bp_per_min": r["sigma_bp_per_min"], "fee_rate": r["fee_rate"],
        "size_usdc": size, "min_edge": min_edge, "max_price": max_price,
        "quiesce_secs": quiesce, "min_fair": min_fair, "min_elapsed_frac": min_elapsed,
        "roll": roll, "clip_usdc": clip,
    }
    if side:
        payload["side_filter"] = side
    reply = _engine_post("/strategies/updown/command", payload)
    rolling = " · rolling" if roll else ""
    console.print(f"[green]armed[/green] {reply.get('armed')}  "
                  f"[dim]{r['kind']} · {r['rem_s']:.0f}s left · size ${size:.0f} · "
                  f"min edge {min_edge * 100:.0f}¢ · σ {r['sigma_bp_per_min']:.2f}bp/min"
                  f"{rolling}[/dim]")
    console.print(f"[dim]market now: {r['verdict']}[/dim]")


@crypto_group.command("disarm")
@click.argument("slug", required=False)
def crypto_disarm(slug: str | None) -> None:
    """Disarm one armed market (SLUG) or all of them (no arg)."""
    body = {"action": "disarm"}
    if slug:
        body["slug"] = slug
    reply = _engine_post("/strategies/updown/command", body)
    console.print(f"disarmed: {reply.get('disarmed') or '(was idle)'} · {reply.get('arms', 0)} arms left")


@crypto_group.command("trigger")
def crypto_trigger() -> None:
    """Live state of the updown trigger: gates, model, edges."""
    reply = _engine_post("/strategies/updown/command", {"action": "status"})
    console.print_json(json.dumps(reply))


def _tape_slug(slug: str) -> str:
    """btc-updown-5m-1787442000 -> 'btc 5m 23:40' (window start, local time)."""
    import re
    import time as _t

    m = re.match(r"([a-z]+)-updown-(\d+m)-(\d+)$", slug)
    if not m:
        return slug
    start = _t.strftime("%H:%M", _t.localtime(int(m.group(3))))
    return f"{m.group(1)} {m.group(2)} {start}"


def _tape_render(line: str) -> str | None:
    import time as _t

    try:
        r = json.loads(line)
    except ValueError:
        return None
    ts = _t.strftime("%H:%M:%S", _t.localtime(r.get("t", 0)))
    head = f"{ts}  {_tape_slug(r.get('slug', '')):<13}"

    def money(v: float) -> str:
        return f"${v:,.2f}".rstrip("0").rstrip(".")

    ev = r.get("ev")
    if ev == "fire":
        tag = {"flip": "FLIP", "spec": "SPEC"}.get(r.get("mode", "safe"), "FIRE")
        label = click.style(f"{tag} {r['side'].upper():<4}", fg="green", bold=True)
        pct = f"  {r['elapsed_frac'] * 100:.0f}% thru" if "elapsed_frac" in r else ""
        return (
            f"{head} {label} {r['size']:g}sh @ {r['ask']:.2f}"
            f"  fair {r['fair']:.4f}  {r['net'] * 100:+.1f}¢"
            f"  ρ{r['rho']:+.2f}  {money(r['committed'])} in{pct}"
        )
    if ev == "exit":
        label = click.style(f"EXIT {r['side'].upper():<4}", fg="red", bold=True)
        return f"{head} {label} {r['size']:g}sh @ bid {r['bid']:.2f}  fair {r['fair']:.4f}"
    if ev == "eval":
        best = max(r.get("sides") or [], key=lambda s: s["net"], default=None)
        book = (
            f"{best['side']} @ {best['ask']:.2f} {best['net'] * 100:+.1f}¢"
            if best
            else "no book"
        )
        banked = click.style("  BANKED", fg="cyan") if r.get("banked_decided") else ""
        body = (
            f"{head} eval  p↑{r['p_up']:.4f}  {book}"
            f"  ρ{r['rho']:+.2f}  {money(r['committed'])} in"
        )
        return click.style(body, dim=True) + banked
    if ev == "gated":
        return click.style(f"{head} gated  {r.get('reason', '?')}", fg="yellow", dim=True)
    if ev == "roll":
        return click.style(f"{head} ROLL  next window armed (${r['size']:g})", fg="cyan")
    if ev == "cleanup":
        return click.style(f"{head} ── window closed ──", dim=True)
    return line.rstrip()


@crypto_group.command("tape")
@click.option("-n", default=20, show_default=True)
@click.option("-f", "--follow", is_flag=True, help="Stream the decision tape live")
@click.option("--json", "as_json", is_flag=True, help="Raw JSONL records")
def crypto_tape(n: int, follow: bool, as_json: bool) -> None:
    """The strategy's decision tape: every fire, exit, eval, and gate."""
    import subprocess

    path = "/var/home/hunter/.pmt/engine/updown-tape.jsonl"
    cmd = ["tail", "-n", str(n)] + (["-f"] if follow else []) + [path]
    if as_json:
        subprocess.run(cmd)
        return
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            rendered = _tape_render(raw)
            if rendered:
                click.echo(rendered)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()


_TAPE_PATH = "/var/home/hunter/.pmt/engine/updown-tape.jsonl"
_V2_ERA = 1787441100


def _tape_scoreboard(floor: float) -> dict:
    """Aggregate the decision tape into W-L / est P&L / calibration.

    P&L assumes fires filled at their vwap up to the window's final
    committed notional — an estimate; wallet balance is the truth.
    """
    import time as _t

    fires: dict[str, list] = {}
    evals: dict[str, dict] = {}
    rolls = 0
    try:
        with open(_TAPE_PATH) as fh:
            for line in fh:
                r = json.loads(line)
                if r["t"] < floor:
                    continue
                if r["ev"] == "fire":
                    fires.setdefault(r["slug"], []).append(r)
                elif r["ev"] == "eval":
                    evals[r["slug"]] = r
                elif r["ev"] == "roll":
                    rolls += 1
    except FileNotFoundError:
        pass

    now = _t.time()
    series: dict[str, dict] = {}
    cal: dict[float, list] = {}
    wins = losses = 0
    net = 0.0
    for slug, fs in fires.items():
        ev = evals.get(slug)
        if not ev:
            continue
        sym, dur = slug.split("-")[0], slug.split("-")[2]
        end = int(slug.rsplit("-", 1)[1]) + int(dur[:-1]) * 60
        committed = ev["committed"]
        if committed < 1:
            continue
        s = series.setdefault(f"{sym} {dur}", {"w": 0, "l": 0, "open": 0, "pnl": 0.0, "usd": 0.0})
        s["usd"] += committed
        if now < end + 60:
            s["open"] += 1
            continue
        p_final = ev["p_up"]
        if not (p_final > 0.995 or p_final < 0.005):
            continue  # ambiguous close — excluded from the record
        won_side = "up" if p_final > 0.5 else "down"
        vwap = sum(f["size"] * f["ask"] for f in fs) / sum(f["size"] for f in fs)
        won = fs[0]["side"] == won_side
        pnl = committed / vwap - committed if won else -committed
        s["w" if won else "l"] += 1
        s["pnl"] += pnl
        wins, losses, net = wins + won, losses + (not won), net + pnl
        for f in fs:
            b = min(int(f["fair"] * 20) / 20, 0.95)
            cal.setdefault(b, [0, 0])
            cal[b][0] += 1
            cal[b][1] += f["side"] == won_side
    return {"wins": wins, "losses": losses, "net": net, "rolls": rolls,
            "series": series, "cal": cal}


@crypto_group.command("stats")
@click.option("--since", type=float, default=None,
              help="Hours of tape to include (default: the v2 fleet era)")
@click.option("--json", "as_json", is_flag=True)
def crypto_stats(since: float | None, as_json: bool) -> None:
    """Fleet scoreboard: est P&L, win rate, calibration, live arms, capital."""
    import time as _t

    floor = _t.time() - since * 3600 if since else _V2_ERA
    sb = _tape_scoreboard(floor)
    wins, losses, net, rolls = sb["wins"], sb["losses"], sb["net"], sb["rolls"]
    series, cal = sb["series"], sb["cal"]

    status, bal = {}, {}
    try:
        status = _engine_post("/strategies/updown/command", {"action": "status"})
    except Exception:
        pass
    try:
        bal = _api().get_usdc_balance()
    except Exception:
        pass

    if as_json:
        click.echo(json.dumps({
            "wins": wins, "losses": losses, "net_est": net, "rolls": rolls,
            "series": series, "calibration": {str(k): v for k, v in cal.items()},
            "arms": status.get("arms", {}), "balance": bal,
        }, indent=2))
        return

    n = wins + losses
    wr = f"{wins / n * 100:.0f}%" if n else "—"
    cap = f"${bal['total']:,.2f}" if bal else "?"
    console.print(f"[bold]{wins}W-{losses}L[/bold] ({wr}) · est P&L "
                  f"[{'green' if net >= 0 else 'red'}]{net:+,.2f}[/] · "
                  f"{rolls} rolls · capital {cap}")

    t = Table(title="By series (est, tape-derived)")
    for col in ("series", "record", "P&L", "notional"):
        t.add_column(col, justify="right")
    for k in sorted(series):
        s = series[k]
        rec = f"{s['w']}-{s['l']}" + (f" ({s['open']} open)" if s["open"] else "")
        t.add_row(k, rec, f"{s['pnl']:+,.2f}", f"${s['usd']:,.0f}")
    console.print(t)

    if cal:
        t = Table(title="Calibration: clips fired at stated fair vs realized")
        for col in ("fair ≥", "clips", "hit rate"):
            t.add_column(col, justify="right")
        for b in sorted(cal):
            tot, hit = cal[b]
            t.add_row(f"{b:.2f}", str(tot), f"{hit}/{tot} ({hit / tot * 100:.0f}%)")
        console.print(t)

    arms = status.get("arms", {})
    if arms:
        t = Table(title="Live arms")
        for col in ("window", "state", "committed", "fair", "roll"):
            t.add_column(col, justify="right")
        for slug, a in arms.items():
            e = a.get("eval") or {}
            state = e.get("state", "?")
            if state == "gated":
                state = (e.get("reason") or "gated")[:40]
            fair = f"{e['p_up']:.4f}" if "p_up" in e else "—"
            t.add_row(_tape_slug(slug), state, f"${e.get('committed', a.get('filled_usdc', 0)):,.2f}",
                      fair, "⟳" if a.get("roll") else "·")
        console.print(t)
        if status.get("pending_rolls"):
            console.print(f"[dim]pending rolls: {', '.join(status['pending_rolls'])}[/dim]")


@crypto_group.command("watch")
@click.option("--since", type=float, default=None,
              help="Hours of tape history for the scoreboard")
def crypto_watch(since: float | None) -> None:
    """Full-screen live dashboard: scoreboard + arms + streaming tape."""
    import time as _t
    from collections import deque

    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    floor = _t.time() - since * 3600 if since else _V2_ERA
    lines: deque = deque(maxlen=200)
    offset = 0
    try:
        with open(_TAPE_PATH) as fh:
            for raw in fh.readlines()[-60:]:
                r = _tape_render(raw)
                if r:
                    lines.append(r)
            offset = fh.tell()
    except FileNotFoundError:
        pass

    sb = _tape_scoreboard(floor)
    status: dict = {}
    bal: dict = {}

    def header() -> Panel:
        wins, losses, net = sb["wins"], sb["losses"], sb["net"]
        n = wins + losses
        wr = f"{wins / n * 100:.0f}%" if n else "—"
        cap = f"${bal['total']:,.2f}" if bal else "…"
        color = "green" if net >= 0 else "red"
        return Panel(
            f"[bold]{wins}W-{losses}L[/bold] ({wr}) · est P&L [{color}]{net:+,.2f}[/] · "
            f"{sb['rolls']} rolls · capital {cap} · [dim]{_t.strftime('%H:%M:%S')}[/dim]",
            title="updown fleet", border_style="cyan")

    def arms_table() -> Table:
        t = Table(expand=True, pad_edge=False)
        for col in ("window", "state", "committed", "fair", "roll"):
            t.add_column(col, justify="left" if col == "state" else "right")
        arms = status.get("arms") or {}
        for slug, a in arms.items():
            e = a.get("eval") or {}
            state = e.get("state", "?")
            if state == "gated":
                state = (e.get("reason") or "gated")[:48]
            fair = f"{e['p_up']:.4f}" if "p_up" in e else "—"
            t.add_row(_tape_slug(slug), state,
                      f"${e.get('committed', a.get('filled_usdc', 0)):,.2f}",
                      fair, "⟳" if a.get("roll") else "·")
        if not arms:
            t.add_row("—", "[red]engine unreachable or no arms[/red]", "", "", "")
        return t

    def tape_panel(height: int) -> Panel:
        shown = list(lines)[-max(height - 2, 1):]
        return Panel(Text.from_ansi("\n".join(shown)), title="tape", border_style="dim")

    layout = Layout()
    layout.split_column(
        Layout(name="head", size=3),
        Layout(name="arms", size=10),
        Layout(name="tape", ratio=1),
    )

    tick = 0
    try:
        with Live(layout, refresh_per_second=4, screen=True) as live:
            while True:
                try:
                    with open(_TAPE_PATH) as fh:
                        fh.seek(offset)
                        for raw in fh:
                            r = _tape_render(raw)
                            if r:
                                lines.append(r)
                        offset = fh.tell()
                except FileNotFoundError:
                    pass
                if tick % 2 == 0:
                    try:
                        status = _engine_post("/strategies/updown/command",
                                              {"action": "status"})
                    except Exception:
                        status = {}
                if tick % 10 == 0:
                    sb = _tape_scoreboard(floor)
                # Balance is a slow call — after first paint, then per minute.
                if tick % 60 == 1:
                    try:
                        bal = _api().get_usdc_balance()
                    except Exception:
                        pass
                layout["arms"].size = max(len(status.get("arms") or {}), 1) + 4
                layout["head"].update(header())
                layout["arms"].update(arms_table())
                h = live.console.size.height - 3 - layout["arms"].size
                layout["tape"].update(tape_panel(h))
                tick += 1
                _t.sleep(1)
    except KeyboardInterrupt:
        pass


@cli.group("sports")
def sports_group() -> None:
    """Live sports data: scores, game state, ESPN win probability."""


@sports_group.command("board")
@click.argument("league")
@click.option("--date", default=None, help="YYYYMMDD (default today)")
def sports_board(league: str, date: str | None) -> None:
    """Scoreboard for a league (mlb, nfl, nba, nhl, wnba, ncaaf, ncaab, mls, epl)."""
    from polymarket import espn

    try:
        games = espn.scoreboard(league, date)
    except ValueError as e:
        raise click.UsageError(str(e))
    t = Table(title=f"{league.upper()} scoreboard")
    for col in ("Event", "Matchup", "Score", "State"):
        t.add_column(col)
    for g in games:
        score = " - ".join(f"{tm['abbrev']} {tm['score'] or ''}".strip() for tm in g["teams"])
        t.add_row(g["event_id"], g["short_name"], score, g["detail"] or g["state"] or "")
    console.print(t)


@sports_group.command("game")
@click.argument("league")
@click.argument("ref")
def sports_game_cmd(league: str, ref: str) -> None:
    """Game state + ESPN win prob. REF is an event id or team-name substring."""
    from polymarket import espn

    try:
        if not ref.isdigit():
            matches = espn.find_games(league, ref)
            if not matches:
                raise click.UsageError(f"No {league} game matches '{ref}' today")
            ref = matches[0]["event_id"]
        g = espn.game_state(league, ref)
    except ValueError as e:
        raise click.UsageError(str(e))
    home, away = g["teams"].get("home", {}), g["teams"].get("away", {})
    console.print(f"[bold]{away.get('name')} @ {home.get('name')}[/bold]  ({g['detail']})")
    console.print(f"  score: {away.get('abbrev')} {away.get('score')} - {home.get('abbrev')} {home.get('score')}")
    if g["home_win_prob"] is not None:
        console.print(f"  ESPN win prob: {home.get('abbrev')} {g['home_win_prob']:.1%} / {away.get('abbrev')} {g['away_win_prob']:.1%}")
    if g["situation"]:
        console.print(f"  situation: {g['situation']}")
    for o in g["odds"]:
        console.print(f"  [dim]{o['book']}: spread {o['spread']} O/U {o['over_under']} ML {o['away_ml']}/{o['home_ml']}[/dim]")


@sports_group.command("watch")
@click.argument("league")
@click.argument("ref")
@click.option("--slug", default=None, help="Override the Polymarket event slug")
@click.option("--pos", default=None, help="TEAM:SIZE@PRICE override; default = your live positions")
@click.option("--interval", default=2.0, show_default=True, help="ESPN poll seconds")
@click.option("--log", "log_path", default=None, help="JSONL path (default ~/.pmt/gamewatch/)")
@click.option("--no-log", is_flag=True, help="Don't record the session")
@click.option("--duration", default=None, type=float, help="Auto-stop after N seconds")
def sports_watch_cmd(league, ref, slug, pos, interval, log_path, no_log, duration) -> None:
    """Live dashboard: game state vs the Polymarket moneyline, with
    game→market correlation and reaction-latency stats. Ctrl+C to stop."""
    from polymarket.gamewatch import resolve_moneyline, run_watch

    try:
        resolved = resolve_moneyline(league, ref, slug=slug)
    except ValueError as e:
        raise click.UsageError(str(e))
    run_watch(
        league, ref, slug=slug, pos=pos, interval=interval,
        log_path=log_path, no_log=no_log, duration=duration, resolved=resolved,
    )


if __name__ == "__main__":
    cli()
