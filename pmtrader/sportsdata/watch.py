"""Live game↔market watcher: ESPN game state vs Polymarket moneyline books.

One full-screen dashboard: score + ESPN win prob, both books in real time
(CLOB websocket), and the microstructure story — how far and how fast the
market moves after each game event. Signed reaction latency: positive means
the market lagged our ESPN feed, negative means traders beat it (i.e. the
edge window we thought we had doesn't exist). Every tick logs to JSONL for
post-hoc strategy research.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from polymarket import hosts
from . import espn

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WP_EVENT_THRESH = 0.015   # ESPN wp jump that counts as a game event
MOVE_THRESH = 0.005       # vig-free prob change that counts as a market move
SPARK_CHARS = "▁▂▃▄▅▆▇█"


# ============================================================
# Market resolution: ESPN game -> Polymarket moneyline tokens
# ============================================================


def resolve_moneyline(league: str, ref: str, slug: str | None = None) -> dict:
    """Find the game on ESPN and its moneyline market on Polymarket.

    Slug convention observed live: {league}-{away}-{home}-{date}, ESPN
    abbrevs lowercased, date = game date in US/Eastern (UTC date is wrong
    for night games).
    """
    games = espn.scoreboard(league)
    game = next((g for g in games if g["event_id"] == ref), None)
    if game is None:
        matches = espn.find_games(league, ref)
        if not matches:
            raise ValueError(f"no {league} game matching '{ref}' today")
        game = matches[0]

    home = next(t for t in game["teams"] if t["home"])
    away = next(t for t in game["teams"] if not t["home"])
    if not slug:
        et_date = (
            datetime.fromisoformat(game["start"].replace("Z", "+00:00"))
            .astimezone(ZoneInfo("America/New_York"))
            .strftime("%Y-%m-%d")
        )
        slug = f"{league.lower()}-{away['abbrev'].lower()}-{home['abbrev'].lower()}-{et_date}"

    r = requests.get(
        f"{hosts.GAMMA}/events", params={"slug": slug}, headers=hosts.UA, timeout=15
    )
    r.raise_for_status()
    events = r.json() or []
    if not events:
        raise ValueError(f"no Polymarket event for slug '{slug}' (override with --slug)")
    ml = next(
        (m for m in events[0]["markets"] if m.get("sportsMarketType") == "moneyline"),
        None,
    )
    if ml is None:
        raise ValueError(f"event '{slug}' has no moneyline market")

    outcomes = json.loads(ml["outcomes"])
    tokens = json.loads(ml["clobTokenIds"])
    by_outcome = dict(zip(outcomes, tokens))

    def token_for(team: dict) -> str:
        for name, tok in by_outcome.items():
            if name == team["name"] or name.split()[-1] in (team["name"] or ""):
                return tok
        raise ValueError(f"can't map {team['name']} to outcomes {outcomes}")

    return {
        "game": game,
        "slug": slug,
        "home": {**home, "token": token_for(home)},
        "away": {**away, "token": token_for(away)},
    }


# ============================================================
# Stats: correlation, lead/lag, reaction latency (pure fns)
# ============================================================


def resample(series, t0: float, t1: float, step: float) -> list[float | None]:
    """Last-known value on a fixed grid; None until first observation."""
    out, i, last = [], 0, None
    pts = list(series)
    t = t0
    while t <= t1:
        while i < len(pts) and pts[i][0] <= t:
            last = pts[i][1]
            i += 1
        out.append(last)
        t += step
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _delta_grid(series, t0, t1, step=1.0, span=5):
    vals = resample(series, t0, t1, step)
    return [
        (b - a) if a is not None and b is not None else None
        for a, b in zip(vals, vals[span:])
    ]


def corr_and_lag(espn_series, mkt_series, max_lag: int = 30) -> dict:
    """Pearson r of 5s deltas at lag 0, plus the lag (s) that maximizes |r|.

    Positive best_lag = market moves AFTER ESPN by that many seconds.
    """
    if len(espn_series) < 5 or len(mkt_series) < 5:
        return {"r": None, "best_lag": None, "best_r": None}
    t0 = max(espn_series[0][0], mkt_series[0][0])
    t1 = min(espn_series[-1][0], mkt_series[-1][0])
    if t1 - t0 < 60:
        return {"r": None, "best_lag": None, "best_r": None}
    de = _delta_grid(espn_series, t0, t1)
    dm = _delta_grid(mkt_series, t0, t1)

    def r_at(lag: int) -> float | None:
        # align so the market series is shifted `lag` seconds later than espn
        if lag >= 0:
            pairs = [(e, m) for e, m in zip(de, dm[lag:]) if e is not None and m is not None]
        else:
            pairs = [(e, m) for e, m in zip(de[-lag:], dm) if e is not None and m is not None]
        if len(pairs) < 30:
            return None
        return pearson([p[0] for p in pairs], [p[1] for p in pairs])

    r0 = r_at(0)
    best_lag, best_r = None, None
    for lag in range(-max_lag, max_lag + 1):
        r = r_at(lag)
        if r is not None and (best_r is None or abs(r) > abs(best_r)):
            best_lag, best_r = lag, r
    return {"r": r0, "best_lag": best_lag, "best_r": best_r}


def match_latencies(
    events: list[dict], moves: list[dict], max_wait: float = 120.0, lead: float = 20.0
) -> dict:
    """Pair each game event with the market's same-direction reaction.

    A same-sign move up to `lead` s BEFORE the event means the market led
    (negative latency — traders saw it before our ESPN poll did).
    """
    lats, led, unmatched = [], 0, 0
    for e in events:
        d = e.get("d")
        if not d:
            continue
        sign = 1 if d > 0 else -1
        before = [
            m for m in moves
            if 0 < e["mono"] - m["mono"] <= lead and (1 if m["d"] > 0 else -1) == sign
        ]
        after = [
            m for m in moves
            if 0 <= m["mono"] - e["mono"] <= max_wait and (1 if m["d"] > 0 else -1) == sign
        ]
        if before:
            lats.append(before[-1]["mono"] - e["mono"])  # negative
            led += 1
        elif after:
            lats.append(after[0]["mono"] - e["mono"])
        else:
            unmatched += 1
    lats.sort()
    med = lats[len(lats) // 2] if lats else None
    return {
        "n": len(lats),
        "median": med,
        "min": lats[0] if lats else None,
        "max": lats[-1] if lats else None,
        "led": led,
        "unmatched": unmatched,
    }


def sparkline(series, width: int = 56, window: float = 900.0) -> str:
    now = time.monotonic()
    pts = [(t, v) for t, v in series if now - t <= window]
    if len(pts) < 2:
        return "·" * width
    vals = resample(pts, now - window, now, window / width)[:width]
    known = [v for v in vals if v is not None]
    if not known:  # all points newer than the last grid bucket
        return "·" * width
    lo, hi = min(known), max(known)
    rng = (hi - lo) or 1e-9
    return "".join(
        " " if v is None else SPARK_CHARS[int((v - lo) / rng * (len(SPARK_CHARS) - 1))]
        for v in vals
    )


# ============================================================
# Shared state + feed threads
# ============================================================


def fetch_positions(resolved: dict) -> list[dict]:
    """Live holdings on the two moneyline tokens, straight from the data API —
    no --pos flag needed. Empty when PM_FUNDER_ADDRESS isn't configured."""
    from polymarket.env import load_project_env

    load_project_env()
    addr = os.environ.get("PM_FUNDER_ADDRESS")
    if not addr:
        return []
    r = requests.get(
        f"{hosts.DATA}/positions",
        params={"user": addr, "limit": 200},
        headers=hosts.UA,
        timeout=10,
    )
    r.raise_for_status()
    by_token = {resolved[s]["token"]: s for s in ("home", "away")}
    out = []
    for p in r.json() or []:
        side = by_token.get(str(p.get("asset")))
        if side and float(p.get("size") or 0) > 0:
            out.append({
                "side": side,
                "size": float(p["size"]),
                "avg": float(p.get("avgPrice") or 0),
            })
    return out


