"""`pmt crypto` commands that touch the live fleet: pricing a window,
arming the engine's updown strategy on it, and reading back what the arms are
doing right now.

These are the only crypto commands that MUTATE anything — `arm`, `disarm` and
`fleet` post to a running pmengine — which is why they live apart from the
read-only reporting surface. `updown` and `trigger` are their read halves: the
same market model an arm hands over, and the same state the engine hands back.

Registered onto the `crypto` group by cli_crypto.py.
"""

from __future__ import annotations

import json

import click
from rich.table import Table

from cli_common import console
from engine import post as _engine_post
from watch_ui import _brake_rich, _rtds_line, _safety_rich, _tape_slug


@click.command("updown")
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


def _resolve_basis_guard(explicit: float | None, symbol: str) -> tuple[float, str | None]:
    """(guard_bp, warning) for an arm on `symbol` (a Binance pair, 'ETHUSDT').

    An explicit `--basis-guard` always wins. Otherwise the MEASURED
    per-symbol guard, so a bare arm can't quietly under-guard an alt
    (docs/LESSONS.md#L32). A symbol with no measured corpus falls back to the
    flat band and says so out loud.
    """
    if explicit is not None:
        return explicit, None
    from polymarket.chainlink import guard_bp_for
    from polymarket.constants import BASIS_NOISE_BP

    measured = guard_bp_for(symbol)
    if measured is not None:
        return measured, None
    return BASIS_NOISE_BP, (
        f"no measured basis guard for {symbol} — falling back to {BASIS_NOISE_BP:.1f}bp. "
        f"Measure it (`pmt crypto basis --aligned`) or pass --basis-guard explicitly."
    )


@click.command("arm")
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
@click.option("--basis-guard", type=float, default=None,
              help="twap only: |projected margin| below this many bp is oracle "
                   "noise, no trade. Defaults to the MEASURED per-symbol guard "
                   "(polymarket.chainlink.GUARD_BP: btc 6, eth 8, sol 10); a "
                   "symbol with no measured corpus falls back to 3bp with a "
                   "warning. Both 2026-08-23 losses were thin-margin windows "
                   "inside the real Chainlink-vs-Binance basis, and a flat 3 "
                   "under-guards the alts by 2-3x")
@click.option("--theta", type=float, default=0.0, show_default=True,
              help="R9 safety gate: first clip needs banked-evidence safety "
                   "(|banked|/cushion, sign-matched to the side) at least this "
                   "high. 0 disables; ~0.3 blocked both 2026-08-23 post-brake "
                   "losses in replay; 1.0 = require banked-decided to enter")
@click.option("--settle-tw", type=float, default=0.0, show_default=True,
              help="Settlement TWAP width (seconds) the model prices against. "
                   "0 keeps the engine's duration default; 60 is the measured "
                   "truth at 5m (analysis/settle_width.md). Only terminal-"
                   "aware paths consult it; range_avg arms ignore it.")
@click.option("--pay-up", type=float, default=0.0, show_default=True,
              help="Fill-chase buffer: a clip's limit may sit up to this many "
                   "cents above the decision ask, funded only by surplus edge "
                   "over --min-edge (marketable limits fill at the book, so "
                   "it costs nothing unless the book moved). 0 disables; the "
                   "2026-08-23 audit measured 32% of taker notional unfilled")
@click.option("--p-cap", type=float, default=1.0, show_default=True,
              help="R6 tail honesty: cap the model's fair unless flip-proof — "
                   "Gaussian p_up 0.99+ is fiction in the tails (>3-sigma "
                   "jumps ~hourly). With min_edge 1.5c, 0.98 makes ~0.945 the "
                   "max ask a non-flip-proof clip pays. 1.0 disables")
