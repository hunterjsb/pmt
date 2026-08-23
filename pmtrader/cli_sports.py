"""`pmt sports ...` — ESPN reference data for pricing sports markets.

Split out of cli.py for size and registered onto the top-level `cli` group by
cli.py, the same way cli_crypto's group attaches. Whole group is flagged for
removal; polymarket/espn.py + gamewatch.py go with it if it goes.
"""

from __future__ import annotations

import click
from rich.table import Table

from cli_common import console, _deprecated


@click.group("sports", short_help="DEPRECATED — live sports data: scores, game state, ESPN win prob.")
@_deprecated(
    "The whole group — board, game, watch — was built on 2026-08-22 and has "
    "no reference outside this module: not CLAUDE.md, not either README, not "
    "ROADMAP, not one analysis script. All three still work against ESPN. "
    "polymarket/espn.py + gamewatch.py go with it if it goes."
)
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
