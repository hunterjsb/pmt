"""`pmt crypto viz` — Visualization of Bitcoin up-down trade dynamics.

Visualizes model probabilities, probability deltas (model vs book), fee structure,
expected return (ROI), and sensitivity across underlying variables (Spot, Margin,
Time Decay, and Volatility).
"""

from __future__ import annotations

import math
import json
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cli_common import console
from polymarket.constants import FEE_RATE, taker_fee
from polymarket.fit import _norm_cdf


def _calc_twap_p_up(spot: float, ref_px: float, banked: float, banked_s: float, rem_s: float, sig1m: float) -> tuple[float, float, float]:
    """Calculate P(UP), projected price, and breakeven for TWAP markets.

    Returns (p_up, projected_px, breakeven_px).
    """
    total_s = banked_s + rem_s
    if total_s <= 0 or rem_s <= 0:
        p = 1.0 if banked >= ref_px else 0.0
        return p, banked, ref_px

    proj = (banked * banked_s + spot * rem_s) / total_s
    breakeven = (ref_px * total_s - banked * banked_s) / rem_s
    sig_avg = sig1m * math.sqrt(max(rem_s / 60.0, 0.02) / 3.0)

    if sig_avg <= 0:
        p = 1.0 if spot >= breakeven else 0.0
    else:
        z = math.log(breakeven / spot) / sig_avg
        p = 1.0 - _norm_cdf(z)

    return max(0.0, min(1.0, p)), proj, breakeven


def _calc_close_open_p_up(spot: float, open_px: float, t_min: float, sig1m: float) -> float:
    """Calculate P(UP) for close-vs-open markets."""
    if t_min <= 0:
        return 1.0 if spot >= open_px else 0.0
    if sig1m <= 0:
        return 1.0 if spot >= open_px else 0.0
    z = math.log(spot / open_px) / (sig1m * math.sqrt(t_min))
    return max(0.0, min(1.0, _norm_cdf(z)))


def render_ascii_p_curve(spot: float, ref_px: float, banked: float, banked_s: float, rem_s: float, sig1m: float, kind: str = "twap") -> str:
    """Render an ASCII curve showing P(UP) across a range of spot price shifts (-30bp to +30bp)."""
    margin_steps = [-30, -20, -15, -10, -5, -2, 0, 2, 5, 10, 15, 20, 30]
    height_levels = 8
    width = len(margin_steps)

    probs = []
    for m_bp in margin_steps:
        s_test = spot * (1.0 + m_bp / 10000.0)
        if kind == "twap":
            p, _, _ = _calc_twap_p_up(s_test, ref_px, banked, banked_s, rem_s, sig1m)
        else:
            p = _calc_close_open_p_up(s_test, ref_px, rem_s / 60.0, sig1m)
        probs.append(p)

    lines = []
    lines.append("  P(UP) Sensitivity S-Curve (Spot Shift -30bp to +30bp)")
    lines.append("  1.00 ┤" + "".join("──" for _ in margin_steps))

    # Render grid
    for h in range(height_levels - 1, -1, -1):
        threshold = (h + 0.5) / height_levels
        row_char = f"  {threshold:4.2f} ┤"
        for idx, p in enumerate(probs):
            m_bp = margin_steps[idx]
            marker = "  "
            p_bucket = int(p * height_levels)
            if p_bucket == h:
                marker = "█ " if m_bp == 0 else "● "
            elif p_bucket > h and h == 0:
                marker = "│ "
            row_char += marker
        lines.append(row_char)

    lines.append("  0.00 ┴" + "".join("──" for _ in margin_steps))
    lines.append("        " + "".join(f"{m:+3d} " if m in [-30, -15, 0, 15, 30] else "    " for m in margin_steps))
    lines.append("        Margin Shift (bp)  [█ = Current Spot Position]")
    return "\n".join(lines)


