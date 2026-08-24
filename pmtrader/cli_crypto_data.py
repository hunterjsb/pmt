"""Read-only `pmt crypto` reports over the wallet, the decision tape, and the
Chainlink corpus: `tape`, `activity`, `window`, `basis`, `outcomes`, `journal`,
`regime`.

Nothing here posts to the engine or places an order — these are the commands
you reach for AFTER something happened, to find out what. `outcomes`,
`journal` and `regime` do write to disk, but only to the local corpus and the
private journal, never to the repo and never to the book.

Grading and wallet acquisition are not defined here: they come from
cli_crypto_stats, which is the one place that owns them.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import click
from rich.table import Table

from cli_common import _parse_since, _pnl_color, console
from cli_crypto_stats import _gamma_resolution_cached, _tape_scoreboard
from polymarket import errlog, tape, updown_slugs, updown_stats, wallet
from watch_ui import _tape_render, _tape_slug


@click.command("tape")
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


def _funder_or_usage_error() -> str:
    """wallet.funder_address(), re-raised as the click.UsageError every
    command-level caller already showed for a missing PM_FUNDER_ADDRESS."""
    try:
        return wallet.funder_address()
    except ValueError as e:
        raise click.UsageError(str(e))


@click.command("activity")
@click.option("--limit", "n", default=40, show_default=True, help="Rows to show")
@click.option("--all", "show_all", is_flag=True,
              help="Every activity type, not just updown windows")
@click.option("--refresh", is_flag=True,
              help=f"Re-walk the full feed into {wallet.ACTIVITY_DUMP} first — the dump "
                   f"the fixture freezer grades money against")
def crypto_activity(n: int, show_all: bool, refresh: bool) -> None:
    """Recent wallet activity — the curl+jq boilerplate, built in.

    Printing is a live read and touches no file. `--refresh` additionally
    rewrites the on-disk activity dump; it is the ONLY command that does, and
    the fixture freezer refuses to grade a window the dump does not reach.
    """
    import time as _t

    addr = _funder_or_usage_error()

    if refresh:
        try:
            written = wallet.refresh_activity_dump(addr=addr)
        except Exception as e:
            console.print(f"[red]refresh failed: {e}[/red]")
            sys.exit(1)
        newest = wallet.activity_dump_coverage()
        stamp = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(newest)) if newest else "empty"
        console.print(f"[green]refreshed[/green] {wallet.ACTIVITY_DUMP} — "
                      f"{written} rows, newest {stamp}")

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


@click.command("window")
@click.argument("slug")
def crypto_window(slug: str) -> None:
    """Post-mortem for one updown window: wallet trades + tape, merged by time."""
    import time as _t

    parsed = updown_slugs.parse_updown_slug(slug)
    if parsed is None:
        raise click.UsageError(f"not an updown slug: {slug!r} (want e.g. btc-updown-15m-1787449500)")
    start, end = parsed["start"], parsed["end"]
    dur = updown_slugs.dur_label(parsed["dur_s"])

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
        except Exception as e:
            # A window post-mortem that silently drops the one record
            # explaining the trade is worse than one that says it couldn't
            # render it.
            errlog.note("cli_crypto_data.window.tape_render", e,
                        slug=slug, ev=r.get("ev"))
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
                           f"run without --no-fetch to build it[/dim]\n")
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


@click.command("basis")
@click.option("--symbol", type=click.Choice(_ORACLE_SYMBOLS), default="all", show_default=True)
@click.option("--hours", type=float, default=24.0, show_default=True, help="Corpus window to analyze")
@click.option("--aligned", is_flag=True,
              help="TWAP-vs-TWAP aligned basis (per-minute + settlement-shaped) instead of point-in-time")
@click.option("--no-fetch", is_flag=True, help="--aligned only: skip corpus extension, use what's on disk")
def crypto_basis(symbol: str, hours: float, aligned: bool, no_fetch: bool) -> None:
    """Chainlink-vs-Binance basis distribution — the R1 decision input for per-symbol guards.

    Joins the stored Chainlink rounds against Binance 1m closes and reports
    basis_bp = (chainlink/binance - 1) * 1e4 per round. --aligned switches to
    the TWAP-vs-TWAP method (ROADMAP.md R1), which strips out the point-in-time
    method's up-to-60s timing noise, and extends the corpus itself first.
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
                           f"build it with: pmt crypto basis --aligned --symbol {sym}[/dim]\n")
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


