"""`pmt crypto ...` — up/down market pricing, the live trigger, and the
fleet's decision-tape tooling (scoreboard, shadow ledger, watch dashboard).

Split out of cli.py for size; registered onto the top-level `cli` group by
cli.py (`from cli_crypto import crypto_group; cli.add_command(crypto_group)`).
Shares `console`/`_api` with cli.py via cli_common so there's exactly one
Rich console and one lazy PolymarketAPI loader across both files.

Commands, fetching, and grading live here; every render function the watch
dashboard and the tape/stats printers use lives in watch_ui.py.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import click
from rich.table import Table

import stats_render
from engine import post as _engine_post

from cli_common import console, _api, _pnl_color
from polymarket import effectiveness, tape, updown_slugs, wallet
from watch_ui import (
    _SB_EMPTY, _brake_rich, _cbreak_stdin, _controls_panel, _eff_table,
    _restore_stdin, _rtds_rich, _safety_rich, _tape_render, _tape_slug, _wait_key,
    build_arms_table, build_header_panel, build_risk_header, build_windows_strip,
)
import watch_ui


@click.group("crypto")
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
               pay_up: float, p_cap: float, feed: str) -> None:
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
    }
    if side:
        payload["side_filter"] = side
    reply = _engine_post("/strategies/updown/command", payload)
    rolling = " · rolling" if roll else ""
    console.print(f"[green]armed[/green] {reply.get('armed')}  "
                  f"[dim]{r['kind']} · {r['rem_s']:.0f}s left · size ${size:.0f} · "
                  f"min edge {min_edge * 100:.0f}¢ · σ {r['sigma_bp_per_min']:.2f}bp/min · "
                  f"guard {guard_bp:.1f}bp · feed {feed}{rolling}[/dim]")
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


@crypto_group.command("fleet")
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


@crypto_group.command("trigger")
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
    rtds = _rtds_rich(reply.get("rtds") or {})
    if rtds:
        console.print(rtds)
    if reply.get("pending_rolls"):
        console.print(f"[dim]pending rolls: {', '.join(reply['pending_rolls'])}[/dim]")
    # Only when it's on: a line saying "no cap" every tick is noise.
    if reply.get("fleet_undecided_cap"):
        console.print(f"[dim]fleet un-decided cap: ${reply['fleet_undecided_cap']:,.0f}[/dim]")


@crypto_group.command("tape")
@click.option("-n", default=20, show_default=True)
@click.option("-f", "--follow", is_flag=True, help="Stream the decision tape live")
@click.option("--json", "as_json", is_flag=True, help="Raw JSONL records")
def crypto_tape(n: int, follow: bool, as_json: bool) -> None:
    """The strategy's decision tape: every fire, exit, eval, and gate."""
    import subprocess

    path = tape.UPDOWN_TAPE
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


_GAMMA_CACHE: dict[str, tuple[float, dict]] = {}
_GAMMA_TTL_S = 120  # watch's scoreboard refreshes every 10s; a slug's resolution doesn't flip that often


def _gamma_resolution_cached(slug: str) -> dict | None:
    """outcomes.gamma_resolution(), cached ~120s per slug so the watch
    dashboard's 10s scoreboard refresh doesn't hammer gamma. None on any
    fetch/parse failure — callers must degrade gracefully, never guess."""
    import time as _t

    import requests

    from polymarket import hosts, outcomes

    now = _t.time()
    hit = _GAMMA_CACHE.get(slug)
    if hit and now - hit[0] < _GAMMA_TTL_S:
        return hit[1]
    try:
        r = requests.get(f"{hosts.GAMMA}/markets", params={"slug": slug},
                          headers=hosts.UA, timeout=8)
        r.raise_for_status()
        result = outcomes.gamma_resolution(r.json())
    except Exception:
        return None
    _GAMMA_CACHE[slug] = (now, result)
    return result


def _funder_or_usage_error() -> str:
    """wallet.funder_address(), re-raised as the click.UsageError every
    command-level caller already showed for a missing PM_FUNDER_ADDRESS."""
    try:
        return wallet.funder_address()
    except ValueError as e:
        raise click.UsageError(str(e))


def _impute_win_pnl(buy_usd: float, sell_usd: float, buy_shares: float) -> float:
    """A gamma-confirmed WIN whose real $1/share redeem row hasn't posted yet
    (Polymarket's auto-redeemer can lag minutes): impute the payout as
    shares*$1 — what it will actually pay — so the $ figure tracks the W/L
    figure instead of showing a fake loss until the real redeem row lands
    and naturally replaces this estimate on a later scan.
    """
    return buy_shares * 1.0 + sell_usd - buy_usd


def _tape_scoreboard(floor: float, sliding_floor: float | None = None) -> dict:
    """Fetch the wallet's activity and grade it — the synchronous one-shot
    form used by `pmt crypto stats` and by anything that wants a fresh full
    walk. The watch dashboard instead keeps a wallet.ActivityLedger warm and
    calls score_activity() directly on its accumulated rows; both go through
    the SAME aggregation below, so the two never drift.
    """
    # Ground truth: every updown trade + redemption on the proxy wallet.
    # funder_address() RAISES on an unset addr, like every sibling command —
    # never fall through to a clean-looking "0W-0L" (docs/LESSONS.md#L26).
    addr = wallet.funder_address()
    return score_activity(wallet.fetch_wallet_activity(addr, floor), floor,
                          sliding_floor=sliding_floor)