@click.option("--maker-bid/--no-maker-bid", default=False, show_default=True,
              help="Maker step 0: rest ONE post-only bid on the side the model "
                   "wants when the book has NOTHING to lift. That miss class is "
                   "9.6% of armed time — bid pinned near 1.00 with no offer at "
                   "any price, median 82% through the window — and no taker "
                   "knob reaches it, because it is supply, not a gate. NOT a "
                   "market maker: early-window maker measured negative "
                   "everywhere (0.5c half-spread against 3.65c of drift per "
                   "5s), so this is a narrow, late-window, theta-approved "
                   "resting bid. Off by default. Read the maker_candidate "
                   "counts on the tape before arming it, and arm ONE symbol")
@click.option("--feed", type=click.Choice(["binance", "rtds"]), default="binance",
              show_default=True,
              help="Market data source. 'binance' is the venue proxy every arm "
                   "has used. 'rtds' reads the Chainlink TWAP stream these "
                   "markets actually SETTLE on — same series for reference, "
                   "spot and TWAP marks, so the cross-venue basis the guard "
                   "was sized for disappears (twap markets only; close_open "
                   "needs a venue's candle open). This is what makes xrp/doge "
                   "tradeable at all — see analysis/xrp_fit.md")
def crypto_arm(ref: str, size: float, min_edge: float, max_price: float,
               side: str | None, quiesce: float, min_fair: float, min_elapsed: float,
               roll: bool, clip: float, basis_guard: float | None, theta: float,
               pay_up: float, p_cap: float, maker_bid: bool, feed: str,
               settle_tw: float) -> None:
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
    # The engine refuses this too; catching it here saves a round trip and
    # says why in the same breath as the flag that caused it.
    if feed == "rtds" and r["kind"] != "twap":
        raise click.UsageError(
            f"--feed rtds does not support {r['kind']} markets — the settlement "
            "stream has no candle opens. Use --feed binance."
        )
    guard_bp, guard_warning = _resolve_basis_guard(basis_guard, r["symbol"])
    if guard_warning:
        console.print(f"[yellow]warning:[/yellow] {guard_warning}")
    payload = {
        "action": "arm", "slug": r["slug"], "kind": r["kind"], "symbol": r["symbol"],
        "token_up": r["tokens"]["up"], "token_down": r["tokens"]["down"],
        "start": r["start"], "end": r["end"],
        "sigma_bp_per_min": r["sigma_bp_per_min"], "fee_rate": r["fee_rate"],
        "size_usdc": size, "min_edge": min_edge, "max_price": max_price,
        "quiesce_secs": quiesce, "min_fair": min_fair, "min_elapsed_frac": min_elapsed,
        "roll": roll, "clip_usdc": clip, "basis_guard_bp": guard_bp,
        "theta": theta, "pay_up_max": pay_up, "p_cap": p_cap, "feed": feed,
        "maker_bid": maker_bid, "settle_tw_s": settle_tw,
    }
    if side:
        payload["side_filter"] = side
    reply = _engine_post("/strategies/updown/command", payload)
    rolling = " · rolling" if roll else ""
    making = " · maker-bid" if maker_bid else ""
    console.print(f"[green]armed[/green] {reply.get('armed')}  "
                  f"[dim]{r['kind']} · {r['rem_s']:.0f}s left · size ${size:.0f} · "
                  f"min edge {min_edge * 100:.0f}¢ · σ {r['sigma_bp_per_min']:.2f}bp/min · "
                  f"guard {guard_bp:.1f}bp · feed {feed}{rolling}{making}[/dim]")
    console.print(f"[dim]market now: {r['verdict']}[/dim]")


@click.command("disarm")
@click.argument("slug", required=False)
def crypto_disarm(slug: str | None) -> None:
    """Disarm one armed market (SLUG) or all of them (no arg)."""
    body = {"action": "disarm"}
    if slug:
        body["slug"] = slug
    reply = _engine_post("/strategies/updown/command", body)
    console.print(f"disarmed: {reply.get('disarmed') or '(was idle)'} · {reply.get('arms', 0)} arms left")