def _refresh_oracle_corpus(symbols: list[str], since: float, now: float) -> dict[str, dict]:
    """Top the Chainlink corpus up for the symbols we're about to grade, and say so.

    Degrades on purpose: a dead Polygon RPC prints a warning and grading
    proceeds against whatever is already on disk, where outcomes.py's
    staleness guards refuse the windows the corpus can't cover. Losing a
    refresh must not cost us the wallet-graded windows in the same run.
    """
    from polymarket.chainlink import refresh_corpus

    result = refresh_corpus(symbols, since, now)
    for sym, r in result.items():
        if r["error"]:
            console.print(f"[yellow]{sym.upper():5s} oracle refresh failed ({r['error']}) — "
                          f"grading against the existing corpus[/yellow]")
    fetched = [f"{s.upper()} +{r['new']}" for s, r in result.items() if not r["error"]]
    if fetched:
        console.print(f"[dim]oracle corpus: {' · '.join(fetched)} rounds[/dim]")
    return result


def _resolution_winners(windows: list[dict], existing: dict[str, dict],
                         wallet_wins: dict[str, str], now: float) -> dict[str, str]:
    """{slug: winner} off the markets' own settled resolutions.

    One gamma round-trip per window that still needs one, and the skips are
    what keep that bounded: a slug the wallet graded this run, or whose corpus
    row is already wallet/resolution, is already at or above this source's
    rank and is left alone — so the first run backfills and later runs cost
    only the windows that closed since.

    A window inside the settlement grace is skipped rather than asked: gamma
    would honestly answer "not resolved" and we'd bake that into the corpus.
    """
    from polymarket import outcomes

    todo = [w for w in windows
            if now >= w["end"] + outcomes.RESOLUTION_GRACE_S
            and w["slug"] not in wallet_wins
            and not outcomes.is_terminal_source((existing.get(w["slug"]) or {}).get("source"))]
    if not todo:
        return {}
    console.print(f"[dim]market resolution: querying gamma for {len(todo)} window(s) ...[/dim]")
    out: dict[str, str] = {}
    unreachable = 0
    for w in todo:
        res = _gamma_resolution_cached(w["slug"])
        if res is None:
            unreachable += 1
        elif res.get("resolved") and res.get("winner"):
            out[w["slug"]] = res["winner"]
    if unreachable:
        console.print(f"[yellow]{unreachable} resolution lookup(s) failed — "
                       f"those windows fall through to chainlink/book[/yellow]")
    return out


@click.command("outcomes")
@click.option("--since", type=float, default=0.0, show_default=True,
              help="Epoch: only windows starting at/after this time")
@click.option("--out", "out_path", type=str, default=None,
              help="Outcomes file to append/update (default: ~/.pmt/corpus/outcomes.jsonl)")
@click.option("--fetch-only", is_flag=True,
              help="Refresh the Chainlink corpus for the windows in range, then stop (no grading)")
@click.option("--resolution/--no-resolution", default=True, show_default=True,
              help="Ask gamma for each ungraded window's settled outcome. One "
                   "request per window that needs one — the first run backfills, "
                   "later runs only pay for windows that closed since")