def score_activity(rows: list[dict], floor: float,
                   sliding_floor: float | None = None) -> dict:
    """W-L / realized P&L graded by the WALLET (data-api activity), not the
    model's own final read — a model that's confidently wrong (XRP basis,
    2026-08-23) would otherwise grade its own loss as a win. The tape only
    contributes fire records (stated fairs) for the calibration table.

    Pure aggregation over already-fetched activity `rows` (plus the local
    tape file and the TTL-cached gamma cross-check) — no wallet pagination,
    so a dashboard refresh costs CPU over the in-memory ledger instead of a
    full re-walk of the history.

    The floor selects WINDOWS (slug start epoch >= floor), never individual
    transactions — filtering by row timestamp let a window's redeem into the
    range while its buys fell outside, printing phantom profit (caught live
    2026-08-23: +$78 shown vs -$17 true).

    `sliding_floor`, if given, additionally derives a "sliding" aggregate
    (recent-window W-L/P&L, keyed "sliding" in the result) from windows with
    start >= sliding_floor — computed in the SAME pass over the SAME rows
    (typically called with floor=0, i.e. all-time, from the watch
    dashboard) so a side-by-side sliding/all-time P&L costs one walk
    instead of two.
    """
    import time as _t

    from polymarket import outcomes

    now = _t.time()
    win_by_slug: dict[str, dict] = {}
    for a in rows:
        slug = a.get("slug") or ""
        if not updown_slugs.is_updown(slug) or updown_slugs.window_start(slug) < floor:
            continue
        w = win_by_slug.setdefault(slug, {"buy": 0.0, "sell": 0.0, "redeem": 0.0,
                                          "redeem_seen": False, "won": None,
                                          "buy_shares": 0.0,
                                          "buy_ts_usd": 0.0, "exit_ts": 0.0})
        usd = a.get("usdcSize") or 0.0
        ts = float(a.get("timestamp") or 0.0)
        if a["type"] == "TRADE":
            w["buy" if a.get("side") == "BUY" else "sell"] += usd
            if a.get("side") == "BUY":
                w["buy_shares"] += a.get("size") or 0.0
                # Exposure-time accumulators (polymarket.effectiveness): the
                # average dollar's entry, and when the capital came back.
                w["buy_ts_usd"] += usd * ts
        elif a["type"] == "REDEEM":
            w["redeem"] += usd
            w["redeem_seen"] = True
            w["exit_ts"] = max(w["exit_ts"], ts)
            if usd > 0.5:
                w["won"] = (a.get("outcome") or "").lower()

    fires: dict[str, list] = {}
    rolls = rolls_sliding = 0
    for r in tape.iter_records(tape.UPDOWN_TAPE, evs={tape.EV_FIRE, tape.EV_ROLL}):
        if r.get("ev") == tape.EV_FIRE:
            if updown_slugs.window_start(r.get("slug", "")) >= floor:
                fires.setdefault(r["slug"], []).append(r)
        elif r.get("t", 0) >= floor:
            rolls += 1
            if sliding_floor is not None and r.get("t", 0) >= sliding_floor:
                rolls_sliding += 1

    series: dict[str, dict] = {}
    cal: dict[float, list] = {}
    window_list: list[dict] = []
    wins = losses = estimated = 0
    riding_n = 0
    net = riding_usd = 0.0
    wins_s = losses_s = estimated_s = 0
    net_s = 0.0
    for slug, w in win_by_slug.items():
        parsed = updown_slugs.parse(slug)
        if parsed is None:
            continue  # not a real updown slug (defensive; upstream already filtered)
        sym, _dur_s, start, end, series_k = parsed
        if w["buy"] + w["sell"] + w["redeem"] < 1:
            continue
        in_sliding = sliding_floor is not None and start >= sliding_floor
        s = series.setdefault(series_k,
                               {"w": 0, "l": 0, "open": 0, "pnl": 0.0, "usd": 0.0, "est": 0})
        s["usd"] += w["buy"]
        fired = fires.get(slug, [{}])[0].get("side")
        # Redemption is silent (no row at all) or slow far more often than a
        # loss actually is — a gamma round-trip is only worth it once the
        # grace window has passed with no redeem of either kind.
        gamma = (_gamma_resolution_cached(slug)
                 if w["redeem"] <= 0.5 and not w["redeem_seen"] and now >= end + 300
                 else None)
        won, is_est = outcomes.grade_window(w["redeem"], w["redeem_seen"], fired, gamma, now, end)
        if won is None:
            s["open"] += 1
            # Still riding — its bought notional is speculative exposure the
            # risk header's "riding N windows $W" needs, distinct from a live
            # arm's committed budget (this window may have already rolled off).
            riding_n += 1
            riding_usd += w["buy"]
            continue
        pnl_est = is_est
        if won and w["redeem"] <= 0.5 and not w["redeem_seen"]:
            # Gamma confirmed the win before Polymarket's redeemer posted the
            # real payout row — impute it so the $ figure doesn't lag the W/L
            # figure by however long the slow auto-redeem takes.
            pnl = _impute_win_pnl(w["buy"], w["sell"], w["buy_shares"])
            pnl_est = True
        else:
            pnl = w["redeem"] + w["sell"] - w["buy"]
        s["w" if won else "l"] += 1
        s["pnl"] += pnl
        s["est"] += pnl_est
        wins, losses, net = wins + won, losses + (not won), net + pnl
        estimated += pnl_est
        if in_sliding:
            wins_s, losses_s, net_s = wins_s + won, losses_s + (not won), net_s + pnl
            estimated_s += pnl_est
        window_list.append({"slug": slug, "won": won, "pnl": pnl,
                             "est": bool(pnl_est), "end_ts": end,
                             "notional": w["buy"],
                             "entry_ts": effectiveness.weighted_ts(w["buy_ts_usd"], w["buy"]),
                             "exit_ts": w["exit_ts"]})
        # Winning outcome for calibration: the paying redeem row names it
        # directly; else gamma's own read if we cross-checked one; else
        # infer from our fired side (right if we won, flipped if we lost).
        if w["won"]:
            won_side = w["won"]
        elif gamma and gamma.get("winner"):
            won_side = gamma["winner"]
        elif fired:
            won_side = fired if won else ("down" if fired == "up" else "up")
        else:
            won_side = ""
        for f in fires.get(slug, []):
            b = min(int(f["fair"] * 20) / 20, 0.95)
            cal.setdefault(b, [0, 0])
            cal[b][0] += 1
            cal[b][1] += f["side"] == won_side
    # Recent-windows strip wants newest-first, capped small — this is a
    # display list, not the ledger (pmt crypto window/outcomes for the rest).
    windows = sorted(window_list, key=lambda r: r["end_ts"], reverse=True)[:12]
    result = {"wins": wins, "losses": losses, "net": net, "rolls": rolls,
              "series": series, "cal": cal, "estimated": estimated,
              "riding_n": riding_n, "riding_usd": riding_usd, "windows": windows,
              # Every graded window (uncapped, unsorted) with its notional and
              # exposure timing — the input to polymarket.effectiveness. Kept
              # separate from `windows`, which is a 12-row display strip.
              "eff_windows": window_list}
    if sliding_floor is not None:
        result["sliding"] = {"wins": wins_s, "losses": losses_s, "net": net_s,
                              "rolls": rolls_sliding, "estimated": estimated_s}
    return result