class WatchState:
    def __init__(self, resolved: dict, log_path: Path | None):
        self.lock = threading.Lock()
        self.resolved = resolved
        self.positions: list[dict] = []
        self.pos_manual = False
        self.game: dict = {}
        self.espn_rtt = None
        self.espn_wp = deque(maxlen=21600)     # (mono, home_wp)
        self.mkt_prob = deque(maxlen=86400)    # (mono, vig-free home prob)
        self.books = {}                        # token -> {bb, ba, bids{}, asks{}, last_trade}
        self.events = deque(maxlen=500)        # game events
        self.moves = deque(maxlen=2000)        # market moves
        self.feed_log = deque(maxlen=14)       # display tail (events+moves merged)
        self.ws_delay_ms = None
        self.ws_msgs = 0
        self.ws_status = "connecting"
        self.stats = {}
        self.latency = {}
        self._log = open(log_path, "a") if log_path else None
        self._last_move_val = None

    def log(self, rec: dict) -> None:
        if self._log:
            rec["t"] = time.time()
            self._log.write(json.dumps(rec) + "\n")
            self._log.flush()

    def home_prob(self) -> float | None:
        """Vig-free home probability from both mids; degrade to one side."""
        h = self.books.get(self.resolved["home"]["token"], {})
        a = self.books.get(self.resolved["away"]["token"], {})
        hm = _mid(h)
        am = _mid(a)
        if hm and am:
            return hm / (hm + am)
        if hm:
            return hm
        if am:
            return 1 - am
        return None

    def record_market(self) -> None:
        """Call with lock held after any book update."""
        p = self.home_prob()
        if p is None:
            return
        mono = time.monotonic()
        self.mkt_prob.append((mono, p))
        if self._last_move_val is None:
            self._last_move_val = p
            return
        d = p - self._last_move_val
        if abs(d) >= MOVE_THRESH:
            mv = {"mono": mono, "from": self._last_move_val, "to": p, "d": d}
            self.moves.append(mv)
            self._last_move_val = p
            home = self.resolved["home"]["abbrev"]
            self.feed_log.append(
                (mono, "MKT", f"{home} {mv['from'] * 100:.1f}→{p * 100:.1f}¢ ({d * 100:+.1f})")
            )
            self.log({"type": "move", **{k: mv[k] for k in ("from", "to", "d")}})