def crypto_outcomes(since: float, out_path: str | None, fetch_only: bool,
                    resolution: bool) -> None:
    """Build the validated outcomes file the replay harness needs (JSONL: slug/winner/source).

    Refreshes the Chainlink round corpus (~/.pmt/corpus/chainlink-{sym}.jsonl,
    append-only) for the symbols it is about to grade first — grading is only
    as good as the corpus, and a window that closed since the last run has no
    rounds behind it otherwise. An RPC failure warns and grades off the corpus
    already on disk; it never grades stale silently, because the staleness
    guards in polymarket.outcomes still refuse anything the corpus can't prove.

    Strict priority, strongest first: wallet redemption (Polymarket settled and
    PAID us), then the market's own resolution off gamma (what it pays redeems
    on — the exchange's answer, not ours), then Chainlink corpus inference and
    the terminal book (our reads, for windows we never touched, and refused
    outright when they can't prove they were trustworthy at settlement time).
    See polymarket.outcomes for why those guards exist and for the line between
    the sources allowed to grade a W-L and the ones that never are.
    """
    import time as _t
    from pathlib import Path

    from polymarket import chainlink as ck
    from polymarket.outcomes import (
        OUTCOMES_PATH, build_outcomes, extract_updown_slugs, load_outcomes,
        merge_outcomes, wallet_outcomes, window_universe, write_outcomes,
    )

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

    symbols = {w["symbol"] for w in windows}
    _refresh_oracle_corpus(sorted(symbols), windows[0]["start"], now)
    if fetch_only:
        return

    addr = _funder_or_usage_error()
    try:
        activity = wallet.fetch_wallet_activity(addr, windows[0]["start"])
    except Exception as e:
        console.print(f"[red]data-api unreachable: {e}[/red]")
        sys.exit(1)
    wallet_wins = wallet_outcomes(activity)

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

    existing = load_outcomes(out_file)
    resolution_by_slug = (_resolution_winners(windows, existing, wallet_wins, now)
                          if resolution else {})

    rows, dropped = build_outcomes(windows, wallet_wins, rounds_by_symbol, book_by_slug,
                                    resolution_by_slug)
    merged, added, upgraded = merge_outcomes(existing, rows)
    write_outcomes(merged, out_file)

    by_source = {s: sum(1 for r in rows if r["source"] == s)
                 for s in ("wallet", "resolution", "chainlink", "book")}
    n_up = sum(1 for r in rows if r["winner"] == "up")
    n_down = sum(1 for r in rows if r["winner"] == "down")

    t = Table(title=f"outcomes — {len(windows)} windows evaluated")
    t.add_column("source", justify="left")
    t.add_column("n", justify="right")
    for label, key in (("wallet", "wallet"), ("resolution (gamma)", "resolution"),
                        ("chainlink", "chainlink"), ("book (terminal)", "book")):
        t.add_row(label, str(by_source[key]))
    t.add_row("dropped (stale)", str(len(dropped)))
    console.print(t)
    console.print(f"[dim]{added} new · {upgraded} upgraded to a stronger source · "
                  f"winner split {n_up} up / {n_down} down[/dim]")
    console.print(f"[dim]{out_file}  ({len(merged)} total rows)[/dim]")


def _load_arms_state() -> list[dict]:
    """The engine's persisted arms, or [] if there is no store yet.

    Read-only and never fatal: the journal is a report, and an engine that has
    never armed anything is a fact about the book, not an error.
    """
    try:
        with open(tape.ARMS_STATE) as fh:
            return json.load(fh).get("arms") or []
    except (OSError, ValueError, AttributeError):
        return []


@click.command("journal")
@click.option("--since", type=float, default=None,
              help="Backfill from here instead of the high-water mark: "
                   "hours-ago if small, raw unix epoch if large, 0 for all "
                   "time. Re-runs never duplicate a line, so a backfill is "
                   "always safe to repeat")
@click.option("--show", is_flag=True, help="Tail the journal instead of adding to it")
@click.option("-n", default=20, show_default=True, help="--show: entries to tail")
@click.option("--dry-run", is_flag=True,
              help="Print the lines this run would add and write nothing")