def effectiveness_summary(sb: dict, bal: dict | None) -> dict:
    """polymarket.effectiveness.summary() over a scoreboard's graded windows.

    The bankroll denominator is cash PLUS notional still riding: the CLOB's
    balance only reports free USDC, so mid-flight capital would otherwise
    vanish from the book's size and flatter every per-bankroll rate. Falls
    back to None (metrics that need a bankroll come back None) when the
    balance call failed — never to a guess.

    The watch header can call this on its own snapshot and render
    effectiveness.header_line() from the result.
    """
    cash = float((bal or {}).get("total") or 0.0)
    bankroll = cash + float(sb.get("riding_usd") or 0.0)
    return effectiveness.summary(sb.get("eff_windows") or [],
                                  bankroll=bankroll or None, now=time.time())


def _eff_table(s: dict) -> Table:
    """The effectiveness block. Lives in stats_render now — kept here as the
    name every caller already imports, so there is exactly one implementation
    of the block rather than two that can drift."""
    return stats_render.effectiveness_table(s)


@crypto_group.command("stats")
@click.option("--since", type=float, default=None,
              help="Windows starting after this point: hours-ago if small, "
                   "raw unix epoch if large (default: all time — the full "
                   "ledger of record). NOTE an hours-ago floor SLIDES — pin "
                   "an epoch for any number you intend to compare across runs")
@click.option("--json", "as_json", is_flag=True)
def crypto_stats(since: float | None, as_json: bool) -> None:
    """Fleet scoreboard: realized P&L (wallet-graded), win rate, calibration, live arms, capital."""
    floor = _shadow_parse_since(since) if since else 0.0
    try:
        sb = _tape_scoreboard(floor)
    except Exception as e:
        console.print(f"[red]data-api unreachable: {e}[/red]")
        sys.exit(1)
    wins, losses, net, rolls = sb["wins"], sb["losses"], sb["net"], sb["rolls"]
    series, cal, estimated = sb["series"], sb["cal"], sb["estimated"]

    status, bal = {}, {}
    try:
        status = _engine_post("/strategies/updown/command", {"action": "status"})
    except (Exception, SystemExit):
        # engine.post() sys.exit()s on failure (SystemExit, not Exception) —
        # engine down shouldn't blank the rest of a one-shot report.
        pass
    try:
        bal = _api().get_usdc_balance()
    except Exception:
        pass

    eff_s = effectiveness_summary(sb, bal)

    if as_json:
        click.echo(json.dumps({
            "wins": wins, "losses": losses, "net_est": net, "rolls": rolls,
            "estimated": estimated, "series": series,
            "calibration": {str(k): v for k, v in cal.items()},
            "arms": status.get("arms", {}), "balance": bal,
            "windows": sb["windows"], "effectiveness": eff_s,
        }, indent=2))
        return

    console.print(stats_render.render_stats(sb, eff_s, bal, status, floor))


# ---------- watch: the render/fetch split ----------
#
# The dashboard runs two threads and nothing else:
#
#   main   — input + render, ZERO network. Polls the tty at 20Hz (the select
#            timeout IS the loop's pacing) and repaints at 1Hz, or instantly
#            when a key changed UI state.
#   worker — one daemon thread owning every network call, each on its own
#            cadence, publishing whole result objects into a WatchState.
#
# Why it is split at all: docs/LESSONS.md#L28.


# Fetch cadences — keep in sync with the line in _controls_panel().
ENGINE_EVERY_S = 2.0
SB_EVERY_S = 10.0
BAL_EVERY_S = 60.0
WORKER_INTERVAL_S = 0.25  # how often the worker checks what's due
# 'q' must feel instant. An idle worker exits its wait immediately; one stuck
# mid-fetch is simply abandoned (daemon thread, the process is leaving anyway)
# rather than holding the operator's terminal for the length of an HTTP call.
WORKER_JOIN_S = 0.25
KEY_POLL_S = 0.05         # 20Hz key polling — the perceived-latency budget
RENDER_EVERY_S = 1.0      # repaint cadence when no key changed anything


class WatchState:
    """The single hand-off point between the fetch thread and the render loop.

    The worker never mutates a published value in place — it builds a whole
    new result object and swaps it in — so a reader can never catch a
    half-built scoreboard. read() takes the lock and copies the mapping,
    handing the renderer one internally consistent snapshot per frame.
    """

    _FIELDS = ("status", "bal", "sb", "sb_stale", "sb_fetched_at", "err")

    def __init__(self, sb: dict | None = None) -> None:
        self._lock = threading.Lock()
        self._d: dict = {
            "status": {}, "bal": {},
            "sb": dict(_SB_EMPTY) if sb is None else sb,
            # Not stale, just not fetched yet: sb_fetched_at None already
            # renders as the header's "—" data-age, which is the honest cue
            # while the first walk is still in flight.
            "sb_stale": False, "sb_fetched_at": None, "err": None,
        }

    def update(self, **kw) -> None:
        unknown = set(kw) - set(self._FIELDS)
        if unknown:
            raise KeyError(f"unknown WatchState field(s): {sorted(unknown)}")
        with self._lock:
            self._d.update(kw)

    def read(self) -> dict:
        with self._lock:
            return dict(self._d)