def _mid(book: dict) -> float | None:
    bb, ba = book.get("bb"), book.get("ba")
    if bb and ba:
        return (bb + ba) / 2
    return bb or ba


def _espn_loop(state: WatchState, stop: threading.Event, interval: float) -> None:
    league = state.resolved["game"].get("_league")
    event_id = state.resolved["game"]["event_id"]
    prev = {}
    cycle = 0
    while not stop.is_set():
        cycle += 1
        if cycle % 30 == 1 and not state.pos_manual:  # ~every minute: catch mid-game adds
            try:
                fresh = fetch_positions(state.resolved)
                with state.lock:
                    state.positions = fresh
            except Exception as e:
                state.log({"type": "pos_err", "err": str(e)})
        t0 = time.perf_counter()
        try:
            gs = espn.game_state(league, event_id)
        except Exception as e:
            with state.lock:
                state.espn_rtt = None
            state.log({"type": "espn_err", "err": str(e)})
            stop.wait(interval * 2)
            continue
        rtt = time.perf_counter() - t0
        mono = time.monotonic()
        wp = gs.get("home_win_prob")
        with state.lock:
            state.game = gs
            state.espn_rtt = rtt
            if isinstance(wp, (int, float)):
                state.espn_wp.append((mono, wp))
            _detect_game_events(state, prev, gs, mono)
        state.log({
            "type": "espn",
            "wp": wp,
            "detail": gs.get("detail"),
            "score": _score_str(gs),
            "situation": gs.get("situation"),
            "rtt_ms": round(rtt * 1000),
        })
        prev = gs
        # final score settled: keep a slow pulse so the screen stays truthful
        stop.wait(15.0 if gs.get("state") == "post" else max(0.2, interval - rtt))