def crypto_journal(since: float | None, show: bool, n: int, dry_run: bool) -> None:
    """Append the notable windows since the last run to the trade journal.

    Writes to ~/.pmt/journal/journal.md — a PRIVATE location, never the repo,
    because it is a running record of a real book. One terse timestamped line
    per notable event: the day's biggest win and biggest loss, a latch that
    refused a side which then lost, the first window of a new symbol or feed
    or maker bid, a streak milestone, and any size/clip change since the last
    run.

    Grading reads the WHOLE book every run (a streak milestone and a "first"
    are only true against all of it); the floor decides what gets WRITTEN, not
    what gets read. Idempotent — a re-run, or an overlapping `--since`
    backfill, adds nothing it has already said.
    """
    from polymarket import journal, outcomes, shadow

    if show:
        lines = journal.tail(n=n)
        if not lines:
            console.print(f"[dim]no journal yet — {journal.JOURNAL_PATH}[/dim]")
            return
        for ln in lines:
            console.print(journal.styled(ln), highlight=False)
        return

    now = time.time()
    state = journal.load_state()
    since_epoch = None if since is None else (_parse_since(since) if since else 0.0)
    floor = journal.floor_for(state, since_epoch, now)

    try:
        sb = _tape_scoreboard(0.0, keep_activity=True)
    except Exception as e:
        console.print(f"[red]data-api unreachable: {e}[/red]")
        sys.exit(1)
    activity = sb.pop("activity", [])
    windows = sb.get("eff_windows") or []

    # Two reads of the same file, deliberately: shadow's episode pipeline is
    # line-based and tape.iter_records is the sanctioned parser. Neither gets
    # a private copy of the other's loop for the sake of one pass.
    try:
        with open(tape.UPDOWN_TAPE) as fh:
            tape_lines = fh.readlines()
    except OSError:
        tape_lines = []
    evals = list(tape.iter_records(tape.UPDOWN_TAPE, evs={tape.EV_EVAL}))
    orders = list(tape.iter_records(tape.ORDER_TAPE))
    arms = _load_arms_state()

    # The outcomes corpus as it stands on disk — read only. `pmt crypto
    # outcomes` / `stats --gates` are what refresh it; a journal run must not
    # go and grade windows as a side effect of writing a diary.
    winners = {slug: row["winner"] for slug, row in outcomes.load_outcomes().items()
               if row.get("winner")}

    events = journal.detect(
        windows=windows, tape_lines=tape_lines, orders=orders,
        fires=list(shadow.iter_fires(tape_lines)), winners=winners,
        maker=updown_stats.maker_summary(evals, orders, activity, windows),
        arms=arms, state=state, now=now)
    written = journal.select(events, state, floor)

    for ln in journal.render_lines(written, ""):
        console.print(journal.styled(ln), highlight=False)

    if dry_run:
        console.print(f"[yellow]dry run[/yellow] — {len(written)} line(s), nothing written")
        return
    journal.append(written)
    journal.commit(state, written, now)
    journal.note_scale(state, arms)
    journal.save_state(state)
    if written:
        console.print(f"[green]{len(written)}[/green] line(s) → {journal.JOURNAL_PATH}")
    else:
        console.print("[dim]nothing notable since the last run[/dim]")