class WatchFetcher:
    """Every network call the watch dashboard makes, on one daemon thread.

    Each source has its own cadence and its own belt: a failure keeps the
    last good value and surfaces as staleness in the header, never as a
    traceback and never as a dead dashboard. The wallet scoreboard runs off
    an ActivityLedger, so a steady-state refresh re-reads only the head of
    the activity feed instead of re-walking the whole history.
    """

    def __init__(self, state: WatchState, sliding_floor: float,
                 ledger: "wallet.ActivityLedger | None" = None) -> None:
        self.state = state
        self.sliding_floor = sliding_floor
        self.ledger = wallet.ActivityLedger() if ledger is None else ledger
        self._due: dict[str, float] = {"status": 0.0, "sb": 0.0, "bal": 0.0}

    # -- individual fetches: each may raise; tick() belts them --

    def fetch_status(self) -> None:
        # engine.post() prints its own red error before sys.exit()ing. Let it —
        # Live's alternate screen paints over it on the next frame. Do NOT hush
        # it with contextlib.redirect_stdout: docs/LESSONS.md#L29.
        status = _engine_post("/strategies/updown/command", {"action": "status"})
        self.state.update(status=status if isinstance(status, dict) else {})

    def fetch_sb(self) -> None:
        addr = wallet.funder_address()
        self.ledger.refresh(addr)
        # Always grade the FULL history (floor 0) and derive both the sliding
        # (recent-pulse) and all-time figures from that one pass.
        sb = score_activity(self.ledger.rows, 0.0, sliding_floor=self.sliding_floor)
        self.state.update(sb=sb, sb_stale=False, sb_fetched_at=time.time(), err=None)

    def fetch_bal(self) -> None:
        self.state.update(bal=_api().get_usdc_balance() or {})

    # -- failure handling: last good value + a visible marker --

    def _status_failed(self, exc: BaseException) -> None:
        # Deliberately NOT the last good arms: a stale committed-$ figure on a
        # trading dashboard is worse than the arms table's red "engine
        # unreachable or no arms", which is what an empty status renders as.
        self.state.update(status={})

    def _sb_failed(self, exc: BaseException) -> None:
        self.state.update(sb_stale=True, err=f"scoreboard: {type(exc).__name__}"[:100])

    def _bal_failed(self, exc: BaseException) -> None:
        pass  # keep the last capital figure; a flaky balance call shouldn't blank it

    def tick(self, now: float) -> None:
        """Run whatever is due at `now`. Never raises."""
        for name, every, fetch, failed in (
            ("status", ENGINE_EVERY_S, self.fetch_status, self._status_failed),
            ("sb", SB_EVERY_S, self.fetch_sb, self._sb_failed),
            ("bal", BAL_EVERY_S, self.fetch_bal, self._bal_failed),
        ):
            if now < self._due[name]:
                continue
            self._due[name] = now + every
            try:
                fetch()
            except (Exception, SystemExit) as e:
                # engine.post() sys.exit()s on failure — SystemExit isn't an
                # Exception, so it must be named explicitly.
                failed(e)

    def loop(self, stop: threading.Event, interval: float = WORKER_INTERVAL_S) -> None:
        """Thread body. stop.wait() returns the moment the flag is set, so
        quitting never waits out a full interval."""
        while not stop.is_set():
            try:
                self.tick(time.time())
            except Exception as e:  # a bug in tick() itself must not kill the feed
                self.state.update(err=f"{type(e).__name__}: {e}"[:100])
            stop.wait(interval)


@crypto_group.command("watch")
@click.option("--since", type=float, default=None,
              help="Sliding-window floor for the header's recent P&L: "
                   "hours-ago if small, raw unix epoch if large (default: "
                   "last 6h — a live dashboard cares about the recent pulse). "
                   "All-time P&L, and the riding/recent-windows figures, "
                   "always walk the full wallet history regardless of this.")
