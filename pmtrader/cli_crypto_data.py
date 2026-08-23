"""Read-only `pmt crypto` reports over the wallet, the decision tape, and the
Chainlink corpus: `tape`, `activity`, `window`, `basis`, `outcomes`, `journal`.

Nothing here posts to the engine or places an order — these are the commands
you reach for AFTER something happened, to find out what. `outcomes` and
`journal` do write to disk, but only to the local corpus and the private
journal, never to the repo and never to the book.

Grading and wallet acquisition are not defined here: they come from
cli_crypto_stats, which is the one place that owns them.
"""

from __future__ import annotations

import json
import sys
import time

import click
from rich.table import Table

from cli_common import _parse_since, _pnl_color, console
from cli_crypto_stats import _gamma_resolution_cached, _tape_scoreboard
from polymarket import tape, updown_slugs, updown_stats, wallet
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
