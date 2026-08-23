"""Wallet acquisition + grading, and the `pmt crypto stats` report over it.

THE SINGLE-SOURCE RULE THIS MODULE EXISTS TO HOLD:

  * `_tape_scoreboard()` is the ONE acquisition path for the trading wallet's
    graded record. `pmt crypto stats` calls it. `pmt crypto watch` calls it —
    the same function object, not a parallel implementation (pinned by
    test_cli_crypto_watch.py::test_watch_fetch_sb_is_the_stats_acquisition_path).
    `pmt crypto journal` calls it. A new consumer calls it too.
  * NEVER cache the wallet feed. The data-api rows mutate in place and the
    pages seam-shift; an incremental ledger over them drifted the dashboard's
    P&L away from the report's five separate ways before it was deleted on
    2026-08-23. polymarket/wallet.py carries the full autopsy — read it before
    you decide the re-walk is too expensive.
  * Grading is `score_activity()`, a PURE fold over already-fetched rows. Any
    caller that already holds the rows aggregates through it rather than
    growing a second convention for what counts as a win.

Pairs with stats_render.py the way cli_crypto_watch.py pairs with watch_ui.py:
everything here fetches and folds, everything there formats.
"""

from __future__ import annotations

import json
import statistics
import sys
import time

import click

import stats_render
from cli_common import _api, _parse_since, console
from engine import post as _engine_post
from polymarket import effectiveness, tape, updown_slugs, updown_stats, wallet


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


def _impute_win_pnl(buy_usd: float, sell_usd: float, buy_shares: float) -> float:
    """A gamma-confirmed WIN whose real $1/share redeem row hasn't posted yet
    (Polymarket's auto-redeemer can lag minutes): impute the payout as
    shares*$1 — what it will actually pay — so the $ figure tracks the W/L
    figure instead of showing a fake loss until the real redeem row lands
    and naturally replaces this estimate on a later scan.
    """
    return buy_shares * 1.0 + sell_usd - buy_usd


def _tape_scoreboard(floor: float, sliding_floor: float | None = None,
                      keep_activity: bool = False) -> dict:
    """Fetch the wallet's activity and grade it. THE acquisition path — every
    consumer of the graded record calls this one: `stats`, `watch`, `journal`.

    There is no faster variant to reach for, and adding one is the bug this
    module's docstring is about. A caller that already holds rows folds them
    with score_activity() instead; a caller that needs rows calls this.

    `keep_activity` hands the raw rows back under "activity". The wallet walk
    is the slowest thing this report does, and the maker attribution and the
    --gates ledger both need the same rows — paginating twice for one report
    would be paying the price twice for one answer.
    """
    # Ground truth: every updown trade + redemption on the proxy wallet.
    # funder_address() RAISES on an unset addr, like every sibling command —
    # never fall through to a clean-looking "0W-0L" (docs/LESSONS.md#L26).
    addr = wallet.funder_address()
    rows = wallet.fetch_wallet_activity(addr, floor)
    sb = score_activity(rows, floor, sliding_floor=sliding_floor)
    if keep_activity:
        sb["activity"] = rows
    return sb


def score_activity(rows: list[dict], floor: float,
                   sliding_floor: float | None = None) -> dict:
    """W-L / realized P&L graded by the WALLET (data-api activity), not the
    model's own final read — a model that's confidently wrong (XRP basis,
    2026-08-23) would otherwise grade its own loss as a win. The tape only
    contributes fire records (stated fairs) for the calibration table.

    Pure aggregation over already-fetched activity `rows` (plus the local
    tape file and the TTL-cached gamma cross-check) — no wallet pagination of
    its own, so a caller holding rows can re-grade them for free. Being pure
    is what lets `stats` and `watch` share one definition of a win.

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
                               {"w": 0, "l": 0, "open": 0, "pnl": 0.0, "usd": 0.0,
                                "est": 0, "pnls": []})
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
        s["pnls"].append(pnl)
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
    # A series' TYPICAL window, which its total P&L hides: one -$300 tail on
    # forty +$4 windows reads as a broken series by sum and a working one by
    # median, and the difference is the whole sizing question.
    for s in series.values():
        pnls = s.pop("pnls")
        s["med"] = statistics.median(pnls) if pnls else None
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

    The watch header calls this on its own snapshot too, so the dashboard and
    the report grade the same windows against the same bankroll.
    """
    cash = float((bal or {}).get("total") or 0.0)
    bankroll = cash + float(sb.get("riding_usd") or 0.0)
    return effectiveness.summary(sb.get("eff_windows") or [],
                                  bankroll=bankroll or None, now=time.time())


