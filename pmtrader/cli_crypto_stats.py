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
from polymarket import effectiveness, eras, tape, updown_slugs, updown_stats, wallet


_GAMMA_CACHE: dict[str, tuple[float, dict]] = {}
_GAMMA_TTL_S = 120  # watch's scoreboard refreshes every 10s; a slug's resolution doesn't flip that often


def _gamma_resolution_cached(slug: str) -> dict | None:
    """outcomes.gamma_resolution(), cached ~120s per slug so the watch
    dashboard's 10s scoreboard refresh doesn't hammer gamma. None on any
    fetch/parse failure — callers must degrade gracefully, never guess.

    THE ONE gamma resolution fetch in the codebase, and `closed=true` is the
    load-bearing part of it. Gamma's /markets defaults to closed=false, so
    without the flag every SETTLED window answers `[]` — which parses as "not
    resolved yet" and rides forever. That is exactly how three resolved-lost
    windows hid -$272.35 from the ledger for 13-25h on 2026-08-23; a window
    that has not settled still answers `[]` here, which is the honest riding.
    """
    import time as _t

    import requests

    from polymarket import hosts, outcomes

    now = _t.time()
    hit = _GAMMA_CACHE.get(slug)
    if hit and now - hit[0] < _GAMMA_TTL_S:
        return hit[1]
    try:
        r = requests.get(f"{hosts.GAMMA}/markets",
                          params={"slug": slug, "closed": "true"},
                          headers=hosts.UA, timeout=8)
        r.raise_for_status()
        result = outcomes.gamma_resolution(r.json())
    except Exception:
        return None
    _GAMMA_CACHE[slug] = (now, result)
    return result


def _impute_win_pnl(buy_usd: float, sell_usd: float, buy_shares: float,
                     sell_shares: float = 0.0) -> float:
    """A resolution-confirmed WIN whose real $1/share redeem row hasn't posted
    yet (Polymarket's auto-redeemer can lag minutes): impute the payout as
    shares*$1 — what it will actually pay — so the $ figure tracks the W/L
    figure instead of showing a fake loss until the real redeem row lands
    and naturally replaces this estimate on a later scan.

    Only the shares still HELD redeem. Counting sold shares at $1 as well as
    banking their sale proceeds books the same shares twice.
    """
    return max(buy_shares - sell_shares, 0.0) * 1.0 + sell_usd - buy_usd


def _tape_scoreboard(floor: float, sliding_floor: float | None = None,
                      keep_activity: bool = False, ceiling: float | None = None,
                      walk_floor: float | None = None,
                      tape_records: list[dict] | None = None) -> dict:
    """Fetch the wallet's activity and grade it. THE acquisition path — every
    consumer of the graded record calls this one: `stats`, `watch`, `journal`.

    There is no faster variant to reach for, and adding one is the bug this
    module's docstring is about. A caller that already holds rows folds them
    with score_activity() instead; a caller that needs rows calls this.

    `keep_activity` hands the raw rows back under "activity". The wallet walk
    is the slowest thing this report does, and the maker attribution, the
    --gates ledger and the by-era table all need the same rows — paginating
    twice for one report would be paying the price twice for one answer.

    `walk_floor` separates how far BACK we paginate from what we grade, and
    exists for exactly one caller: `--era` grades one era but still owes the
    operator a complete era table, which needs the whole ledger. Defaults to
    `floor`, so every other caller pages back exactly as far as it grades.
    """
    # Ground truth: every updown trade + redemption on the proxy wallet.
    # funder_address() RAISES on an unset addr, like every sibling command —
    # never fall through to a clean-looking "0W-0L" (docs/LESSONS.md#L26).
    addr = wallet.funder_address()
    rows = wallet.fetch_wallet_activity(addr, floor if walk_floor is None else walk_floor)
    sb = score_activity(rows, floor, sliding_floor=sliding_floor, ceiling=ceiling,
                        tape_records=tape_records)
    if keep_activity:
        sb["activity"] = rows
    return sb