def crypto_watch(since: float | None) -> None:
    """Full-screen live dashboard: risk header + scoreboard + arms + streaming tape."""
    import time as _t
    from collections import deque
    from datetime import datetime, timezone

    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    WATCH_DEFAULT_LOOKBACK_H = 6.0  # sliding recent window — it's a live dashboard, not the ledger

    collapser = watch_ui.TapeCollapser()

    floor = _shadow_parse_since(since) if since else (_t.time() - WATCH_DEFAULT_LOOKBACK_H * 3600)
    floor_label = ("all time" if floor <= 0 else
                   datetime.fromtimestamp(floor, tz=timezone.utc).strftime("since %m-%d %H:%MZ"))
    lines: deque = deque(maxlen=200)
    offset = 0
    try:
        with open(tape.UPDOWN_TAPE) as fh:
            for raw in fh.readlines()[-120:]:
                collapser.add(raw, lines)
            offset = fh.tell()
    except OSError:
        pass

    # All network lives on the worker; the loop below only ever reads this
    # snapshot, so the first paint is immediate (data age "—" until the first
    # wallet walk lands). See docs/LESSONS.md#L28.
    state = WatchState()
    stop = threading.Event()
    fetcher = WatchFetcher(state, floor)
    worker = threading.Thread(target=fetcher.loop, args=(stop,),
                              name="pmt-watch-fetch", daemon=True)
    snap = state.read()
    render_err: str | None = None

    def header() -> Panel:
        return build_header_panel(snap, floor_label, render_err)

    def risk_panel() -> Text:
        return Text.from_markup(build_risk_header(snap["status"], snap["bal"], snap["sb"]))

    def arms_table() -> Table:
        return build_arms_table(snap["status"].get("arms"), _t.time())

    def strip_panel() -> Panel:
        return Panel(Text.from_markup(build_windows_strip(snap["sb"].get("windows"))),
                     title="recent windows", subtitle="[dim]h = controls[/dim]",
                     border_style="dim")

    def tape_panel(height: int) -> Panel:
        shown = list(lines)[-max(height - 2, 1):]
        return Panel(Text.from_ansi("\n".join(shown)), title="tape", border_style="dim")

    layout = Layout()
    layout.split_column(
        Layout(name="head", size=3),
        Layout(name="risk", size=1),
        Layout(name="arms", size=10),
        Layout(name="strip", size=3),
        Layout(name="tape", ratio=1),
    )

    show_controls = False
    next_render = 0.0
    saved_term = _cbreak_stdin()
    worker.start()
    try:
        with Live(layout, refresh_per_second=4, screen=True) as live:
            while True:
                # The ONLY wait in this loop, and it's the key wait: 20Hz, so
                # 'q'/'h' are seen within ~50ms no matter what the worker is
                # doing. Nothing below this line touches the network.
                key = _wait_key(KEY_POLL_S)
                if key == "q":
                    break
                dirty = False
                if key == "h":
                    show_controls = not show_controls
                    dirty = True  # a toggle repaints now, not on the next second
                now = time.time()
                if not dirty and now < next_render:
                    continue
                next_render = now + RENDER_EVERY_S
                snap = state.read()
                # Final belt: neither the tape file nor a render bug may tear
                # the dashboard down. Note it in the header and keep painting;
                # only Ctrl+C or 'q' stops this.
                try:
                    # Local file, seek+read from the last offset: sub-
                    # millisecond, so the render never waits on it. Belted
                    # because a torn mid-write line can be undecodable bytes,
                    # which is a UnicodeDecodeError, not an OSError.
                    try:
                        with open(tape.UPDOWN_TAPE) as fh:
                            fh.seek(offset)
                            for raw in fh:
                                collapser.add(raw, lines)
                            offset = fh.tell()
                    except OSError:
                        pass
                    layout["arms"].size = max(len(snap["status"].get("arms") or {}), 1) + 4
                    layout["head"].update(header())
                    layout["risk"].update(risk_panel())
                    layout["arms"].update(arms_table())
                    layout["strip"].update(
                        _controls_panel() if show_controls else strip_panel())
                    h = live.console.size.height - 3 - 1 - 3 - layout["arms"].size
                    layout["tape"].update(tape_panel(h))
                    render_err = None
                except KeyboardInterrupt:
                    raise
                except (Exception, SystemExit) as e:
                    render_err = f"{type(e).__name__}: {e}"[:100]
                    try:
                        layout["head"].update(header())
                    except Exception:
                        pass
                live.refresh()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        worker.join(timeout=WORKER_JOIN_S)  # daemon thread — never hang the exit
        _restore_stdin(saved_term)


@crypto_group.command("activity")
@click.option("--limit", "n", default=40, show_default=True, help="Rows to show")
@click.option("--all", "show_all", is_flag=True,
              help="Every activity type, not just updown windows")
def crypto_activity(n: int, show_all: bool) -> None:
    """Recent wallet activity — the curl+jq boilerplate, built in."""
    import time as _t

    addr = _funder_or_usage_error()

    rows: list[dict] = []
    offset = 0
    while True:
        try:
            page = wallet.fetch_activity_page(addr, offset)
        except Exception as e:
            console.print(f"[red]data-api unreachable: {e}[/red]")
            sys.exit(1)
        rows.extend(page if show_all else
                    (a for a in page if updown_slugs.is_updown(a.get("slug") or "")))
        if len(page) < wallet.PAGE_SIZE or len(rows) >= n:
            break
        offset += wallet.PAGE_SIZE
    rows = rows[:n]

    if not rows:
        console.print("[dim]No activity.[/dim]")
        return

    t = Table(title=f"wallet activity{'' if show_all else ' (updown)'}")
    for col in ("time", "type", "$", "size", "price", "outcome", "window"):
        t.add_column(col, justify="right" if col in ("time", "$", "size", "price") else "left")
    for a in rows:
        ts = _t.strftime("%H:%M:%S", _t.localtime(a.get("timestamp", 0)))
        typ, side = a.get("type", ""), a.get("side", "")
        usd = a.get("usdcSize") or 0.0
        if typ == "TRADE":
            color = "green" if side == "BUY" else "yellow"
            label = f"[{color} bold]{side or typ}[/]"
        elif typ == "REDEEM":
            # A $0 redeem means the held side lost — that's the loss signal,
            # not a sale, so it needs its own color rather than reusing SELL's.
            label = f"[{'cyan' if usd > 0 else 'red'} bold]REDEEM[/]"
        else:
            label = typ or "?"
        slug = a.get("slug") or ""
        window = _tape_slug(slug) if updown_slugs.is_updown(slug) else slug
        t.add_row(ts, label, f"${usd:,.2f}", f"{a.get('size', 0):g}",
                  f"{a.get('price', 0):.3f}", a.get("outcome", ""), window[:40])
    console.print(t)