# ---------- regime: the book-only leader-persistence gauge ----------
#
# MEASUREMENT ONLY. This command computes a quantity, prints it, and appends it
# to the corpus. It gates nothing, sizes nothing and touches no arm — the
# sizing hook it exists to feed is documented DARK in docs/regime-gauge.md and
# needs its own A/B before a single dollar moves on it.
#
# The BACKFILL METHODOLOGY, frozen here rather than left to the operator's
# flags, so "the gauge since day one" means one thing:
#
#   sources    every book tape on the box — the live engine tape plus every
#              frozen `*book-tape*.jsonl` archive in the corpus, oldest first.
#              Globbed, not named: a dated snapshot is cut whenever one is
#              needed and a hard-coded list goes stale the next time.
#   scope      every updown tenor the tapes hold. The study's headline is 5m;
#              `--tenor 5m` reproduces it, and the per-series table splits by
#              tenor anyway, so pooling costs no information.
#   grading    the outcomes corpus, TERMINAL sources only. This command never
#              refreshes it (that is `pmt crypto outcomes` / `stats --gates`)
#              — a gauge that grades windows as a side effect of reporting is
#              a gauge that changes what it measures.
#   ordering   window END then slug. Reproducible from the corpus alone,
#              never from the order the estimator happened to run in.
#   writing    `--rebuild` re-cuts the whole file (the only correct move after
#              a METHOD bump, because each row's gauge state depends on every
#              row before it); the default appends the slugs the file lacks.

_REGIME_BANDS = {"strong": "green", "mixed": "yellow", "weak": "red",
                 "unknown": "dim"}
# Below this the gauge's own span is mostly ungraded, and the windows that
# grade FIRST are the ones we traded — a selection on the very axis being
# measured. Loud, not a footnote.
_COVERAGE_WARN = 0.75


def _regime_stamp(ts: float | None) -> str:
    import time as _t

    return _t.strftime("%m-%d %H:%MZ", _t.gmtime(ts)) if ts else "—"


def _regime_span(sec: float | None) -> str:
    if sec is None:
        return "—"
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _regime_cells(g: dict) -> tuple[str, str, str, str, str, str]:
    """One gauge as (n, k, persist, Wilson95, trend, band) display cells."""
    from polymarket import regime

    p = g["persist"]
    b = regime.band(p)
    pct = "—" if p is None else f"{p * 100:.1f}%"
    ci = "—" if p is None else f"[{g['lo'] * 100:.1f}, {g['hi'] * 100:.1f}]"
    if g["delta"] is None or g["arrow"] == "·":
        trend = f"[dim]· {g['prior_n']} prior[/dim]"
    else:
        z = "" if g["z"] is None else f" [dim]z {g['z']:+.2f}[/dim]"
        trend = f"{g['arrow']} {g['delta'] * 100:+.1f}pp{z}"
    return (str(g["n"]), str(g["k"]), pct, ci, trend,
            f"[{_REGIME_BANDS[b]}]{b}[/{_REGIME_BANDS[b]}]")


# Beyond this the two grading populations are not one population, and the
# headline is partly a read of our own entry filter rather than of the market.
_SELECTION_Z = 2.0


def _print_selection_check(bg: dict) -> None:
    """Persistence split by grade source, whenever the split disagrees.

    A `wallet` grade exists because we TRADED the window; a `resolution` grade
    exists whether we did or not. Wallet rows also grade FIRST, so the most
    recent slice of the gauge is its most selected slice. On this corpus the
    two disagree by 16 points (z 4.73) — big enough that quoting the headline
    without the split would be quoting the engine back at itself.
    """
    srcs = bg.get("sources") or {}
    w, r, z = srcs.get("wallet"), srcs.get("resolution"), bg.get("z")
    if not w or not r or w["persist"] is None or r["persist"] is None:
        return
    body = (f"  by grade · wallet {w['persist'] * 100:.1f}% ({w['n']}) vs "
            f"resolution {r['persist'] * 100:.1f}% ({r['n']})"
            + (f" · z {z:+.2f}" if z is not None else ""))
    if z is not None and abs(z) >= _SELECTION_Z:
        console.print(f"[yellow]{body}[/yellow]")
        console.print("[yellow]  a wallet grade exists because we TRADED that "
                      "window — that gap is our entry filter,[/yellow]")
        console.print("[yellow]  not the market, and wallet rows grade FIRST. "
                      "Weigh the recent end accordingly.[/yellow]")
    else:
        console.print(f"[dim]{body}[/dim]")


