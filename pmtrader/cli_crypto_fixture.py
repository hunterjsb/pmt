"""`pmt crypto fixture` — freeze one wallet-graded window into a committed
characterization fixture for pmengine's replay harness.

Reads the local corpus only: no network, and it never writes to a tape. The
window must be wallet-graded, because a fixture is the ground truth other
measurements get checked against (docs/LESSONS.md#L36). See
the fixtures README (in the pmt-strategies submodule) for what a fixture is and
what a failing one means.

Its own module because it is a build tool with a build tool's dependencies —
the pmengine binary, the repo layout, a secret scan — that no reporting
command shares.
"""

from __future__ import annotations

import json
import sys

import click

from cli_common import _pnl_color, console
from polymarket import tape, updown_slugs


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


def _pmengine_binary(explicit: str | None):
    """The engine binary that blesses a fixture. Release first — a debug
    build is 20x slower and a full-mode fixture replays 240 book ticks."""
    from pathlib import Path

    if explicit:
        return Path(explicit)
    base = _repo_root() / "pmengine" / "target"
    for build in ("release", "debug"):
        cand = base / build / "pmengine"
        if cand.exists():
            return cand
    raise click.UsageError(
        "no pmengine binary found — build one with "
        "`(cd pmengine && cargo build --release --features ec2)` or pass --engine"
    )


def _corpus_jsonl(name: str) -> list[dict]:
    from pathlib import Path

    path = Path.home() / ".pmt" / "corpus" / name
    out: list[dict] = []
    try:
        fh = open(path)
    except FileNotFoundError:
        raise click.UsageError(f"{path} not found — the fixture freezer reads the local corpus only")
    with fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _rtds_corpus_rows() -> list[dict]:
    """Every recorder row under ~/.pmt/corpus/rtds. Read whole because a
    window's lookback can cross a daily rotation."""
    from pathlib import Path

    d = Path.home() / ".pmt" / "corpus" / "rtds"
    files = sorted(d.glob("rtds-*.jsonl")) if d.is_dir() else []
    if not files:
        raise click.UsageError(
            f"{d}: no rtds-*.jsonl recorder files — a stream-fed window's market "
            f"data exists nowhere else, the feed serves no history"
        )
    out: list[dict] = []
    for path in files:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def _rtds_gap(coverage: tuple[float, float] | None, symbol: str,
              start: int, end: int) -> str | None:
    """Why this window cannot be frozen full-mode off the corpus, or None.

    Mirrors replay/rtds.rs's own refusal: the slice has to reach the
    settlement reference printed at `start` and run through the close.
    """
    if coverage is None:
        return (f"the RTDS corpus carries no {symbol} samples — full mode rebuilds "
                f"the model from the settlement stream this window traded on")
    first, last = coverage
    if first > start or last < end:
        return (f"the RTDS corpus covers {first:.0f}..{last:.0f} but this window needs "
                f"{start}..{end} (its settlement reference through the close) — short by "
                f"{max(first - start, 0):.0f}s at the start and {max(end - last, 0):.0f}s "
                f"at the end")
    return None


@click.command("fixture")
@click.argument("slug")
@click.option("--out", "out_dir", default=None,
              help="Fixture directory (default: pmengine/src/strategies/private/fixtures"
                   " — the pmt-strategies submodule; fixtures embed as-armed params"
                   " and must never land in the public repo)")
@click.option("--mode", type=click.Choice(["auto", "evals", "full"]), default="auto",
              show_default=True,
              help="auto = full when the book tape and klines cover the window")
@click.option("--teaches", default=None, help="One line on why this window earns a permanent slot")
@click.option("--lesson", "lessons_ref", default=None, help="e.g. docs/LESSONS.md#L13")
@click.option("--era", "eras", multiple=True, help="Era tag, repeatable (pre-brake, post-theta, ...)")
@click.option("--invariant", "invariants", multiple=True,
              help="Declared check, repeatable (fires_eq:N, no_fire_before_t:T, "
                   "all_fires_side:up, all_fires_mode:safe, pnl_sign:neg, "
                   "max_committed_le:N, gated_ticks_ge:N, sim_notional_ge_wallet)")