@crypto_group.command("window")
@click.argument("slug")
def crypto_window(slug: str) -> None:
    """Post-mortem for one updown window: wallet trades + tape, merged by time."""
    import time as _t

    parsed = updown_slugs.parse_updown_slug(slug)
    if parsed is None:
        raise click.UsageError(f"not an updown slug: {slug!r} (want e.g. btc-updown-15m-1787449500)")
    start, end = parsed["start"], parsed["end"]
    dur = f"{parsed['dur_s'] // 60}m"

    addr = _funder_or_usage_error()

    try:
        rows = [a for a in wallet.fetch_wallet_activity(addr, start) if a.get("slug") == slug]
    except Exception as e:
        console.print(f"[red]data-api unreachable: {e}[/red]")
        sys.exit(1)

    buy = sell = redeem = 0.0
    win_outcome: str | None = None
    lost = False
    events: list[tuple[float, str]] = []
    for a in rows:
        usd = a.get("usdcSize") or 0.0
        typ, side = a.get("type", ""), a.get("side", "")
        if typ == "TRADE":
            if side == "BUY":
                buy += usd
            else:
                sell += usd
            color = "green" if side == "BUY" else "yellow"
            label = click.style(f"{side:<5}", fg=color, bold=True)
        elif typ == "REDEEM":
            redeem += usd
            if usd > 0.5:
                win_outcome = a.get("outcome")
            else:
                lost = True
            label = click.style("REDEEM", fg="cyan" if usd > 0.5 else "red", bold=True)
        else:
            label = typ or "?"
        ts = a.get("timestamp", 0)
        line = (f"{_t.strftime('%H:%M:%S', _t.localtime(ts))}  {label}  "
                f"{a.get('size', 0):g}sh @ {a.get('price', 0):.3f}  "
                f"${usd:,.2f}  {a.get('outcome', '')}")
        events.append((ts, line))

    for r in tape.iter_records(tape.UPDOWN_TAPE):
        if r.get("slug") != slug:
            continue
        try:
            rendered = _tape_render(json.dumps(r))
        except Exception:
            continue
        if rendered:
            events.append((r.get("t", 0), rendered))

    net = redeem + sell - buy
    now = _t.time()
    if win_outcome:
        outcome_label = f"[bold]{win_outcome}[/bold]"
    elif lost:
        outcome_label = "[bold red]LOSS[/bold red]"
    elif now < end + 300:
        outcome_label = "[dim]pending[/dim]"
    else:
        # No redeem row at all past the grace window — Polymarket doesn't
        # reliably auto-redeem a slow WIN, so silence isn't a loss; ask gamma.
        from polymarket import outcomes

        bought = next(((a.get("outcome") or "").lower() for a in rows
                       if a.get("type") == "TRADE" and a.get("side") == "BUY"), None)
        gamma = _gamma_resolution_cached(slug)
        won, is_est = outcomes.grade_window(redeem, False, bought, gamma, now, end)
        if won is None:
            outcome_label = "[yellow]riding[/yellow]" if gamma is not None else "[dim]?[/dim]"
        elif is_est:
            outcome_label = "[dim]~LOSS[/dim]"  # gamma unreachable — old assume-LOSS heuristic
        else:
            outcome_label = "[bold]WIN[/bold]" if won else "[bold red]LOSS[/bold red]"

    fmt = "%H:%M:%S"
    console.print(f"[bold]{slug}[/bold]  {_t.strftime(fmt, _t.localtime(start))}"
                  f"–{_t.strftime(fmt, _t.localtime(end))} ({dur})")
    console.print(
        f"  bought ${buy:,.2f} · sold ${sell:,.2f} · redeemed ${redeem:,.2f} · "
        f"P&L [{_pnl_color(net)}]{net:+,.2f}[/] · outcome {outcome_label}"
    )
    if not events:
        console.print("[dim]No activity or tape for this window.[/dim]")
        return
    console.print()
    for _, line in sorted(events, key=lambda x: x[0]):
        click.echo(line)


_ORACLE_SYMBOLS = ["btc", "eth", "sol", "xrp", "doge", "bnb", "all"]  # keep in sync with chainlink.SYMBOLS


@crypto_group.command("oracle")
@click.option("--symbol", type=click.Choice(_ORACLE_SYMBOLS), default="all", show_default=True)
@click.option("--hours", type=float, default=24.0, show_default=True, help="History window to fetch")
def crypto_oracle(symbol: str, hours: float) -> None:
    """Fetch Chainlink Polygon oracle rounds, append new ones to the corpus.

    Ground truth for the Chainlink-vs-Binance basis (`pmt crypto basis`) —
    corpus lives at ~/.pmt/corpus/chainlink-{symbol}.jsonl, append-only.
    """
    from polymarket.chainlink import SYMBOLS, corpus_path, fetch_rounds, append_corpus

    symbols = SYMBOLS if symbol == "all" else [symbol]
    for sym in symbols:
        try:
            rounds = fetch_rounds(sym, hours=hours)
        except Exception as e:
            console.print(f"[red]{sym.upper():5s} RPC error — {e}[/red]")
            continue
        new_n = append_corpus(sym, rounds)
        if rounds:
            span_h = (rounds[-1]["updated_at"] - rounds[0]["updated_at"]) / 3600
            span = f"{span_h:.1f}h span"
        else:
            span = "no rounds"
        console.print(f"{sym.upper():5s} fetched {len(rounds):4d} · new {new_n:4d} · "
                      f"{span} · {corpus_path(sym)}")


def _print_aligned_basis(symbol: str, hours: float, no_fetch: bool) -> None:
    """TWAP-vs-TWAP aligned basis (ROADMAP.md R1) — per-minute + settlement-shaped,
    the report that measures the error which actually decides wins/losses at the wire.
    """
    from polymarket.chainlink import ALIGNED_FETCH_BUFFER_H, SYMBOLS, aligned_basis_report, extend_all

    symbols = SYMBOLS if symbol == "all" else [symbol]

    if not no_fetch:
        console.print(f"[dim]extending corpus to >= {hours:g}h for {', '.join(s.upper() for s in symbols)} ...[/dim]")
        for sym, r in extend_all(hours + ALIGNED_FETCH_BUFFER_H, symbols).items():
            if r["top_up_error"]:
                console.print(f"[red]  {sym:5s} top-up failed: {r['top_up_error']}[/red]")
            if r["backfill_error"]:
                console.print(f"[red]  {sym:5s} backfill failed: {r['backfill_error']}[/red]")
            console.print(f"  {sym:5s} +{r['topped']:<4d} recent  +{r['backfilled']:<5d} backfilled")
        console.print()

    for sym in symbols:
        report = aligned_basis_report(sym, hours=hours)
        if not report["per_minute"]:
            console.print(f"[bold]{sym.upper()}/USD[/bold]  [dim]no corpus data — "
                           f"run: pmt crypto oracle --symbol {sym}[/dim]\n")
            continue

        t = Table(title=f"{sym.upper()}/USD aligned basis — last {hours:g}h "
                        f"({report['n_rounds']} rounds, {report['span_h']:.1f}h span)")
        t.add_column("variant", justify="left")
        for col in ("n", "mean", "std", "p50", "p90", "p95", "p99", "max"):
            t.add_column(col, justify="right")
        for label, s in (("per-minute", report["per_minute"]),
                          ("settlement-5m", report["settlement_5m"]),
                          ("settlement-15m", report["settlement_15m"])):
            if s is None:
                t.add_row(label, *(["—"] * 8))
            else:
                t.add_row(label, str(s["n"]), f"{s['mean']:.2f}", f"{s['std']:.2f}", f"{s['p50']:.2f}",
                          f"{s['p90']:.2f}", f"{s['p95']:.2f}", f"{s['p99']:.2f}", f"{s['max']:.2f}")
        console.print(t)
        console.print()


