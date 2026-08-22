"""ESPN's keyless site API: live scores, game state, and win probability.

Unofficial but long-stable (site.api.espn.com). Win probability comes from
ESPN's own in-game model — the reference we price Polymarket sports books
against. No auth; be a polite client and cache upstream of this if polling.
"""

from __future__ import annotations

import requests

BASE = "https://site.api.espn.com/apis/site/v2/sports"
TIMEOUT = 15
# ESPN 403s non-browser UAs (same trick as lb-api)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

# league key -> ESPN (sport, league) path segments
LEAGUES = {
    "mlb": ("baseball", "mlb"),
    "nfl": ("football", "nfl"),
    "nba": ("basketball", "nba"),
    "nhl": ("hockey", "nhl"),
    "wnba": ("basketball", "wnba"),
    "ncaaf": ("football", "college-football"),
    "ncaab": ("basketball", "mens-college-basketball"),
    "mls": ("soccer", "usa.1"),
    "epl": ("soccer", "eng.1"),
}


def _league_path(league: str) -> str:
    try:
        sport, lg = LEAGUES[league.lower()]
    except KeyError:
        raise ValueError(f"Unknown league '{league}'. Known: {', '.join(sorted(LEAGUES))}")
    return f"{BASE}/{sport}/{lg}"


def _get(url: str, params: dict | None = None) -> dict:
    r = requests.get(url, params=params or {}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json() or {}


def scoreboard(league: str, date: str | None = None) -> list[dict]:
    """All games for a league/date (YYYYMMDD; default today, ESPN's clock).

    Rows: event_id, name, short_name, start (ISO), state (pre/in/post),
    detail, and per-team abbrev/score/home flags.
    """
    params = {"dates": date} if date else None
    data = _get(f"{_league_path(league)}/scoreboard", params)
    rows = []
    for ev in data.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        status = (comp.get("status") or {}).get("type") or {}
        teams = [
            {
                "abbrev": (c.get("team") or {}).get("abbreviation"),
                "name": (c.get("team") or {}).get("displayName"),
                "score": c.get("score"),
                "home": c.get("homeAway") == "home",
            }
            for c in comp.get("competitors") or []
        ]
        rows.append({
            "event_id": ev.get("id"),
            "name": ev.get("name"),
            "short_name": ev.get("shortName"),
            "start": ev.get("date"),
            "state": status.get("state"),
            "detail": status.get("detail"),
            "teams": teams,
        })
    return rows


def game_state(league: str, event_id: str) -> dict:
    """Live state for one game: score, situation, ESPN win prob, book odds."""
    data = _get(f"{_league_path(league)}/summary", {"event": event_id})
    comp = ((data.get("header") or {}).get("competitions") or [{}])[0]
    status = (comp.get("status") or {}).get("type") or {}

    teams = {}
    for c in comp.get("competitors") or []:
        side = "home" if c.get("homeAway") == "home" else "away"
        teams[side] = {
            "abbrev": (c.get("team") or {}).get("abbreviation"),
            "name": (c.get("team") or {}).get("displayName"),
            "score": c.get("score"),
            "record": next((r.get("summary") for r in c.get("record") or []), None),
        }

    # last entry of the winprobability series = current model estimate
    wp = data.get("winprobability") or []
    home_wp = wp[-1].get("homeWinPercentage") if wp else None

    odds = []
    for o in data.get("pickcenter") or []:
        odds.append({
            "book": (o.get("provider") or {}).get("name"),
            "spread": o.get("spread"),
            "over_under": o.get("overUnder"),
            "home_ml": o.get("homeMoneyLine"),
            "away_ml": o.get("awayMoneyLine"),
        })

    situation = data.get("situation") or {}
    return {
        "event_id": event_id,
        "state": status.get("state"),
        "detail": status.get("detail"),
        "teams": teams,
        "home_win_prob": home_wp,
        "away_win_prob": (1 - home_wp) if isinstance(home_wp, (int, float)) else None,
        "situation": {
            k: situation.get(k)
            for k in ("balls", "strikes", "outs", "onFirst", "onSecond", "onThird",
                      "possession", "downDistanceText", "lastPlay")
            if situation.get(k) is not None
        },
        "odds": odds,
    }


def find_games(league: str, team: str, date: str | None = None) -> list[dict]:
    """Scoreboard rows whose name/teams match `team` (case-insensitive substring)."""
    q = team.lower()
    return [
        g for g in scoreboard(league, date)
        if q in (g.get("name") or "").lower()
        or any(q in (t.get("abbrev") or "").lower() or q in (t.get("name") or "").lower()
               for t in g.get("teams") or [])
    ]