def _score_str(gs: dict) -> str:
    t = gs.get("teams") or {}
    a, h = t.get("away") or {}, t.get("home") or {}
    return f"{a.get('abbrev')} {a.get('score')} - {h.get('abbrev')} {h.get('score')}"


def _detect_game_events(state: WatchState, prev: dict, gs: dict, mono: float) -> None:
    """Score changes and wp jumps become latency-measurable events."""
    if not prev:
        return
    wp0, wp1 = prev.get("home_win_prob"), gs.get("home_win_prob")
    last_play = ((gs.get("situation") or {}).get("lastPlay") or {}).get("text") or ""

    def scores(g):
        t = g.get("teams") or {}
        return tuple((t.get(s) or {}).get("score") for s in ("away", "home"))

    if scores(prev) != scores(gs) and all(s is not None for s in scores(gs)):
        d = (wp1 - wp0) if isinstance(wp0, (int, float)) and isinstance(wp1, (int, float)) else None
        ev = {"mono": mono, "kind": "SCORE", "d": d, "text": _score_str(gs)}
        state.events.append(ev)
        state.feed_log.append((mono, "GAME", f"SCORE {_score_str(gs)} {last_play[:40]}"))
        state.log({"type": "event", "kind": "SCORE", "d": d, "text": ev["text"], "play": last_play})
    elif (
        isinstance(wp0, (int, float))
        and isinstance(wp1, (int, float))
        and abs(wp1 - wp0) >= WP_EVENT_THRESH
    ):
        d = wp1 - wp0
        ev = {"mono": mono, "kind": "WP", "d": d, "text": last_play[:60]}
        state.events.append(ev)
        state.feed_log.append((mono, "GAME", f"WP {d * 100:+.1f} {last_play[:40]}"))
        state.log({"type": "event", "kind": "WP", "d": d, "play": last_play})


def _ws_loop(state: WatchState, stop: threading.Event) -> None:
    from websockets.sync.client import connect

    tokens = [state.resolved["home"]["token"], state.resolved["away"]["token"]]
    while not stop.is_set():
        try:
            ws = connect(WS_URL, open_timeout=10)
            ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
            with state.lock:
                state.ws_status = "live"
            while not stop.is_set():
                try:
                    raw = ws.recv(timeout=5)
                except TimeoutError:
                    ws.send("PING")  # server drops quiet connections
                    continue
                if raw in ("PONG", b"PONG"):
                    continue
                msg = json.loads(raw if isinstance(raw, str) else raw.decode())
                for item in msg if isinstance(msg, list) else [msg]:
                    _handle_ws(state, item)
            ws.close()
        except Exception as e:
            with state.lock:
                state.ws_status = f"reconnecting ({type(e).__name__})"
            state.log({"type": "ws_err", "err": str(e)})
            stop.wait(2)