@crypto_group.command("basis")
@click.option("--symbol", type=click.Choice(_ORACLE_SYMBOLS), default="all", show_default=True)
@click.option("--hours", type=float, default=24.0, show_default=True, help="Corpus window to analyze")
@click.option("--aligned", is_flag=True,
              help="TWAP-vs-TWAP aligned basis (per-minute + settlement-shaped) instead of point-in-time")
@click.option("--no-fetch", is_flag=True, help="--aligned only: skip corpus extension, use what's on disk")
def crypto_basis(symbol: str, hours: float, aligned: bool, no_fetch: bool) -> None:
    """Chainlink-vs-Binance basis distribution — the R1 decision input for per-symbol guards.

    Joins stored Chainlink rounds (`pmt crypto oracle`) against Binance 1m
    closes and reports basis_bp = (chainlink/binance - 1) * 1e4 per round.
    --aligned switches to the TWAP-vs-TWAP method (ROADMAP.md R1), which
    strips out the point-in-time method's up-to-60s timing noise.
    """
    if aligned:
        _print_aligned_basis(symbol, hours, no_fetch)
        return

    from polymarket.chainlink import SYMBOLS, GUARD_BP, basis_report

    symbols = SYMBOLS if symbol == "all" else [symbol]
    for sym in symbols:
        try:
            report = basis_report(sym, hours=hours)
        except Exception as e:
            console.print(f"[red]{sym.upper()}: {e}[/red]\n")
            continue
        stats = report["stats"]
        if not stats:
            console.print(f"[bold]{sym.upper()}/USD[/bold]  [dim]no corpus data — "
                           f"run: pmt crypto oracle --symbol {sym}[/dim]\n")
            continue

        t = Table(title=f"{sym.upper()}/USD basis — last {hours:g}h")
        for col in ("n", "mean bp", "std bp", "p5 bp", "p50 bp", "p95 bp", "max|bp|"):
            t.add_column(col, justify="right")
        t.add_row(str(stats["n"]), f"{stats['mean']:+.2f}", f"{stats['std']:.2f}",
                  f"{stats['p5']:+.2f}", f"{stats['p50']:+.2f}", f"{stats['p95']:+.2f}",
                  f"{stats['max_abs']:.2f}")
        console.print(t)

        guard = GUARD_BP.get(sym)
        p95abs = stats["p95_abs"]
        if guard is None:
            console.print(f"[dim]no live guard set (arm disabled) — "
                           f"p95 |basis| {p95abs:.1f}bp is the re-entry gate[/dim]\n")
        elif guard >= p95abs:
            console.print(f"[green]guard {guard:.1f}bp covers p95 |basis| {p95abs:.1f}bp ✓[/green]\n")
        else:
            console.print(f"[red]guard {guard:.1f}bp TOO TIGHT — p95 |basis| {p95abs:.1f}bp[/red]\n")


@crypto_group.command("outcomes")
@click.option("--since", type=float, default=0.0, show_default=True,
              help="Epoch: only windows starting at/after this time")
@click.option("--out", "out_path", type=str, default=None,
              help="Outcomes file to append/update (default: ~/.pmt/corpus/outcomes.jsonl)")
def crypto_outcomes(since: float, out_path: str | None) -> None:
    """Build the validated outcomes file the replay harness needs (JSONL: slug/winner/source).

    Strict priority: wallet redemption (we traded it, Polymarket already
    settled and paid) beats Chainlink corpus inference (windows we never
    touched) — and a Chainlink read is refused outright if the corpus can't
    prove it was fresh enough at settlement time. See polymarket.outcomes
    for why that guard exists; it's the fix for a real mislabeling incident.
    """
    import time as _t
    from pathlib import Path

    from polymarket import chainlink as ck
    from polymarket.outcomes import (
        OUTCOMES_PATH, build_outcomes, extract_updown_slugs, load_outcomes,
        merge_outcomes, wallet_outcomes, window_universe, write_outcomes,
    )

    addr = _funder_or_usage_error()
    out_file = Path(out_path) if out_path else OUTCOMES_PATH

    now = _t.time()
    slugs: set[str] = set()
    for path in (tape.BOOK_TAPE, tape.UPDOWN_TAPE):
        try:
            with open(path) as fh:
                slugs |= extract_updown_slugs(fh)
        except FileNotFoundError:
            continue
    windows = window_universe(slugs, since, now)
    if not windows:
        console.print("[dim]No closed updown windows in range.[/dim]")
        return

    try:
        activity = wallet.fetch_wallet_activity(addr, windows[0]["start"])
    except Exception as e:
        console.print(f"[red]data-api unreachable: {e}[/red]")
        sys.exit(1)
    wallet_wins = wallet_outcomes(activity)

    symbols = {w["symbol"] for w in windows}
    rounds_by_symbol = {sym: ck.load_corpus(sym) for sym in symbols}

    # terminal-book fallback source: only samples near each window's end matter,
    # so keep just the tail of each slug's book records while streaming the tape
    from polymarket.outcomes import BOOK_TERMINAL_S
    end_by_slug = {w["slug"]: w["end"] for w in windows}
    book_by_slug: dict[str, list[dict]] = {}
    for r in tape.iter_records(tape.BOOK_TAPE, floor=windows[0]["start"]):
        end = end_by_slug.get(r.get("slug") or "")
        if end is not None and r.get("t", 0) >= end - BOOK_TERMINAL_S:
            book_by_slug.setdefault(r["slug"], []).append(r)

    rows, dropped = build_outcomes(windows, wallet_wins, rounds_by_symbol, book_by_slug)
    existing = load_outcomes(out_file)
    merged, added, upgraded = merge_outcomes(existing, rows)
    write_outcomes(merged, out_file)

    n_wallet = sum(1 for r in rows if r["source"] == "wallet")
    n_chain = sum(1 for r in rows if r["source"] == "chainlink")
    n_book = sum(1 for r in rows if r["source"] == "book")
    n_up = sum(1 for r in rows if r["winner"] == "up")
    n_down = sum(1 for r in rows if r["winner"] == "down")

    t = Table(title=f"outcomes — {len(windows)} windows evaluated")
    t.add_column("source", justify="left")
    t.add_column("n", justify="right")
    t.add_row("wallet", str(n_wallet))
    t.add_row("chainlink", str(n_chain))
    t.add_row("book (terminal)", str(n_book))
    t.add_row("dropped (stale)", str(len(dropped)))
    console.print(t)
    console.print(f"[dim]{added} new · {upgraded} upgraded chainlink→wallet · "
                  f"winner split {n_up} up / {n_down} down[/dim]")
    console.print(f"[dim]{out_file}  ({len(merged)} total rows)[/dim]")