@click.option("--param", "param_overrides", multiple=True, metavar="KEY=VALUE",
              help="Pin an as-armed param the tape cannot prove; recorded as an operator override")
@click.option("--lifted-tunables", is_flag=True,
              help="Reproduce the pre-brake engine (distrust/avg-down lifted)")
@click.option("--note", default=None,
              help="Curator note recorded in provenance — say where a --param came from")
@click.option("--regen", is_flag=True,
              help="Overwrite an existing fixture. DELIBERATE: the commit message must say "
                   "which expectations moved and what moved them")
@click.option("--refresh", is_flag=True,
              help="Re-walk the wallet activity dump before grading (the one network call "
                   "this command will make). Needed for any window newer than the dump")
@click.option("--accounting-only", is_flag=True,
              help="Repair ONLY the outcome money block of an existing fixture, leaving "
                   "params, slices and expectations byte-identical. The correct repair for "
                   "a fixture frozen against a stale activity dump — a full --regen would "
                   "also re-derive as-armed params from TODAY's arm store")
@click.option("--no-bless", is_flag=True, help="Write the fixture without expectations")
@click.option("--engine", default=None, help="Path to the pmengine binary")
def crypto_fixture(slug: str, out_dir: str | None, mode: str, teaches: str | None,
                   lessons_ref: str | None, eras: tuple[str, ...],
                   invariants: tuple[str, ...], param_overrides: tuple[str, ...],
                   lifted_tunables: bool, note: str | None, regen: bool, refresh: bool,
                   accounting_only: bool, no_bless: bool, engine: str | None) -> None:
    """Freeze ONE wallet-graded window into a committed characterization fixture.

    Reads the local corpus only — no network unless `--refresh` is passed — and
    never writes to a tape. The window must be wallet-graded: a fixture is the
    ground truth other measurements get checked against, so a chainlink/book-
    derived label is refused rather than downgraded (docs/LESSONS.md#L36).
    """
    import subprocess
    import time as _t
    from pathlib import Path

    from polymarket import fixtures as fx
    from polymarket import wallet

    parsed = updown_slugs.parse_updown_slug(slug)
    if parsed is None:
        raise click.UsageError(f"not an updown slug: {slug!r}")
    start, end = parsed["start"], parsed["end"]

    # Default into the pmt-strategies submodule mount: a fresh capture embeds
    # the arm's as-armed params — alpha that must never land in public pmt.
    out_path = (
        Path(out_dir)
        if out_dir
        else _repo_root() / "pmengine" / "src" / "strategies" / "private" / "fixtures"
    )
    out_path.mkdir(parents=True, exist_ok=True)
    dest = out_path / f"{slug}.json"
    prior: dict = {}
    if dest.exists():
        if not (regen or accounting_only):
            raise click.UsageError(
                f"{dest} already exists. Re-freezing rewrites an expectation that a real "
                f"trade is pinned to — pass --regen and say in the commit message what moved it. "
                f"To fix ONLY a stale money block, --accounting-only leaves expectations alone."
            )
        # Carry the existing curation forward so a bare --regen re-cuts the
        # slice and re-blesses WITHOUT quietly dropping anything: the old
        # expectations so bless can diff against them (a regen that printed
        # "first expectations" would hide exactly what moved), and the
        # declared invariants so omitting a flag cannot delete curator intent.
        prior = json.loads(dest.read_text())

    if refresh:
        try:
            written = wallet.refresh_activity_dump()
        except Exception as e:
            raise click.UsageError(f"activity refresh failed: {e}")
        click.echo(f"refreshed activity dump: {written} rows")

    graded = next((r for r in _corpus_jsonl("outcomes.jsonl") if r.get("slug") == slug), None)
    try:
        # `end` makes a dump that stops before this window fatal rather than a
        # silent row of zeros — the defect that put $0 buy/$0 redeem/$0 pnl on
        # seven fixtures of windows that really traded.
        acct = fx.wallet_accounting(_corpus_jsonl("activity.jsonl"), slug, window_end=end)
        outcome = fx.build_outcome(graded, acct, slug)
    except fx.FixtureError as e:
        raise click.UsageError(str(e))

    if accounting_only:
        # WHY THIS EXISTS, and why it is not just --regen.
        #
        # A full regen re-derives the as-armed params from the LIVE arm store,
        # which describes the arms running NOW. For a window whose arm retired
        # hours ago that silently stamps today's configuration onto yesterday's
        # trade — measured on xrp-updown-5m-1787485200: maker_bid False -> True,
        # settle_tw_s None -> 60.0, and sigma_bp_per_min drifting with a re-cut
        # slice. Those params feed the basis guard, so the blessed expectations
        # move too (fires 2 -> 0 on that fixture) and a passing regression
        # baseline gets rewritten to match a fiction.
        #
        # The money is the only thing that was ever wrong. Repair exactly that.
        if not prior:
            raise click.UsageError(
                f"{dest} does not exist — --accounting-only repairs an existing fixture")
        before = prior.get("outcome", {})
        money = {k: outcome[k] for k in fx.OUTCOME_KEYS[2:]}
        if before.get("winner") != outcome["winner"]:
            raise click.UsageError(
                f"{slug}: graded winner moved {before.get('winner')!r} -> "
                f"{outcome['winner']!r}. That is a grading change, not an accounting "
                f"repair — investigate before touching the fixture.")
        moved = {k: (before.get(k), v) for k, v in money.items() if before.get(k) != v}
        if not moved:
            console.print(f"[dim]{slug}: accounting already matches the dump — unchanged[/dim]")
            return
        # A textual swap of the one block, so the commit is a diff of the money
        # and literally nothing else — no key reordering, no float reformatting
        # of the recorded eval/book/rtds slices.
        dest.write_text(
            fx.replace_top_level_block(dest.read_text(), "outcome", {**before, **money}))
        console.print(f"[green]repaired accounting[/green] {dest}")
        for k, (was, now) in sorted(moved.items()):
            console.print(f"  - {k}: {was!r} -> {now!r}")

        # Prove the surgical edit did not disturb the regression baseline: a
        # plain replay, no --bless, so a moved expectation is a failure rather
        # than a rewrite.
        binary = _pmengine_binary(engine)
        proc = subprocess.run(
            [str(binary), "--log-level", "warn", "replay", "--fixtures", str(dest)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            console.print(proc.stdout + proc.stderr)
            raise click.UsageError(
                f"{slug}: replay FAILED after an accounting-only repair. The money block "
                f"is not supposed to move an expectation — this is a finding.")
        console.print(f"  [green]replay still passes[/green] — expectations untouched")
        return

    # Slices are only needed to BUILD a fixture; --accounting-only returned above
    # without reading a tape, so an old window whose tape has since rotated away
    # can still have its money repaired.
    tape_recs = fx.slice_tape(tape.iter_records(tape.UPDOWN_TAPE), slug)
    if not tape_recs:
        raise click.UsageError(f"{slug}: no records in {tape.UPDOWN_TAPE}")
    book_recs = [fx.trim_book_record(r)
                 for r in fx.slice_tape(tape.iter_records(tape.BOOK_TAPE), slug)]

    symbol = fx.SYMBOL.get(slug.split("-")[0], "")

    # The arm store decides which market data this window even HAS: a
    # stream-fed arm never read a kline, and a Binance arm has no place in
    # the recorder corpus. Resolved before the mode decision for that reason.
    arms_path = Path(tape.ARMS_STATE)
    try:
        live = {a["symbol"]: a for a in json.loads(arms_path.read_text())["arms"]}
    except (OSError, ValueError, KeyError) as e:
        raise click.UsageError(f"{arms_path}: {e}")
    live_arm = live.get(symbol) or next(iter(live.values()))
    feed = (prior.get("params", {}).get("feed")
            or live_arm.get("feed") or "binance")

    klines: list[dict] = []
    rtds_recs: list[dict] = []
    missing: list[int] = []
    if feed == "rtds":
        rtds_symbol = fx.rtds_symbol(symbol)
        if not rtds_symbol:
            raise click.UsageError(f"{slug}: the RTDS stream does not carry {symbol}")
        rtds_recs, coverage = fx.rtds_slice(
            _rtds_corpus_rows(), rtds_symbol, start, end)
        rtds_gap = _rtds_gap(coverage, rtds_symbol, start, end)
    else:
        klines, missing = fx.kline_slice(
            _corpus_jsonl(f"klines-1m-{symbol}.jsonl"), start, end)
        rtds_gap = None

    if mode == "auto":
        ready = not rtds_gap if feed == "rtds" else not missing
        mode = "full" if book_recs and ready else "evals"
    if mode == "full":
        if not book_recs:
            raise click.UsageError(
                f"{slug}: no book records — this window predates the book recorder "
                f"(02:45:20Z on 2026-08-23) and can only be frozen as --mode evals"
            )
        if feed == "rtds":
            if rtds_gap:
                raise click.UsageError(f"{slug}: {rtds_gap}")
        elif missing:
            raise click.UsageError(
                f"{slug}: kline cache is missing {len(missing)} minute(s) — full mode "
                f"rebuilds the model from them and a fixture may never fetch"
            )
    else:
        klines, rtds_recs = [], []
    series = updown_slugs.series_key(parsed["symbol"], parsed["dur_s"])
    series_roll = _series_first_roll(series)
    overrides = dict(_parse_override(o) for o in param_overrides)
    try:
        params, prov = fx.build_params(slug, tape_recs, live_arm, series_roll,
                                       overrides, lifted_tunables)
    except fx.FixtureError as e:
        raise click.UsageError(str(e))

    provenance = {
        "frozen_at": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
        "evals": {"source": tape.UPDOWN_TAPE, "records": len(tape_recs),
                  "sha256": fx.sha256_records(tape_recs)},
        "book": ({"source": tape.BOOK_TAPE, "records": len(book_recs),
                  "sha256": fx.sha256_records(book_recs)} if book_recs else None),
        "klines": ({"source": f"~/.pmt/corpus/klines-1m-{symbol}.jsonl",
                    "records": len(klines), "sha256": fx.sha256_records(klines)}
                   if klines else None),
        # The stream serves no history, so unlike klines this slice can
        # never be re-cut from anywhere. `lookback_s` says how far back the
        # hub was warmed, because rho and the slow sigma depend on it.
        "rtds": ({"source": "~/.pmt/corpus/rtds/rtds-*.jsonl",
                  "symbol": fx.rtds_symbol(symbol),
                  "records": len(rtds_recs),
                  "lookback_s": fx.RTDS_LOOKBACK_S,
                  "sha256": fx.sha256_records(rtds_recs)}
                 if rtds_recs else None),
        "outcome": {"source": "~/.pmt/corpus/outcomes.jsonl + activity.jsonl",
                    "graded": "wallet"},
        # Which structured fields this slice's tape generation carries. An
        # expectation only means something against the schema that was live
        # when the window ran (issue #5, gap 3).
        "tape_schema": _tape_schema(tape_recs),
    }
    if note or prior.get("provenance", {}).get("curator_note"):
        provenance["curator_note"] = note or prior["provenance"]["curator_note"]

    fixture = fx.build_fixture(
        slug, mode, params, prov, outcome, tape_recs,
        book_recs if mode == "full" else [], klines,
        teaches or prior.get("teaches") or "TODO: one line on why this window earns a permanent slot",
        lessons_ref or prior.get("lessons_ref"),
        list(eras) or prior.get("era") or [],
        list(invariants) or prior.get("invariants") or [],
        provenance, prior.get("expect"), rtds_recs,
    )

    rendered = fx.render_fixture(fixture)
    needles = [v for v in (_env_secret("PM_FUNDER_ADDRESS"), _env_secret("PM_PRIVATE_KEY")) if v]
    hits = fx.secret_scan(rendered, needles)
    if hits:
        raise click.UsageError(
            f"{slug}: refusing to write — the slice carries {len(hits)} value(s) that "
            f"must not be committed: {hits[:5]}"
        )
    dest.write_text(rendered)
    console.print(f"[green]wrote[/green] {dest}  mode={mode} "
                  f"evals={len(tape_recs)} book={len(book_recs) if mode == 'full' else 0} "
                  f"klines={len(klines)}  winner={outcome['winner']} "
                  f"wallet P&L [{_pnl_color(outcome['pnl'])}]{outcome['pnl']:+,.2f}[/]")

    if no_bless:
        console.print("[yellow]no expectations written[/yellow] — the fixture cannot pass a run "
                      "until it is blessed")
        return
    binary = _pmengine_binary(engine)
    run = lambda *extra: subprocess.run(
        [str(binary), "--log-level", "warn", "replay", "--fixtures", str(dest), *extra],
        capture_output=True, text=True)
    proc = run("--bless")
    if proc.returncode != 0:
        if not regen:
            dest.unlink(missing_ok=True)
        console.print(proc.stdout + proc.stderr)
        raise click.UsageError(f"{slug}: bless failed — fixture not written")
    click.echo(proc.stdout.strip())

    # The hand-check the mission asks for, printed rather than assumed: the
    # sim's fires/notional beside what the window ACTUALLY did. They will not
    # match exactly (instant fills, no partials, no queue) — a wild gap is
    # the signal that a reconstructed param is wrong for this window's era.
    verify = run()
    report = next((json.loads(l) for l in verify.stdout.splitlines()
                   if l.startswith("{")), None)
    if report:
        sim, real = report["sim"], report["real"]
        console.print(
            f"  sim {sim['fires']} fire(s) ${sim['notional']:,.2f} pnl "
            f"[{_pnl_color(sim['pnl'] or 0)}]{(sim['pnl'] or 0):+,.2f}[/]  vs  "
            f"live {real['fires']} fire(s) ${real['notional']:,.2f} · "
            f"wallet spent ${outcome['buy']:,.2f} pnl "
            f"[{_pnl_color(outcome['pnl'])}]{outcome['pnl']:+,.2f}[/]"
        )
    if verify.returncode != 0:
        # Bless writes the generated expectations and never the DECLARED
        # invariants, so a fresh fixture can fail its own curator intent. That
        # is a curation error: loud, non-zero, and the file is left on disk to
        # be looked at.
        console.print("[red]declared invariants do not hold on this window:[/red]")
        console.print(verify.stdout + verify.stderr)
        sys.exit(1)


def _parse_override(raw: str) -> tuple[str, object]:
    if "=" not in raw:
        raise click.UsageError(f"--param wants KEY=VALUE, got {raw!r}")
    k, v = raw.split("=", 1)
    try:
        return k, json.loads(v)
    except ValueError:
        return k, v


def _env_secret(name: str) -> str | None:
    import os

    v = os.environ.get(name)
    return v if v and len(v) >= 8 else None


def _series_first_roll(series: str) -> float | None:
    """The earliest `roll` size in a slug's series — the budget fallback for
    a window whose own roll record rolled off the tape."""
    best: tuple[float, float] | None = None
    for r in tape.iter_records(tape.UPDOWN_TAPE, evs={tape.EV_ROLL}):
        parsed = updown_slugs.parse(r.get("slug", ""))
        if parsed is None or parsed[4] != series:
            continue
        t = r.get("t", 0.0)
        if best is None or t < best[0]:
            best = (t, r.get("size"))
    return best[1] if best else None


def _tape_schema(recs: list[dict]) -> dict:
    """Which structured fields this slice's records actually carry. The tape
    grew fields mid-corpus (margin/banked/cushion, then guard_bp, then the
    gated numerics), and a fixture's expectations are only meaningful against
    the generation present in ITS slice."""
    evals = [r for r in recs if r.get("ev") == tape.EV_EVAL]
    gated = [r for r in recs if r.get("ev") == tape.EV_GATED]
    fires = [r for r in recs if r.get("ev") == tape.EV_FIRE]
    has = lambda rows, k: bool(rows) and all(k in r for r in rows)
    return {
        "eval_margin_fields": has(evals, "margin_bp"),
        "eval_guard_bp": has(evals, "guard_bp"),
        "gated_structured": has(gated, "margin_bp"),
        # The last unstructured gate number: the staleness gate's spot age,
        # which lived in prose (or, on rtds, inside a nested error string)
        # until it became a field.
        "gated_spot_age": has(gated, "spot_age_s"),
        # The marketable limit actually submitted. False = this slice cannot
        # prove a pay-up chase and its pay_up_max is 0 by necessity, not by
        # measurement (fixtures/README.md gap 2).
        "fire_limit": has(fires, "limit"),
    }