def _handle_ws(state: WatchState, msg: dict) -> None:
    now_ms = time.time() * 1000
    ts = msg.get("timestamp")
    with state.lock:
        state.ws_msgs += 1
        if ts:
            state.ws_delay_ms = now_ms - int(ts)

        if msg.get("event_type") == "book":
            tok = msg["asset_id"]
            bids = {float(x["price"]): float(x["size"]) for x in msg.get("bids") or []}
            asks = {float(x["price"]): float(x["size"]) for x in msg.get("asks") or []}
            state.books[tok] = {
                "bids": bids,
                "asks": asks,
                "bb": max(bids) if bids else None,
                "ba": min(asks) if asks else None,
                "last_trade": msg.get("last_trade_price"),
            }
            state.record_market()
            state.log({"type": "book", "tok": tok[:10], "bb": state.books[tok]["bb"],
                       "ba": state.books[tok]["ba"], "ts_srv": ts})

        elif "price_changes" in msg:
            for ch in msg["price_changes"]:
                tok = ch.get("asset_id")
                book = state.books.setdefault(
                    tok, {"bids": {}, "asks": {}, "bb": None, "ba": None, "last_trade": None}
                )
                top0 = (book["bb"], book["ba"])
                px, sz = float(ch["price"]), float(ch["size"])
                side = book["bids"] if ch.get("side") == "BUY" else book["asks"]
                if sz == 0:
                    side.pop(px, None)
                else:
                    side[px] = sz
                # server hands us top-of-book directly; trust it over our depth map
                if ch.get("best_bid"):
                    book["bb"] = float(ch["best_bid"])
                elif book["bids"]:
                    book["bb"] = max(book["bids"])
                if ch.get("best_ask"):
                    book["ba"] = float(ch["best_ask"])
                elif book["asks"]:
                    book["ba"] = min(book["asks"])
                # depth-only jiggles are noise for coupling research — log top changes
                if (book["bb"], book["ba"]) != top0:
                    state.log({"type": "mkt", "tok": (tok or "")[:10], "bb": book["bb"],
                               "ba": book["ba"], "ts_srv": ts})
            state.record_market()

        elif msg.get("event_type") == "last_trade_price":
            tok = msg.get("asset_id")
            if tok in state.books:
                state.books[tok]["last_trade"] = msg.get("price")
            state.log({"type": "trade", "tok": (tok or "")[:10], "price": msg.get("price"),
                       "side": msg.get("side"), "ts_srv": ts})


# ============================================================
# Dashboard
# ============================================================


def _diamond(sit: dict):
    """Bases + count, scoreboard-style: occupied bases light up solid."""
    from rich.text import Text

    def sty(k):
        return "bold black on yellow" if sit.get(k) else "dim"

    s1, s2, s3 = sty("onFirst"), sty("onSecond"), sty("onThird")
    rows = [
        [("      ", None), ("╭───╮", s2), ("      ", None)],
        [("      ", None), ("│ 2 │", s2), ("      ", None)],
        [("    ╱ ", "dim"), ("╰───╯", s2), (" ╲    ", "dim")],
        [("╭───╮", s3), ("       ", None), ("╭───╮", s1)],
        [("│ 3 │", s3), ("       ", None), ("│ 1 │", s1)],
        [("╰───╯", s3), (" ╲   ╱ ", "dim"), ("╰───╯", s1)],
        [("        ⌂        ", "bold")],
    ]
    t = Text()
    for row in rows:
        for chunk, style in row:
            t.append(chunk, style=style)
        t.append("\n")

    def dots(n, mx):
        n = min(int(n or 0), mx)
        return "●" * n + "○" * (mx - n)

    t.append("\n ")
    t.append(f"B {dots(sit.get('balls'), 3)}", style="cyan")
    t.append("  ")
    t.append(f"S {dots(sit.get('strikes'), 2)}", style="cyan")
    t.append("\n      ")
    t.append(f"O {dots(sit.get('outs'), 2)}", style="bold red")
    return t


def _scorebox(teams: dict):
    """Line score: innings, R/H/E."""
    from rich.table import Table

    away, home = teams.get("away") or {}, teams.get("home") or {}
    n = max(9, len(away.get("linescores") or []), len(home.get("linescores") or []))
    t = Table(box=None, padding=(0, 1), header_style="dim")
    t.add_column("")
    for i in range(1, n + 1):
        t.add_column(str(i), justify="right")
    for lbl in ("R", "H", "E"):
        t.add_column(lbl, justify="right", style="bold")
    for tm in (away, home):
        ls = tm.get("linescores") or []
        cells = [str(v) for v in ls] + [" "] * (n - len(ls))
        name = tm.get("abbrev") or ""
        if tm.get("record"):
            name += f" [dim]{tm['record']}[/]"
        t.add_row(
            name,
            *cells,
            str(tm.get("score") or "0"),
            "—" if tm.get("hits") is None else str(tm.get("hits")),
            "—" if tm.get("errors") is None else str(tm.get("errors")),
        )
    return t