@click.command("fleet")
@click.option("--cap", type=float, default=None,
              help="USDC ceiling on total UN-DECIDED committed notional across all arms. "
                   "0 turns the cap off. Omit to just read the current setting.")
def crypto_fleet(cap: float | None) -> None:
    """Read or set the R7 fleet-wide un-decided exposure cap.

    The cap rations the one thing arms share: speculative committed
    notional, summed across every armed window. Banked-decided capital is
    outside it (R9 gates entry into that state), and so are exits, quiesce
    and flip clips. Off by default — this is a deliberate ration, never a
    default. It survives restarts in ~/.pmt/engine/arms-state.json.
    """
    if cap is None:
        reply = _engine_post("/strategies/updown/command", {"action": "status"})
        now = reply.get("fleet_undecided_cap", 0) or 0
        state = f"${now:,.0f}" if now > 0 else "[yellow]off[/yellow]"
        console.print(f"fleet un-decided cap: {state} · {reply.get('count', 0)} arms")
        return
    reply = _engine_post("/strategies/updown/command",
                         {"action": "fleet", "undecided_cap_usdc": cap})
    if reply.get("enabled"):
        console.print(f"fleet un-decided cap: [green]${reply['undecided_cap_usdc']:,.0f}[/green] "
                      f"· {reply.get('arms', 0)} arms")
    else:
        console.print(f"fleet un-decided cap: [yellow]off[/yellow] · {reply.get('arms', 0)} arms")


@click.command("trigger")
@click.option("--json", "as_json", is_flag=True, help="Raw status JSON")
def crypto_trigger(as_json: bool) -> None:
    """Live state of the updown fleet: one line per arm."""
    try:
        reply = _engine_post("/strategies/updown/command", {"action": "status"})
    except Exception as e:
        raise click.UsageError(f"engine unreachable ({e}) — try: pmt engine start")
    if as_json:
        console.print_json(json.dumps(reply))
        return
    arms = reply.get("arms", {})
    if not arms:
        console.print("engine up · [yellow]no arms[/yellow] — pmt crypto arm <url> --size N")
        return
    for slug, a in arms.items():
        e = a.get("eval") or {}
        state = e.get("state", "?")
        roll = "⟳" if a.get("roll") else " "
        # Which feed an arm reads is the difference between pricing the
        # settlement object and pricing a proxy for it — never leave it
        # implicit on a fleet running both.
        stream = " [cyan]≈[/cyan]" if a.get("feed") == "rtds" else "  "
        if state == "gated":
            body = f"[yellow]gated[/yellow]   {e.get('reason', '')}"
        elif state == "armed":
            body = "[green]armed[/green]"
            if "p_up" in e:
                body += f"   p↑{e['p_up']:.4f} ρ{e.get('rho', 0):+.2f} {e.get('mode', '')}"
            if e.get("banked_decided"):
                body += " [cyan]BANKED[/cyan]"
            sides = e.get("sides") or []
            saf = _safety_rich(sides, e.get("p_up"))
            if saf:
                body += f"  {saf}"
            brakes = _brake_rich(sides)
            if brakes:
                body += f"  {brakes}"
            committed = e.get("committed", a.get("filled_usdc", 0))
            body += f"  ${committed:,.2f}/${e.get('budget', 0):,.0f}"
        else:
            body = state
        console.print(f"{roll}{stream} {_tape_slug(slug):<14} {body}")
    rtds = _rtds_line(reply)
    if rtds:
        console.print(rtds)
    if reply.get("pending_rolls"):
        console.print(f"[dim]pending rolls: {', '.join(reply['pending_rolls'])}[/dim]")
    # Only when it's on: a line saying "no cap" every tick is noise.
    if reply.get("fleet_undecided_cap"):
        console.print(f"[dim]fleet un-decided cap: ${reply['fleet_undecided_cap']:,.0f}[/dim]")