def _print_regime(est: dict, wrote: int, out_path, dry_run: bool) -> None:
    from polymarket import regime

    f, cov = est["fleet"], est["coverage"]
    n, k, pct, ci, trend, band = _regime_cells(f)
    console.print(f"\n[bold]leader persistence[/bold] [dim]· the book's leader at "
                  f"elapsed {regime.ELAPSED_MARK} held to settlement[/dim]")
    console.print(f"\n  [bold]FLEET[/bold]  {pct} {ci}  {trend}  {band}")
    console.print(f"  [dim]{k}/{n} windows · trailing {est['trail']} · "
                  f"{_regime_stamp(f['span_start'])} → "
                  f"{_regime_stamp(f['t_end'])}[/dim]")

    t = Table(box=None, pad_edge=False, padding=(0, 2))
    t.add_column("series", justify="left")
    for col in ("n", "held"):
        t.add_column(col, justify="right")
    t.add_column("persist", justify="right")
    t.add_column("Wilson95", justify="right")
    t.add_column("trend", justify="left")
    t.add_column("band", justify="left")
    for name, g in est["series"].items():
        t.add_row(name, *_regime_cells(g))
    console.print()
    console.print(t)

    skips = est["skips"]
    console.print(f"\n[dim]  {est['observations']}/{est['resolved']} graded "
                  f"windows had a leader · dropped "
                  f"{skips.get(regime.SKIP_STALE, 0)} stale, "
                  f"{skips.get(regime.SKIP_NO_LEAD, 0)} no-lead, "
                  f"{skips.get(regime.SKIP_NO_MARK, 0)} no-book, "
                  f"{skips.get(regime.SKIP_NO_PRICE, 0)} unquoted[/dim]")

    # The corpus lag is the honest headline qualifier: on a box whose grading
    # has fallen behind, the gauge is a read of the windows that graded FIRST.
    frac = cov["span_frac"]
    line = (f"  corpus ends {_regime_stamp(cov['gauge_end'])}, tape reaches "
            f"{_regime_stamp(cov['book_end'])} "
            f"({_regime_span(cov['lag_s'])} behind) · {cov['pending']} windows "
            f"await a grade")
    if frac is not None and frac < _COVERAGE_WARN:
        console.print(f"[yellow]{line}[/yellow]")
        console.print(f"[yellow]  grading covers {cov['span_graded']}/"
                      f"{cov['span_marked']} ({frac * 100:.0f}%) of this span, and "
                      f"traded windows grade FIRST —[/yellow]")
        console.print("[yellow]  a selection on the very axis being measured. "
                      "Refresh with `pmt crypto outcomes` first.[/yellow]")
    else:
        console.print(f"[dim]{line}[/dim]")

    _print_selection_check(est["by_grade"])
    console.print(f"[dim]  method {regime.METHOD}[/dim]")
    console.print("[dim]  MEASUREMENT ONLY — nothing sizes off this gauge; "
                  "the dark hook is docs/regime-gauge.md[/dim]")
    if dry_run:
        console.print(f"[yellow]  dry run[/yellow] [dim]— {wrote} row(s) would "
                      f"be written to {out_path}[/dim]")
    elif wrote:
        console.print(f"[green]  {wrote}[/green] [dim]row(s) → {out_path}[/dim]")
    else:
        console.print(f"[dim]  {out_path} already current[/dim]")


@click.command("regime")
@click.option("--trail", default=None, type=int,
              help="Windows in the trailing block (default 50)")
@click.option("--series", default=None,
              help="Only this series key prefix, e.g. `btc` or `btc 5m`")
@click.option("--tenor", default=None,
              help="Only this window duration, e.g. `5m` (the study's scope)")
@click.option("--out", "out_path", default=None,
              type=click.Path(dir_okay=False),
              help="JSONL destination (default ~/.pmt/corpus/regime.jsonl)")
@click.option("--rebuild", is_flag=True,
              help="Re-cut the whole JSONL instead of appending what's new")