def _fire_roll_records() -> list[dict]:
    """The updown tape's fire+roll slice, read once and materialized.

    Hand this to every score_activity() call in one report. The by-era table
    grades N eras off the same tape; re-reading a 15MB jsonl per era would
    pay for one answer N times over, the same way re-walking the wallet
    would. Callers that grade once can leave it None and let score_activity
    read the tape itself.
    """
    return list(tape.iter_records(tape.UPDOWN_TAPE,
                                  evs={tape.EV_FIRE, tape.EV_ROLL}))


# Decided windows the display list carries. A cap, so views must SAY they are
# capped — a retention limit that isn't on screen is indistinguishable from a
# missing trade (watch's trades panel names it in its title).
WINDOWS_SHOWN = 12


def _window_row(slug: str, w: dict, end: float, side: str | None,
                 won: bool | None, pnl: float | None, est: bool) -> dict:
    """One window's record, decided or still riding.

    `won=None`/`pnl=None` IS the riding case, given the same shape as a decided
    row on purpose: a live view renders both from one list without a second
    convention for what a position looks like.
    """
    shares = w["buy_shares"]
    return {
        "slug": slug, "won": won, "pnl": pnl, "est": est, "end_ts": end,
        "notional": w["buy"], "shares": shares,
        # The average dollar's entry price. Notional alone can't say it —
        # 10sh @ 0.96 and 96sh @ 0.10 are both $9.60 and opposite bets.
        "entry_px": (w["buy"] / shares) if shares else None,
        "side": side,
        "entry_ts": effectiveness.weighted_ts(w["buy_ts_usd"], w["buy"]),
        "exit_ts": w["exit_ts"],
    }