def _matchup(gs: dict):
    """Current pitcher vs batter, plus ESPN's situational notes (RISP, BvP)."""
    from rich.text import Text

    t = Text()
    p = gs.get("pitcher")
    if p:
        st = p.get("pitching") or {}
        parts = [x for x in (
            f"{st.get('IP')} IP" if st.get("IP") else None,
            f"{st.get('H')} H" if st.get("H") is not None else None,
            f"{st.get('ER')} ER" if st.get("ER") is not None else None,
            f"{st.get('K')} K" if st.get("K") is not None else None,
            f"{st.get('PC')} P" if st.get("PC") else None,
            f"ERA {st.get('ERA')}" if st.get("ERA") else None,
        ) if x]
        t.append("P  ", style="dim")
        t.append(f"{p.get('name')} ({p.get('team')})  ", style="bold")
        t.append(" · ".join(parts) + "\n")
    b = gs.get("batter")
    if b:
        st = b.get("batting") or {}
        parts = [x for x in (
            st.get("H-AB"),
            f"{st.get('HR')} HR" if st.get("HR") not in (None, "0") else None,
            f"{st.get('RBI')} RBI" if st.get("RBI") not in (None, "0") else None,
            f"AVG {st.get('AVG')}" if st.get("AVG") else None,
        ) if x]
        t.append("AB ", style="dim")
        t.append(f"{b.get('name')} ({b.get('team')})  ", style="bold")
        t.append(" · ".join(parts) + "\n")
    for note in (gs.get("notes") or [])[:2]:
        t.append(note + "\n", style="dim italic")
    if not t.plain:
        t.append("between innings", style="dim")
    return t


def _ladder(book: dict, depth: int = 4):
    """Order-book depth ladder: asks above, bids below, sized bars."""
    from rich.table import Table
    from rich.text import Text

    asks = sorted((book.get("asks") or {}).items())[:depth]
    bids = sorted((book.get("bids") or {}).items(), reverse=True)[:depth]
    if not asks and not bids:
        return Text("no depth", style="dim")
    mx = max(s for _, s in asks + bids)
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", width=4)
    t.add_column(justify="right", width=6)
    t.add_column()

    def bar(sz, style):
        return Text("█" * max(1, round(sz / mx * 12)), style=style)

    t.add_row(Text("¢", style="dim"), Text("shares", style="dim"), "")
    for px, sz in reversed(asks):
        t.add_row(Text(f"{px * 100:.1f}", style="red"), f"{sz:,.0f}", bar(sz, "red"))
    mid, spr = _mid(book), None
    if book.get("bb") and book.get("ba"):
        spr = book["ba"] - book["bb"]
    label = f"mid {mid * 100:.1f}" + (f" · spr {spr * 100:.1f}" if spr else "") if mid else "·"
    t.add_row("", "", Text(label, style="bold"))
    for px, sz in bids:
        t.add_row(Text(f"{px * 100:.1f}", style="green"), f"{sz:,.0f}", bar(sz, "green"))
    return t