def build_trade_metrics(fair_up: float, ask_up: float, ask_down: float, fee_rate: float = FEE_RATE) -> dict:
    """Calculate probability deltas, taker fees, net edges, and expected returns (ROI)."""
    fair_down = 1.0 - fair_up

    fee_up = taker_fee(ask_up, fee_rate)
    cost_up = ask_up + fee_up
    prob_delta_up = fair_up - ask_up
    net_edge_up = fair_up - cost_up
    roi_up = (net_edge_up / cost_up * 100.0) if cost_up > 0 else 0.0

    fee_down = taker_fee(ask_down, fee_rate)
    cost_down = ask_down + fee_down
    prob_delta_down = fair_down - ask_down
    net_edge_down = fair_down - cost_down
    roi_down = (net_edge_down / cost_down * 100.0) if cost_down > 0 else 0.0

    return {
        "up": {
            "fair": fair_up, "ask": ask_up, "fee": fee_up, "cost": cost_up,
            "delta_p": prob_delta_up, "net_edge": net_edge_up, "roi": roi_up,
        },
        "down": {
            "fair": fair_down, "ask": ask_down, "fee": fee_down, "cost": cost_down,
            "delta_p": prob_delta_down, "net_edge": net_edge_down, "roi": roi_down,
        }
    }


def build_sensitivity_matrix(spot: float, ref_px: float, banked: float, banked_s: float, rem_s: float, sig1m: float, ask_up: float, ask_down: float, kind: str = "twap") -> Table:
    """Table showing sensitivity of P(UP), Prob Delta ΔP, and Expected Return (ROI) to changes in Spot, Time, and Volatility."""
    table = Table(title="Scenario & Sensitivity Analysis Matrix", show_header=True, header_style="bold cyan")
    table.add_column("Scenario / Variable Shift", style="bold")
    table.add_column("Spot / Margin", justify="right")
    table.add_column("Rem Time", justify="right")
    table.add_column("Vol (σ)", justify="right")
    table.add_column("P(UP)", justify="right")
    table.add_column("ΔP (UP)", justify="right")
    table.add_column("Net Edge", justify="right")
    table.add_column("Exp. ROI", justify="right")

    base_p, _, _ = _calc_twap_p_up(spot, ref_px, banked, banked_s, rem_s, sig1m) if kind == "twap" else (_calc_close_open_p_up(spot, ref_px, rem_s / 60.0, sig1m), spot, ref_px)
    base_m = build_trade_metrics(base_p, ask_up, ask_down)

    scenarios = [
        ("Base Case", spot, rem_s, sig1m),
        ("Spot +10 bp", spot * 1.001, rem_s, sig1m),
        ("Spot -10 bp", spot * 0.999, rem_s, sig1m),
        ("Time Decay (75% Elapsed)", spot, max(30.0, (banked_s + rem_s) * 0.25), sig1m),
        ("Time Decay (90% Elapsed)", spot, max(15.0, (banked_s + rem_s) * 0.10), sig1m),
        ("Vol Compression (0.5x)", spot, rem_s, sig1m * 0.5),
        ("Vol Spike (1.5x)", spot, rem_s, sig1m * 1.5),
    ]

    for label, s_val, t_val, sig_val in scenarios:
        if kind == "twap":
            b_s = (banked_s + rem_s) - t_val
            p_val, _, _ = _calc_twap_p_up(s_val, ref_px, banked, b_s, t_val, sig_val)
        else:
            p_val = _calc_close_open_p_up(s_val, ref_px, t_val / 60.0, sig_val)

        m_val = build_trade_metrics(p_val, ask_up, ask_down)
        u = m_val["up"]

        m_bp = (s_val / ref_px - 1.0) * 10000.0
        roi_color = "green" if u["roi"] > 0 else ("red" if u["roi"] < 0 else "dim")

        table.add_row(
            label,
            f"{s_val:,.1f} ({m_bp:+.1f}bp)",
            f"{t_val:.0f}s",
            f"{sig_val * 10000:.1f}bp/m",
            f"{p_val:.3f}",
            f"{u['delta_p'] * 100:+.1f}%",
            f"{u['net_edge'] * 100:+.1f}¢",
            f"[{roi_color}]{u['roi']:+.1f}%[/{roi_color}]"
        )

    return table