def _shadow_parse_since(v: float | None) -> float:
    """HOURS_AGO_OR_EPOCH: small values are hours-ago, big ones (a real Unix
    timestamp is always > 1e6 in hours-ago terms) are a raw epoch already."""
    import time as _t

    if v is None:
        return 0.0
    if v > 1_000_000:
        return v
    return _t.time() - v * 3600


@crypto_group.command("shadow")
@click.option("--since", type=float, default=None,
              help="Hours-ago, or a raw epoch if the value is large (default: all tape)")
@click.option("--json", "as_json", is_flag=True, help="Raw report JSON")
def crypto_shadow(since: float | None, as_json: bool) -> None:
    """Shadow P&L ledger: what our own gates cost/saved us, per refusal reason.

    Every refused side on the decision tape — basis-guard gates, the
    safety/latched/distrust/avg_down brakes, and unbraked sides that just
    missed min_fair/min_edge — plus every unfilled remainder of a real fire,
    becomes a hindsight-priced counterfactual clip: net shadow P&L = missed
    wins MINUS avoided losses, so a gate that dodges one big loss can still
    net-positive even after refusing several winners — always reported both
    ways, never just the missed-wins half.

    Reuses polymarket.outcomes' wallet-first / Chainlink-fallback resolver
    (and refreshes the ~/.pmt/corpus/outcomes.jsonl corpus in-process, same
    as `pmt crypto outcomes`) — a window's winner is never guessed, so an
    unresolved window's episodes surface as an honest coverage gap instead
    of a silent zero.
    """
    import time as _t

    from polymarket import chainlink as ck
    from polymarket import outcomes, shadow

    addr = _funder_or_usage_error()

    since_epoch = _shadow_parse_since(since)
    now = _t.time()

    try:
        with open(tape.UPDOWN_TAPE) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []

    slugs = outcomes.extract_updown_slugs(lines)
    windows = outcomes.window_universe(slugs, since_epoch, now)

    floor = windows[0]["start"] if windows else since_epoch
    try:
        activity = wallet.fetch_wallet_activity(addr, floor)
    except Exception as e:
        console.print(f"[red]data-api unreachable: {e}[/red]")
        sys.exit(1)

    wallet_wins = outcomes.wallet_outcomes(activity)
    symbols = {w["symbol"] for w in windows}
    rounds_by_symbol = {sym: ck.load_corpus(sym) for sym in symbols}
    rows, _dropped = outcomes.build_outcomes(windows, wallet_wins, rounds_by_symbol)

    existing = outcomes.load_outcomes()
    merged, _added, _upgraded = outcomes.merge_outcomes(existing, rows)
    outcomes.write_outcomes(merged)
    winners = {slug: row["winner"] for slug, row in merged.items()}

    report = shadow.build_report(lines, winners, activity, since=since_epoch)

    if as_json:
        console.print_json(json.dumps(report))
        return

    categories, totals, coverage = report["categories"], report["totals"], report["coverage"]

    if totals["episodes"] == 0:
        console.print("[dim]No refusals on the tape in range.[/dim]")
        return

    console.print(f"[bold]shadow P&L[/bold] — {totals['episodes']} episodes across "
                  f"{coverage['windows']} windows")

    t = Table(title="by refusal reason (hindsight-priced)")
    cols = [("category", "left"), ("episodes", "right"), ("priced", "right"),
            ("hit rate", "right"), ("missed wins", "right"), ("avoided losses", "right"),
            ("net", "right"), ("verdict", "left")]
    for name, justify in cols:
        t.add_column(name, justify=justify)
    for cat in shadow.CATEGORY_ORDER:
        s = categories.get(cat)
        if not s or s["episodes"] == 0:
            continue
        hr = f"{s['hit_rate'] * 100:.0f}%" if s["hit_rate"] is not None else "—"
        net = s["net"]
        net_style = "red" if net > 0 else "green"
        t.add_row(cat, str(s["episodes"]), str(s["priced"]), hr,
                  f"{s['missed_wins']:,.2f}", f"{s['avoided_losses']:,.2f}",
                  f"[{net_style}]{net:+,.2f}[/{net_style}]", shadow.verdict(s))
    console.print(t)

    grand_net = totals["net"]
    grand_style = "red" if grand_net > 0 else "green"
    console.print(f"[bold]grand total[/bold]  missed wins {totals['missed_wins']:,.2f} · "
                  f"avoided losses {totals['avoided_losses']:,.2f} · "
                  f"net [{grand_style}]{grand_net:+,.2f}[/{grand_style}]")
    console.print(f"[dim]coverage: {coverage['windows']} windows touched · "
                  f"{coverage['unpriced_episodes']} unpriced episodes (no recorded ask) · "
                  f"{coverage['skipped_unresolved']} skipped (window not yet resolved)[/dim]")