def _stats_blocks(sb: dict, status: dict, floor: float) -> dict:
    """The tape-derived half of the report: arm flags, the resting-bid
    experiment, the order path, the fleet ration.

    Two tape files, read once each and folded by polymarket.updown_stats.
    Deliberately NOT part of score_activity: the watch dashboard re-scores
    every 10s and has no use for any of this, so putting it there would
    charge the dashboard for a one-shot report's blocks.
    """
    evals = list(tape.iter_records(tape.UPDOWN_TAPE, floor=floor,
                                    evs={tape.EV_EVAL, tape.EV_FIRE}))
    fires = [r for r in evals if r.get("ev") == tape.EV_FIRE]
    evals = [r for r in evals if r.get("ev") == tape.EV_EVAL]
    orders = list(tape.iter_records(tape.ORDER_TAPE, floor=floor))
    return {
        "flags": updown_stats.arm_flags(status.get("arms")),
        "maker": updown_stats.maker_summary(evals, orders,
                                             sb.get("activity") or [],
                                             sb.get("eff_windows") or []),
        "chase": updown_stats.chase_summary(orders, fires),
        "fleet": updown_stats.fleet_summary(evals, status.get("fleet_undecided_cap")),
    }


@click.command("stats")
@click.option("--since", type=float, default=None,
              help="Windows starting after this point: hours-ago if small, "
                   "raw unix epoch if large (default: all time — the full "
                   "ledger of record). NOTE an hours-ago floor SLIDES — pin "
                   "an epoch for any number you intend to compare across runs")
@click.option("--full", is_flag=True,
              help="Also print calibration and a live-arms snapshot "
                   "(both demoted: see analysis/r6_report.txt and `pmt crypto watch`)")
@click.option("--gates", is_flag=True,
              help="Also price every refusal on the tape — what our own gates "
                   "cost and saved, per reason. Resolves (and REFRESHES on "
                   "disk) the outcomes corpus, so it is markedly slower than "
                   "the default report")
@click.option("--json", "as_json", is_flag=True)
def crypto_stats(since: float | None, full: bool, gates: bool, as_json: bool) -> None:
    """Fleet scoreboard: record + streak, per-symbol P&L, effectiveness, live experiments."""
    floor = _parse_since(since) if since else 0.0
    try:
        sb = _tape_scoreboard(floor, keep_activity=True)
    except Exception as e:
        console.print(f"[red]data-api unreachable: {e}[/red]")
        sys.exit(1)

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
    blocks = _stats_blocks(sb, status, floor)
    gates_report = _gates_report(sb.pop("activity", []), floor) if gates else None

    if as_json:
        click.echo(json.dumps({
            "wins": sb["wins"], "losses": sb["losses"], "net_est": sb["net"],
            "rolls": sb["rolls"], "estimated": sb["estimated"],
            "series": sb["series"],
            "calibration": {str(k): v for k, v in sb["cal"].items()},
            "arms": status.get("arms", {}), "balance": bal,
            "windows": sb["windows"], "effectiveness": eff_s,
            "maker": blocks["maker"], "chase": blocks["chase"],
            "fleet": blocks["fleet"], "gates": gates_report,
        }, indent=2))
        return

    console.print(stats_render.render_stats(sb, eff_s, bal, status, floor,
                                             blocks=blocks, full=full,
                                             gates=gates_report))


def _gates_report(activity: list[dict], since_epoch: float) -> dict:
    """The shadow ledger behind `pmt crypto stats --gates`.

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

    `activity` is the caller's already-walked wallet history, not a fresh
    fetch: this runs as one section of a report that has already paid for it.
    """
    from polymarket import chainlink as ck
    from polymarket import outcomes, shadow

    try:
        with open(tape.UPDOWN_TAPE) as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []

    slugs = outcomes.extract_updown_slugs(lines)
    windows = outcomes.window_universe(slugs, since_epoch, time.time())

    wallet_wins = outcomes.wallet_outcomes(activity)
    rounds_by_symbol = {w["symbol"]: ck.load_corpus(w["symbol"]) for w in windows}
    rows, _dropped = outcomes.build_outcomes(windows, wallet_wins, rounds_by_symbol)

    merged, _added, _upgraded = outcomes.merge_outcomes(outcomes.load_outcomes(), rows)
    outcomes.write_outcomes(merged)
    winners = {slug: row["winner"] for slug, row in merged.items()}

    return shadow.build_report(lines, winners, activity, since=since_epoch)