def _render(state: WatchState):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    r = state.resolved
    home, away = r["home"], r["away"]
    gs = state.game or {}
    wp = gs.get("home_win_prob")
    mkt = state.home_prob()
    hb = state.books.get(home["token"], {})
    ab = state.books.get(away["token"], {})

    title = Text.assemble(
        (f"{away['abbrev']} @ {home['abbrev']}  ", "bold"),
        (_score_str(gs) + "  ", "bold yellow"),
        (gs.get("detail") or "…", "cyan"),
    )

    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", width=6)
    t.add_column(min_width=44)
    t.add_column(style="dim")
    if isinstance(wp, (int, float)):
        t.add_row(
            "ESPN",
            f"{home['abbrev']} [bold]{wp * 100:.1f}%[/]   {away['abbrev']} {100 - wp * 100:.1f}%",
            f"rtt {state.espn_rtt * 1000:.0f}ms" if state.espn_rtt else "rtt —",
        )
    else:
        t.add_row("ESPN", "no win prob yet", "")

    def side_str(b, name):
        bb, ba = b.get("bb"), b.get("ba")
        if bb is None and ba is None:
            return f"{name} —"
        return f"{name} [bold]{(_mid(b) or 0) * 100:.1f}¢[/] ({bb if bb else '—'}/{ba if ba else '—'})"

    delay = f"{state.ws_delay_ms:+.0f}ms" if state.ws_delay_ms is not None else "—"
    t.add_row(
        "MKT",
        f"{side_str(hb, home['abbrev'])}   {side_str(ab, away['abbrev'])}",
        f"ws {state.ws_status} · delay {delay} · {state.ws_msgs} msgs",
    )
    if isinstance(wp, (int, float)) and mkt is not None:
        edge = (wp - mkt) * 100
        color = "green" if edge > 0 else "red"
        t.add_row(
            "EDGE",
            f"{home['abbrev']} [{color}]{edge:+.1f}pts[/]   {away['abbrev']} [{color}]{-edge:+.1f}pts[/]"
            f"   (vig-free mkt {mkt * 100:.1f}%)",
            "espn − mkt",
        )
    for pos in state.positions:
        side = r[pos["side"]]
        mark = _mid(state.books.get(side["token"], {}))
        if mark:
            pnl = pos["size"] * (mark - pos["avg"])
            color = "green" if pnl >= 0 else "red"
            t.add_row(
                "POS",
                f"{side['abbrev']} {pos['size']:g} @ {pos['avg'] * 100:.1f}¢ → mark {mark * 100:.1f}¢"
                f"  [{color}]{pnl:+.2f}$[/]",
                "manual" if state.pos_manual else "auto",
            )

    sparks = Table.grid(padding=(0, 1))
    sparks.add_column(style="dim", width=5)
    sparks.add_column()
    sparks.add_column(justify="right", width=6)

    def spark_row(label, series, style):
        cur = f"{series[-1][1] * 100:.1f}%" if series else "—"
        sparks.add_row(label, Text(sparkline(series), style=style), Text(cur, style=f"bold {style}"))

    spark_row("ESPN", list(state.espn_wp), "cyan")
    spark_row("MKT", list(state.mkt_prob), "yellow")

    feed = Table.grid(padding=(0, 1))
    feed.add_column(style="dim", width=8)
    feed.add_column(width=5)
    feed.add_column()
    now_mono, now_wall = time.monotonic(), time.time()
    tail = list(state.feed_log)[-5:]
    if not tail:
        feed.add_row("", "", Text("waiting for game events and book moves…", style="dim"))
    for mono, kind, txt in tail:
        wall = datetime.fromtimestamp(now_wall - (now_mono - mono)).strftime("%H:%M:%S")
        feed.add_row(wall, Text(kind, style="cyan" if kind == "GAME" else "yellow"), txt)

    st, lat = state.stats, state.latency
    fmt = lambda v, f=".2f": format(v, f) if v is not None else "—"
    stats_line = (
        f"corr(Δespn,Δmkt) [bold]{fmt(st.get('r'))}[/] · "
        f"best lag [bold]{fmt(st.get('best_lag'), '+.0f')}s[/] (r {fmt(st.get('best_r'))}) · "
        f"reaction n={lat.get('n', 0)} med [bold]{fmt(lat.get('median'), '+.1f')}s[/] "
        f"[{fmt(lat.get('min'), '+.1f')} … {fmt(lat.get('max'), '+.1f')}] · "
        f"mkt-led {lat.get('led', 0)} · no-react {lat.get('unmatched', 0)}"
    )

    blocks = [Panel(t, title=title, border_style="blue")]

    sit = gs.get("situation") or {}
    if any(k in sit for k in ("balls", "strikes", "outs")):  # baseball mode
        field = Table.grid(padding=(0, 1))
        field.add_column()
        field.add_column()
        field.add_row(
            Panel(_diamond(sit), title="field", border_style="dim", padding=(0, 1)),
            Group(
                Panel(_scorebox(gs.get("teams") or {}), title="line score",
                      border_style="dim", padding=(0, 1)),
                Panel(_matchup(gs), title="matchup", border_style="dim", padding=(0, 1)),
            ),
        )
        blocks.append(field)

    books_row = Table.grid(padding=(0, 1))
    books_row.add_column()
    books_row.add_column()
    books_row.add_row(
        Panel(_ladder(ab), title=f"{away['abbrev']} to win", border_style="dim", padding=(0, 1)),
        Panel(_ladder(hb), title=f"{home['abbrev']} to win", border_style="dim", padding=(0, 1)),
    )
    blocks.append(books_row)

    blocks += [
        Panel(sparks, title=f"{home['abbrev']} win prob · espn vs market · 15m", border_style="dim"),
        Panel(feed, title="events", border_style="dim"),
        Panel(Text.from_markup(stats_line), title="game→market coupling", border_style="magenta"),
    ]
    return Group(*blocks)