def score_activity(rows: list[dict], floor: float,
                   sliding_floor: float | None = None,
                   ceiling: float | None = None,
                   tape_records: list[dict] | None = None) -> dict:
    """W-L / realized P&L graded by the EXCHANGE — the wallet's own rows
    first, and the market's settled resolution where the wallet is silent.
    Never the model's own final read: a model that's confidently wrong (XRP
    basis, 2026-08-23) would otherwise grade its own loss as a win, so a
    chainlink or terminal-book label never decides a W or an L here. The tape
    only contributes fire records (stated fairs) for the calibration table.

    Resolution is on the exchange's side of that line, not ours — it is what
    redeems are paid on — and it is the only evidence a LOSS can ever have,
    because a losing position pays $0 and posts no wallet row at all. Grading
    on wallet rows alone books every win and no silent loss (2026-08-23:
    -$272.35 hidden across three windows). polymarket.outcomes has the full
    ranking; the fixture freezer stays stricter and still demands "wallet".

    Pure aggregation over already-fetched activity `rows` (plus the local
    tape file and the TTL-cached gamma cross-check) — no wallet pagination of
    its own, so a caller holding rows can re-grade them for free. Being pure
    is what lets `stats` and `watch` share one definition of a win.

    The floor selects WINDOWS (slug start epoch >= floor), never individual
    transactions — filtering by row timestamp let a window's redeem into the
    range while its buys fell outside, printing phantom profit (caught live
    2026-08-23: +$78 shown vs -$17 true).

    `ceiling` closes that selection on the right (start < ceiling), making it
    half-open [floor, ceiling). That is what the by-era table grades through:
    adjacent eras share a boundary, and half-open is what stops a window that
    starts exactly on one from being counted in both.

    `tape_records`, if given, is a pre-read fire+roll slice (_fire_roll_records)
    used instead of re-reading the tape file — the sharing discipline the era
    loop needs. None means read it here, which is what every other caller does.

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

    def _in_range(start: float) -> bool:
        return start >= floor and (ceiling is None or start < ceiling)

    win_by_slug: dict[str, dict] = {}
    for a in rows:
        slug = a.get("slug") or ""
        if not updown_slugs.is_updown(slug) or not _in_range(updown_slugs.window_start(slug)):
            continue
        w = win_by_slug.setdefault(slug, {"buy": 0.0, "sell": 0.0, "redeem": 0.0,
                                          "redeem_seen": False, "won": None,
                                          "buy_shares": 0.0, "sell_shares": 0.0,
                                          "buy_sides": set(),
                                          "buy_ts_usd": 0.0, "exit_ts": 0.0})
        usd = a.get("usdcSize") or 0.0
        ts = float(a.get("timestamp") or 0.0)
        if a["type"] == "TRADE":
            w["buy" if a.get("side") == "BUY" else "sell"] += usd
            if a.get("side") == "BUY":
                w["buy_shares"] += a.get("size") or 0.0
                w["buy_sides"].add((a.get("outcome") or "").lower())
                # Exposure-time accumulators (polymarket.effectiveness): the
                # average dollar's entry, and when the capital came back.
                w["buy_ts_usd"] += usd * ts
            else:
                # Shares out, not dollars out: what decides whether anything
                # is still held at settlement (outcomes.exited_flat).
                w["sell_shares"] += a.get("size") or 0.0
        elif a["type"] == "REDEEM":
            # A zero-share, zero-dollar REDEEM is a stub, not a redemption
            # (data-api posted them beside two resolution-confirmed wins,
            # 2026-08-23 23:01Z): it burned nothing and proves nothing, but
            # setting redeem_seen on it locks grade_window's rule 2 into LOSS
            # and skips the gamma cross-check + win imputation — two wins
            # graded as -$38.69. Real loss evidence burns shares (size > 0).
            if usd <= 0 and (a.get("size") or 0.0) <= 0:
                continue
            w["redeem"] += usd
            w["redeem_seen"] = True
            w["exit_ts"] = max(w["exit_ts"], ts)
            if usd > 0.5:
                w["won"] = (a.get("outcome") or "").lower()

    fires: dict[str, list] = {}
    rolls = rolls_sliding = 0
    records = (tape.iter_records(tape.UPDOWN_TAPE, evs={tape.EV_FIRE, tape.EV_ROLL})
               if tape_records is None else tape_records)
    for r in records:
        ev = r.get("ev")
        if ev == tape.EV_FIRE:
            if _in_range(updown_slugs.window_start(r.get("slug", ""))):
                fires.setdefault(r["slug"], []).append(r)
        # A roll has no window of its own, so it is ranged on its own record
        # time — the one place in this function that is not a window start.
        elif ev == tape.EV_ROLL and _in_range(r.get("t", 0)):
            rolls += 1
            if sliding_floor is not None and r.get("t", 0) >= sliding_floor:
                rolls_sliding += 1

    series: dict[str, dict] = {}
    cal: dict[float, list] = {}
    window_list: list[dict] = []
    riding_list: list[dict] = []
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
        # The side we HELD. The tape's fire names it, but the tape rotates and
        # the wallet's BUY rows don't — without the fallback a window whose
        # fire aged out can never be graded against a resolution, and rides
        # forever (btc-updown-5m-1787419200, 26h). Mixed sides in one window
        # mean we can't say which we held, so say nothing.
        fired = (fires.get(slug, [{}])[0].get("side")
                 or (next(iter(w["buy_sides"])) if len(w["buy_sides"]) == 1 else None)
                 or None)
        no_redeem = w["redeem"] <= 0.5 and not w["redeem_seen"]
        if no_redeem and outcomes.exited_flat(w["buy_shares"], w["sell_shares"]):
            # Sold flat before settlement: nothing left to redeem, so no wallet
            # row and no resolution is ever coming. The P&L is already fully
            # realized, and its sign is the whole verdict.
            won, is_est = (w["sell"] - w["buy"]) > 0, False
            gamma = None
        else:
            # Redemption is silent (no row at all) or slow far more often than a
            # loss actually is — a resolution round-trip is only worth it once
            # the grace window has passed with no redeem of either kind.
            gamma = (_gamma_resolution_cached(slug)
                     if no_redeem and now >= end + outcomes.RESOLUTION_GRACE_S
                     else None)
            won, is_est = outcomes.grade_window(w["redeem"], w["redeem_seen"],
                                                 fired, gamma, now, end)
        if won is None:
            s["open"] += 1
            # Still riding — its bought notional is speculative exposure the
            # risk header's "riding N windows $W" needs, distinct from a live
            # arm's committed budget (this window may have already rolled off).
            riding_n += 1
            riding_usd += w["buy"]
            # Its IDENTITY, not just its count. Between the fill and the redeem
            # row — or the 300s gamma grace on a loss, which posts no row at all
            # — this is the ONLY record of a position the operator is holding:
            # the arm has already rolled to the next window, and the fire has
            # scrolled off the tape. A count can't be rendered as a trade.
            riding_list.append(_window_row(slug, w, end, fired, None, None, False))
            continue
        pnl_est = is_est
        if won and no_redeem and not outcomes.exited_flat(w["buy_shares"], w["sell_shares"]):
            # Resolution confirmed the win before Polymarket's redeemer posted
            # the real payout row — impute it so the $ figure doesn't lag the
            # W/L figure by however long the slow auto-redeem takes. A window
            # sold flat has no shares left to pay and needs no estimate.
            pnl = _impute_win_pnl(w["buy"], w["sell"], w["buy_shares"], w["sell_shares"])
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
        window_list.append(_window_row(slug, w, end, fired, won, pnl, bool(pnl_est)))
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
    windows = sorted(window_list, key=lambda r: r["end_ts"], reverse=True)[:WINDOWS_SHOWN]
    result = {"wins": wins, "losses": losses, "net": net, "rolls": rolls,
              "series": series, "cal": cal, "estimated": estimated,
              "riding_n": riding_n, "riding_usd": riding_usd, "windows": windows,
              # Uncapped: a handful at most, and every one of them is money
              # currently at risk — capping THAT would be the same bug again.
              "riding_windows": sorted(riding_list, key=lambda r: r["end_ts"],
                                        reverse=True),
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


def era_scoreboards(rows: list[dict], tape_records: list[dict] | None = None,
                     registry: tuple | None = None) -> list[dict]:
    """One graded scoreboard per policy era (polymarket.eras), off ONE walk.

    Grading is score_activity's, called once per era over the era's own
    half-open [start, next start) window range — the same selection the
    --since path uses. Nothing here re-derives a W, an L or a dollar, so an
    era row and a floored report can never disagree about what a window was
    worth.

    The expensive halves are fetched ONCE by the caller and shared: `rows` is
    the already-paginated wallet activity and `tape_records` the already-read
    fire+roll slice. N eras therefore cost N CPU passes over memory, not N
    wallet walks and not N tape reads.

    Every era in the registry gets a row, including ones the fleet never
    traded — an era that renders as 0-0 is information, and an era that
    silently vanishes is how a bad regime gets forgotten.
    """
    registry = eras.ERAS if registry is None else registry
    if tape_records is None:
        tape_records = _fire_roll_records()
    now = time.time()
    out = []
    for e in registry:
        lo, hi = eras.bounds(e, registry)
        sb = score_activity(rows, lo, ceiling=hi, tape_records=tape_records)
        out.append({
            "name": e.name, "why": e.why, "start": lo, "end": hi, "sb": sb,
            # Stamped here, not in the renderer: the running era's span ends
            # at the clock, and stats_render owns pixels, never a clock.
            "span_h": None if lo <= 0 else ((now if hi == float("inf") else hi) - lo) / 3600.0,
            # The bar this era's OWN payoff shape had to clear — a break-even
            # need computed over all time would grade a regime against a
            # payoff structure it never traded.
            "breakeven": effectiveness.breakeven_win_rate(sb.get("eff_windows") or []),
        })
    return out


def era_row(era_rows: list[dict], name: str) -> dict | None:
    for r in era_rows:
        if r["name"] == name:
            return r
    return None


def _stats_blocks(sb: dict, status: dict, floor: float,
                   ceiling: float | None = None) -> dict:
    """The tape-derived half of the report: arm flags, the resting-bid
    experiment, the order path, the fleet ration.

    Two tape files, read once each and folded by polymarket.updown_stats.
    Deliberately NOT part of score_activity: the watch dashboard re-scores
    every 10s and has no use for any of this, so putting it there would
    charge the dashboard for a one-shot report's blocks.
    """
    def _under(rs):
        # These blocks fold TAPE records, which carry a record time and no
        # window of their own — so an era scope closes them on `t`, not on a
        # window start the way the scoreboard does.
        return [r for r in rs if ceiling is None or r.get("t", 0) < ceiling]

    evals = _under(tape.iter_records(tape.UPDOWN_TAPE, floor=floor,
                                      evs={tape.EV_EVAL, tape.EV_FIRE}))
    fires = [r for r in evals if r.get("ev") == tape.EV_FIRE]
    evals = [r for r in evals if r.get("ev") == tape.EV_EVAL]
    orders = _under(tape.iter_records(tape.ORDER_TAPE, floor=floor))
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
@click.option("--era", "era_name", type=str, default=None,
              help="Scope the whole report to one policy era (see the by-era "
                   "table for the names). The era table itself stays complete "
                   "— scoping the view never hides an era")
@click.option("--json", "as_json", is_flag=True)
def crypto_stats(since: float | None, full: bool, gates: bool,
                 era_name: str | None, as_json: bool) -> None:
    """Fleet scoreboard: record + streak, per-symbol P&L, effectiveness, live experiments."""
    era = None
    if era_name is not None:
        era = eras.by_name(era_name)
        if era is None:
            raise click.UsageError(
                f"unknown era {era_name!r} — known eras: {', '.join(eras.names())}")
    if era is not None and since is not None:
        raise click.UsageError("--era and --since both pick a range; use one")

    floor, ceiling = (_parse_since(since) if since else 0.0), None
    scope_label = None
    if era is not None:
        floor, ceiling = eras.bounds(era)
        scope_label = f"era {era.name} · {stats_render.era_span_label(floor, ceiling)}"
    # The era table needs the FULL ledger even when the report is scoped, so
    # --era pages back to the start while grading only its own era. --since is
    # the operator narrowing the walk itself, and gets no era table at all.
    show_eras = since is None
    try:
        tape_records = _fire_roll_records()
        sb = _tape_scoreboard(floor, keep_activity=True, ceiling=ceiling,
                              walk_floor=0.0 if show_eras else None,
                              tape_records=tape_records)
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
    blocks = _stats_blocks(sb, status, floor, ceiling=ceiling)
    # Same rows, same tape: the era table is the third consumer of the one
    # walk, not a second walk of its own.
    era_rows = (era_scoreboards(sb.get("activity") or [], tape_records=tape_records)
                if show_eras else None)
    gates_report = _gates_report(sb.pop("activity", []), floor) if gates else None

    # The chip beside all-time names the era the report is LOOKING THROUGH:
    # the scoped one under --era, otherwise the era the clock is in.
    era_now = era_row(era_rows or [], (era or eras.current()).name) if era_rows else None

    if as_json:
        click.echo(json.dumps({
            "wins": sb["wins"], "losses": sb["losses"], "net_est": sb["net"],
            "rolls": sb["rolls"], "estimated": sb["estimated"],
            "series": sb["series"],
            "calibration": {str(k): v for k, v in sb["cal"].items()},
            "arms": status.get("arms", {}), "balance": bal,
            "windows": sb["windows"],
            # Filled but not yet graded. Same field watch's trades panel
            # paints, so the two views can't disagree about what is open.
            "riding_windows": sb["riding_windows"], "effectiveness": eff_s,
            "maker": blocks["maker"], "chase": blocks["chase"],
            "fleet": blocks["fleet"], "gates": gates_report,
            "era": era.name if era else None,
            "eras": [{"name": r["name"], "why": r["why"], "start": r["start"],
                      "end": None if r["end"] == float("inf") else r["end"],
                      "wins": r["sb"]["wins"], "losses": r["sb"]["losses"],
                      "net_est": r["sb"]["net"], "breakeven_win_rate": r["breakeven"]}
                     for r in (era_rows or [])],
        }, indent=2))
        return

    console.print(stats_render.render_stats(sb, eff_s, bal, status, floor,
                                             blocks=blocks, full=full,
                                             gates=gates_report, era_rows=era_rows,
                                             era_now=era_now, scope_label=scope_label,
                                             eras_omitted=not show_eras))


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
    # No resolution lookups here on purpose: that is one gamma round-trip per
    # ungraded window, and `pmt crypto outcomes` already banks them into the
    # corpus — which load_outcomes below picks up for free.
    rows, _dropped = outcomes.build_outcomes(windows, wallet_wins, rounds_by_symbol)

    merged, _added, _upgraded = outcomes.merge_outcomes(outcomes.load_outcomes(), rows)
    outcomes.write_outcomes(merged)
    winners = {slug: row["winner"] for slug, row in merged.items()}

    return shadow.build_report(lines, winners, activity, since=since_epoch)