@click.option("--dry-run", is_flag=True, help="Print the gauge, write nothing")
@click.option("--json", "as_json", is_flag=True, help="The estimate as JSON")
def crypto_regime(trail: int | None, series: str | None, tenor: str | None,
                  out_path: str | None, rebuild: bool, dry_run: bool,
                  as_json: bool) -> None:
    """Leader persistence — the book-only regime gauge, and its corpus row.

    Of the windows where the book had a leader at elapsed 0.25, how often did
    that leader go on to win? `pmt-alpha/analysis/underdog_search.md` §5 found
    that number moved 79.7% -> 71.5% (z 3.12) inside 24 hours, and that when it
    moved, the dog/favourite bias INVERTED in elapsed [0.00, 0.25). A binary's
    price band is a volatility position; this is the one book-only number that
    says which way that position is currently paying.

    Reads the book tapes and the outcomes corpus, prints the fleet and
    per-series gauge, and appends one row per resolved window (carrying the
    gauge as of that window) to ~/.pmt/corpus/regime.jsonl for studies to join
    against. It sizes nothing and gates nothing — see docs/regime-gauge.md.
    """
    from polymarket import regime

    trail_n = regime.TRAIL_DEFAULT if trail is None else trail
    if trail_n <= 0:
        raise click.UsageError("--trail must be positive")
    est = regime.estimate(trail=trail_n, series=series, tenor=tenor)
    rows = regime.rows_for(est["obs"], trail_n)
    dest = regime.REGIME_PATH if out_path is None else out_path
    if dry_run:
        have = {r["slug"] for r in regime.load_rows(dest)}
        wrote = len(rows) if rebuild else sum(1 for r in rows
                                              if r["slug"] not in have)
    else:
        wrote = regime.write_rows(rows, dest, rebuild=rebuild)

    if as_json:
        payload = {k: v for k, v in est.items() if k != "obs"}
        payload["written"] = wrote
        payload["out"] = str(dest)
        click.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    _print_regime(est, wrote, dest, dry_run)


# ---------- the swallowed-error log ----------

def _age_label(seconds: float) -> str:
    """`4m`, `3h`, `2d` — coarse, because this column answers "still
    happening?" and never "when exactly"."""
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


# How much of an exception message the table carries. Enough to recognise the
# failure, short enough that the count and age columns always survive an
# 80-column terminal — `--tail` and `--json` have the untruncated text.
_ERR_MSG_W = 44


@click.command("errors")
@click.option("--since", type=float, default=None,
              help="Only marks after this point: hours-ago if small, raw unix "
                   "epoch if large (default: everything on file)")
@click.option("--site", "site_filter", type=str, default=None,
              help="Only sites containing this substring")
@click.option("--trace", is_flag=True,
              help="Print the first traceback kept for each row — the frames, "
                   "which is what the header cell could never carry")
@click.option("--tail", "tail_n", type=int, default=None,
              help="Instead of the aggregate, print the last N marks in the "
                   "order they landed")
@click.option("--path", "path_override", type=str, default=None,
              help="Read a different errlog file (default ~/.pmt/engine/"
                   "swallowed-errors.jsonl, or $PMT_ERRLOG_PATH)")