def run_watch(
    league: str,
    ref: str,
    *,
    slug: str | None = None,
    pos: str | None = None,
    interval: float = 2.0,
    log_path: str | None = None,
    no_log: bool = False,
    duration: float | None = None,
    resolved: dict | None = None,
) -> None:
    from rich.console import Console
    from rich.live import Live

    console = Console()
    if resolved is None:
        resolved = resolve_moneyline(league, ref, slug=slug)
    resolved["game"]["_league"] = league

    path = None
    if not no_log:
        default_dir = Path.home() / ".pmt" / "gamewatch"
        default_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        path = Path(log_path) if log_path else default_dir / f"{resolved['slug']}-{stamp}.jsonl"

    state = WatchState(resolved, path)
    if pos:  # manual override, e.g. what-if sizing
        team, rest = pos.split(":", 1)
        size, price = rest.split("@")
        team = team.lower()
        side = "home" if team in (
            resolved["home"]["abbrev"].lower(), resolved["home"]["name"].lower()
        ) else "away"
        state.positions = [{"side": side, "size": float(size), "avg": float(price)}]
        state.pos_manual = True
    else:
        try:
            state.positions = fetch_positions(resolved)
        except Exception:
            pass  # no funder configured / data-api hiccup: watch still works
    state.log({"type": "start", "slug": resolved["slug"],
               "home": resolved["home"]["abbrev"], "away": resolved["away"]["abbrev"]})
    stop = threading.Event()
    threads = [
        threading.Thread(target=_espn_loop, args=(state, stop, interval), daemon=True),
        threading.Thread(target=_ws_loop, args=(state, stop), daemon=True),
    ]
    for th in threads:
        th.start()

    started = time.monotonic()
    last_stats = last_stats_log = 0.0
    with state.lock:
        first = _render(state)
    try:
        with Live(first, console=console, refresh_per_second=6, screen=True) as live:
            while not stop.is_set():
                time.sleep(0.15)
                now = time.monotonic()
                if now - last_stats > 2.0:
                    cutoff = now - 1800  # rolling half hour: regimes change with the score
                    with state.lock:
                        espn_s = [p for p in state.espn_wp if p[0] >= cutoff]
                        mkt_s = [p for p in state.mkt_prob if p[0] >= cutoff]
                        events = list(state.events)
                        moves = list(state.moves)
                    stats = corr_and_lag(espn_s, mkt_s)
                    lat = match_latencies(events, moves)
                    with state.lock:
                        state.stats, state.latency = stats, lat
                    last_stats = now
                    if now - last_stats_log > 30.0:
                        state.log({"type": "stats", **stats,
                                   **{f"lat_{k}": v for k, v in lat.items()}})
                        last_stats_log = now
                with state.lock:
                    live.update(_render(state))
                if duration and now - started >= duration:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()

    with state.lock:
        st, lat = state.stats, state.latency
        n_ev, n_mv = len(state.events), len(state.moves)
    fmt = lambda v, f=".2f": format(v, f) if v is not None else "—"
    console.print(
        f"\n[bold]session[/]: {n_ev} game events, {n_mv} market moves · "
        f"corr {fmt(st.get('r'))} · best lag {fmt(st.get('best_lag'), '+.0f')}s · "
        f"median reaction {fmt(lat.get('median'), '+.1f')}s (led {lat.get('led', 0)})"
    )
    if path:
        console.print(f"log: {path}")