@click.command("viz")
@click.argument("ref", required=False)
@click.option("--kind", type=click.Choice(["twap", "close_open"]), default="twap", show_default=True, help="Resolution family")
@click.option("--spot", type=float, default=90000.0, show_default=True, help="Current spot price")
@click.option("--ref-px", type=float, default=90000.0, show_default=True, help="Range start reference price")
@click.option("--banked", type=float, default=90000.0, show_default=True, help="Banked TWAP average price so far")
@click.option("--elapsed-pct", type=float, default=50.0, show_default=True, help="Percent of 5m window elapsed")
@click.option("--duration-min", type=float, default=5.0, show_default=True, help="Window duration in minutes")
@click.option("--sigma-bp", type=float, default=15.0, show_default=True, help="Volatility in bp/min")
@click.option("--ask-up", type=float, default=0.50, show_default=True, help="Current book ask price for UP")
@click.option("--ask-down", type=float, default=0.50, show_default=True, help="Current book ask price for DOWN")
@click.option("--fee-rate", type=float, default=FEE_RATE, show_default=True, help="Taker fee rate (crypto_fees_v2)")
@click.option("--json", "as_json", is_flag=True, help="Emit output as JSON")
def crypto_viz(ref: str | None, kind: str, spot: float, ref_px: float, banked: float,
               elapsed_pct: float, duration_min: float, sigma_bp: float,
               ask_up: float, ask_down: float, fee_rate: float, as_json: bool) -> None:
    """Visualize Bitcoin up-down trade mechanics, probabilities, fees & expected return.

    Accepts an optional REF (URL or slug) to load live market data, or uses simulation flags.
    """
    symbol = "BTCUSDT"
    title = "Bitcoin Up/Down Trade Simulation"

    if ref:
        from polymarket.crypto import eval_updown
        try:
            r = eval_updown(ref)
            symbol = r["symbol"]
            title = r["title"]
            kind = r["kind"]
            spot = r["spot"]
            sigma_bp = r["sigma_bp_per_min"]
            fee_rate = r["fee_rate"]
            m = r["model"]

            if kind == "twap" and not m.get("pending"):
                ref_px = m.get("ref", spot)
                banked = m.get("banked", spot)
                banked_s = m.get("banked_s", 150.0)
                rem_s = m.get("rem_s", 150.0)
            else:
                ref_px = m.get("open", spot)
                banked = spot
                rem_s = m.get("t_min", 2.5) * 60.0
                banked_s = max(0.0, duration_min * 60.0 - rem_s)

            if r.get("books"):
                ask_up = r["books"]["up"].get("best_ask") or ask_up
                ask_down = r["books"]["down"].get("best_ask") or ask_down

        except Exception as e:
            raise click.UsageError(f"Failed to fetch live market ref '{ref}': {e}")
    else:
        total_s = duration_min * 60.0
        banked_s = total_s * (elapsed_pct / 100.0)
        rem_s = total_s - banked_s

    sig1m = sigma_bp / 10000.0

    if kind == "twap":
        p_up, proj, breakeven = _calc_twap_p_up(spot, ref_px, banked, banked_s, rem_s, sig1m)
        margin_bp = (proj / ref_px - 1.0) * 10000.0
    else:
        p_up = _calc_close_open_p_up(spot, ref_px, rem_s / 60.0, sig1m)
        proj, breakeven = spot, ref_px
        margin_bp = (spot / ref_px - 1.0) * 10000.0

    metrics = build_trade_metrics(p_up, ask_up, ask_down, fee_rate)

    if as_json:
        out = {
            "title": title, "symbol": symbol, "kind": kind,
            "variables": {
                "spot": spot, "ref_px": ref_px, "banked": banked, "margin_bp": margin_bp,
                "banked_s": banked_s, "rem_s": rem_s, "sigma_bp_per_min": sigma_bp,
                "breakeven": breakeven, "projected": proj,
            },
            "p_up": p_up, "p_down": 1.0 - p_up,
            "metrics": metrics,
        }
        console.print_json(json.dumps(out))
        return

    # Header Panel
    console.print(Panel(
        f"[bold white]{title}[/bold white]\n"
        f"[dim]{symbol} · {kind.upper()} · Spot: ${spot:,.2f} · Ref: ${ref_px:,.2f} · Margin: {margin_bp:+.1f}bp · σ: {sigma_bp:.1f}bp/min[/dim]",
        style="blue"
    ))

    # 1. Model Mechanics & Equations Table
    overview_table = Table(title="1. Model Mechanics & Current Variables", show_header=True)
    overview_table.add_column("Variable / Parameter", style="bold")
    overview_table.add_column("Value", justify="right")
    overview_table.add_column("Formula & Economic Meaning", style="dim")

    overview_table.add_row("Spot Price (S)", f"${spot:,.2f}", "Current exchange mark")
    overview_table.add_row("Reference Price (S₀)", f"${ref_px:,.2f}", "Window start price benchmark")
    if kind == "twap":
        overview_table.add_row("Banked TWAP (S_banked)", f"${banked:,.2f}", f"Realized average over first {banked_s:.0f}s")
        overview_table.add_row("Projected Final TWAP", f"${proj:,.2f}", f"Weighted avg of banked ({banked_s:.0f}s) + spot ({rem_s:.0f}s)")
        overview_table.add_row("Breakeven Spot Level", f"${breakeven:,.2f}", "Required spot for remaining time to win UP")
    overview_table.add_row("Margin (bp)", f"{margin_bp:+.1f} bp", "Projected distance to reference baseline")
    overview_table.add_row("Volatility (σ)", f"{sigma_bp:.1f} bp/min", "Realized per-minute standard deviation")
    overview_table.add_row("Time Elapsed / Rem", f"{banked_s:.0f}s / {rem_s:.0f}s", f"Total window {banked_s + rem_s:.0f}s")
    overview_table.add_row("Model P(UP) / P(DOWN)", f"[bold cyan]{p_up:.3f}[/bold cyan] / [bold yellow]{1.0 - p_up:.3f}[/bold yellow]", "Reflection principle / lognormal driftless walk")
    console.print(overview_table)

    # 2. Probability Delta & Expected Return Table
    exp_table = Table(title="2. Probability Delta (ΔP) & Expected Return (ROI) Analysis", show_header=True)
    exp_table.add_column("Outcome Side", style="bold")
    exp_table.add_column("Fair Prob (P)", justify="right")
    exp_table.add_column("Book Ask (P_ask)", justify="right")
    exp_table.add_column("Prob Delta (ΔP)", justify="right")
    exp_table.add_column("Taker Fee (f)", justify="right")
    exp_table.add_column("Net Cost (C)", justify="right")
    exp_table.add_column("Net Edge", justify="right")
    exp_table.add_column("Expected ROI (%)", justify="right")

    for side in ("up", "down"):
        m_side = metrics[side]
        roi_val = m_side["roi"]
        roi_str = f"{roi_val:+.1f}%"
        roi_style = "[bold green]" if roi_val >= 2.0 else ("[red]" if roi_val < 0 else "[dim]")

        exp_table.add_row(
            side.upper(),
            f"{m_side['fair']:.3f}",
            f"{m_side['ask']:.3f}",
            f"{m_side['delta_p'] * 100:+.1f}%",
            f"${m_side['fee']:.4f}",
            f"${m_side['cost']:.4f}",
            f"{m_side['net_edge'] * 100:+.1f}¢",
            f"{roi_style}{roi_str}{'[/bold green]' if roi_val >= 2.0 else ('[/red]' if roi_val < 0 else '[/dim]')}"
        )
    console.print(exp_table)

    # 3. ASCII Sensitivity Curve
    curve_str = render_ascii_p_curve(spot, ref_px, banked, banked_s, rem_s, sig1m, kind)
    console.print(Panel(curve_str, title="3. Probability P(UP) Sensitivity Curve", border_style="cyan"))

    # 4. Scenario Impact Matrix
    matrix_table = build_sensitivity_matrix(spot, ref_px, banked, banked_s, rem_s, sig1m, ask_up, ask_down, kind)
    console.print(matrix_table)