@click.option("--json", "as_json", is_flag=True)
def crypto_errors(since: float | None, site_filter: str | None, trace: bool,
                  tail_n: int | None, path_override: str | None,
                  as_json: bool) -> None:
    """Errors the code caught and kept going from — what the belts swallowed.

    Most `except` handlers in this codebase are correct: a torn tape line must
    not take the dashboard down, a flaky balance call must not blank a report.
    What was wrong is that they were also SILENT — the watch header's
    `scoreboard: AttributeError` was the entire record of a failure, with no
    site, no message, no traceback and no count.

    polymarket/errlog.py fixes the silence without touching the belts: the
    first occurrence of each (site, exception type) keeps a full traceback,
    and every one after that is counted. This reads the result.

    `count` is a HIGH-WATER mark, not a number of lines — repeats are written
    on a power-of-two schedule, so a site at 4096 really did fail that many
    times. A big count on a cheap site is noise; a big count on a fetch loop
    is a loop that has quietly been dead.
    """
    import time as _t

    from polymarket import errlog

    floor = _parse_since(since) if since else 0.0
    src = path_override or errlog.path()
    records = errlog.load(src, since=floor)
    if site_filter:
        records = [r for r in records if site_filter in str(r.get("site") or "")]

    if tail_n is not None:
        records = records[-max(tail_n, 0):]
        if as_json:
            click.echo(json.dumps(records, indent=2, sort_keys=True))
            return
        if not records:
            console.print("[dim]no marks[/dim]")
            return
        for r in records:
            when = _t.strftime("%m-%d %H:%M:%S", _t.localtime(float(r.get("t") or 0)))
            n = int(r.get("n") or 1)
            console.print(f"[dim]{when}[/dim] [cyan]{r.get('site')}[/cyan] "
                          f"[red]{r.get('exc')}[/red]"
                          + (f" [dim]×{n}[/dim]" if n > 1 else "")
                          + f" {r.get('msg') or ''}")
            if trace and r.get("traceback"):
                console.print(f"[dim]{r['traceback']}[/dim]")
        return

    rows = errlog.aggregate(records)
    if as_json:
        click.echo(json.dumps(rows, indent=2, sort_keys=True))
        return
    if not rows:
        # A clean file and a missing file are the same good news, and saying
        # which one it is stops "no errors" from meaning "nothing is writing".
        exists = Path(str(src)).exists()
        console.print("[green]no swallowed errors[/green] [dim]· "
                      + ("clean" if exists else "no file yet") + f"[/dim]\n[dim]{src}[/dim]")
        return

    now = _t.time()
    t = Table(box=None, pad_edge=False, padding=(0, 1))
    # min_width, not just width: on a narrow console Rich shrinks every column
    # proportionally, and the two that must never become `…` are the count and
    # the age — "how bad" and "still happening".
    t.add_column("count", justify="right", width=7, min_width=7, no_wrap=True)
    t.add_column("age", justify="right", width=11, min_width=11, no_wrap=True)
    # The two identity columns flex: a narrow terminal shortens a site name,
    # which is legible. Fixed widths here add past 80 and Rich answers by
    # dropping whole columns, starting with the count — the one number this
    # report exists to show.
    t.add_column("site", justify="left", overflow="ellipsis", no_wrap=True)
    t.add_column("exception", justify="left", overflow="ellipsis", no_wrap=True)
    # No `ratio` anywhere: a ratio column claims ALL the slack and Rich pays for
    # it by squeezing the fixed ones down to ellipses — the count first, which
    # is the number this report exists to show. The message is truncated in
    # Python instead, so the table sizes to its content.
    t.add_column("message", justify="left", overflow="ellipsis", no_wrap=True)
    for r in rows:
        n = int(r["n"])
        # first→last, because "started an hour ago, last seen 2s ago" and
        # "started an hour ago, last seen an hour ago" are opposite situations.
        span = (f"{_age_label(now - float(r['first_t'] or now))}"
                f"→{_age_label(now - float(r['last_t'] or now))}")
        t.add_row(f"[{'red' if n > 1 else 'yellow'}]{n:,}[/]",
                  f"[dim]{span}[/dim]",
                  f"[cyan]{r['site']}[/cyan]", f"[red]{r['exc']}[/red]",
                  f"[dim]{str(r['msg'])[:_ERR_MSG_W]}[/dim]")
    console.print(t)
    if trace:
        for r in rows:
            if not r.get("traceback"):
                continue
            console.print(f"\n[cyan]{r['site']}[/cyan] [dim]· first occurrence[/dim]")
            console.print(f"[dim]{r['traceback']}[/dim]")
