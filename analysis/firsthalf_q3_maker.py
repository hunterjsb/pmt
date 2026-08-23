"""Q3 — conservative maker backtest over the recorded first halves.

WHAT THE BOOK ACTUALLY IS (measured, see the study writeup): UP and DOWN are
two mirrored views of one price. Across 76 live simultaneous both-leg
snapshots (5 symbols x 2 durations x 4 rounds) `up_ask == 1 - dn_bid` and
`up_bid == 1 - dn_ask` held EXACTLY every time; ask-sum was never below 1.000
and bid-sum never above it. So the only quote a maker can place is a two-sided
quote on one axis, and "rest a bid on UP" and "rest an ask on DOWN" are the
same economic order. Both legs are BUYs, so neither needs starting inventory
and neither needs on-chain minting.

P&L identity used throughout (maker fee is zero — fees.md, "Makers are never
charged fees"):

    net = SUM over fills of (payoff_of_that_token - fill_price)

Pair-merge does not appear in it: holding min(up,dn) as a complete set redeems
for $1 whatever happens, which is exactly what marking each leg separately
already says. Merging frees capital early; it does not create P&L. It is
reported as a capital number, not a profit number.

Fill model is docs/maker-design.md §5.3 method 1 (queue-conservative): at each
(re)quote we join the BACK of the visible level, so `queue_ahead` = the
recorded top-of-book size at our price, and only opposing print volume in
EXCESS of that queue reaches us, capped at our remaining size. Prints come
from the real data-api backfill (firsthalf_harvest_prints.py), not the
engine's own flow fields, which recorded ~0.4% of actual prints.

Two fill regimes are reported as BOUNDS, because whether Polymarket's
complementary matching fills our UP bid from DOWN buy flow is supported by the
corpus but not proven by it:

  own-leg  (LOWER bound) — a buy of UP fills only on taker SELL prints in UP.
  complementary (UPPER)  — it also fills on taker BUY prints in DOWN at
                           >= 1 - our price, which is the same order seen
                           from the other side of the mirror.

Run: uv run python analysis/firsthalf_q3_maker.py
"""

from __future__ import annotations

import bisect
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from firsthalf_lib import TAKER_FEE_RATE, load_book_windows, mid_up, parse_slug, pct  # noqa: E402
from firsthalf_q2_bookstruct import build_truth  # noqa: E402

PRINTS = Path.home() / ".pmt" / "corpus" / "prints.jsonl"
PER_DAY = {"5m": 288, "15m": 96}
OTHER = {"up": "dn", "dn": "up"}
EPS = 1e-9


def load_prints() -> dict[str, list[dict]]:
    by_slug: dict[str, list[dict]] = defaultdict(list)
    with PRINTS.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("t") is None or d.get("size") is None or d.get("price") is None:
                continue
            by_slug[d["slug"]].append(d)
    for v in by_slug.values():
        v.sort(key=lambda p: p["t"])
    return dict(by_slug)


def simulate(
    rows: list[dict],
    prints: list[dict],
    *,
    size: float,
    requote_s: float = 5.0,
    half_end: float = 0.5,
    back_off_ticks: int = 0,
    complementary: bool = True,
    max_gross: float | None = None,
) -> list[dict]:
    """Rest a BUY at (or behind) the touch on both tokens. Returns fills."""
    ptimes = [p["t"] for p in prints]
    st = {tok: {"px": None, "queue": 0.0, "left": 0.0} for tok in ("up", "dn")}
    gross = 0.0
    fills: list[dict] = []
    last_quote_at = -1e18
    active = [r for r in rows if r["frac"] <= half_end]
    if not active:
        return fills
    w = rows[0]["_w"]

    for i, r in enumerate(active):
        now = r["t"]
        if now - last_quote_at >= requote_s:
            last_quote_at = now
            for tok in ("up", "dn"):
                bid, ask = r.get(f"{tok}_bid"), r.get(f"{tok}_ask")
                s = st[tok]
                if bid is None or ask is None:
                    s["px"] = None  # one-sided book: post-only isn't safely placeable
                    continue
                tick = 0.01 if 0.10 <= bid <= 0.90 else 0.001
                want = round(bid - back_off_ticks * tick, 4)
                if want <= 0:
                    s["px"] = None
                    continue
                if s["px"] is not None and abs(want - s["px"]) < 0.005:
                    continue  # dead-band (maker-design §3): don't thrash the book
                s["px"] = want
                # Queue ahead = what is already resting at our price. Behind the
                # touch there is nothing displayed, so the only queue is whatever
                # arrives later — assume zero, the conservative-for-count case.
                s["queue"] = (r.get(f"{tok}_bid_sz") or 0.0) if back_off_ticks == 0 else 0.0
                s["left"] = size

        t_next = active[i + 1]["t"] if i + 1 < len(active) else now
        if t_next <= now:
            continue
        lo = bisect.bisect_right(ptimes, now)
        hi = bisect.bisect_right(ptimes, t_next)
        for p in prints[lo:hi]:
            tok_p = "up" if p["outcome"] == "up" else "dn"
            # Which of our two resting buys can this print hit?
            if p["side"] == "sell":
                tok, ref = tok_p, p["price"]
            elif complementary:
                tok, ref = OTHER[tok_p], 1.0 - p["price"]
            else:
                continue
            s = st[tok]
            if s["px"] is None or s["left"] <= 0:
                continue
            if ref > s["px"] + 0.0005:
                continue
            vol = p["size"]
            eaten = min(vol, s["queue"])
            s["queue"] -= eaten
            vol -= eaten
            if vol <= EPS:
                continue
            got = min(vol, s["left"])
            if max_gross is not None:
                got = min(got, max(max_gross - gross, 0.0))
            if got <= EPS:
                continue
            s["left"] -= got
            gross += got
            m = mid_up(r)
            fills.append(
                {
                    "t": p["t"],
                    "tok": tok,
                    "px": s["px"],
                    "sz": got,
                    "mid": None if m is None else (m if tok == "up" else 1.0 - m),
                    "frac": (p["t"] - w["start"]) / w["dur_s"],
                }
            )
    return fills


def pnl(fills: list[dict], winner: str) -> dict:
    got = {"up": 0.0, "dn": 0.0}
    cost = 0.0
    spread = 0.0
    for f in fills:
        got[f["tok"]] += f["sz"]
        cost += f["px"] * f["sz"]
        if f["mid"] is not None:
            spread += (f["mid"] - f["px"]) * f["sz"]
    win_tok = "up" if winner == "up" else "dn"
    payoff = got[win_tok]
    net = payoff - cost
    paired = min(got["up"], got["dn"])
    return {
        "net": net,
        "spread": spread,
        "adverse": net - spread,
        "shares": got["up"] + got["dn"],
        "cost": cost,
        "paired": paired,
        "n": len(fills),
    }


def rebate(fills: list[dict]) -> float:
    """Maker's 20% share of the taker fee its counterparty paid, if rebates
    accrue per fill. NOT counted in net — accrual is unverified."""
    return sum(0.2 * TAKER_FEE_RATE * f["px"] * (1 - f["px"]) * f["sz"] for f in fills)


def run(slugs, wins, prints, truth, *, size, back_off_ticks, complementary, max_gross, label):
    agg: dict[str, Counter] = defaultdict(Counter)
    nets: dict[str, list[float]] = defaultdict(list)
    for slug in slugs:
        w = parse_slug(slug)
        fl = simulate(
            wins[slug], prints[slug], size=size, back_off_ticks=back_off_ticks,
            complementary=complementary, max_gross=max_gross,
        )
        p = pnl(fl, truth[slug])
        a = agg[w["series"]]
        a["windows"] += 1
        for k in ("n", "shares", "net", "spread", "adverse", "cost", "paired"):
            a[k] += p[k]
        a["rebate"] += rebate(fl)
        nets[w["series"]].append(p["net"])

    print(f"--- {label} ---")
    print(
        f"{'series':<9} {'wins':>5} {'fills/win':>10} {'shares/win':>11} {'cost/win':>9} "
        f"{'spread$':>8} {'advsel$':>9} {'net$/win':>9} {'net$/day':>10} {'c/share':>8} {'win%':>5}"
    )
    gtot = reb_tot = 0.0
    for s in sorted(agg):
        a = agg[s]
        n = a["windows"]
        d = PER_DAY["5m" if s.endswith("5m") else "15m"]
        daily = a["net"] / n * d
        gtot += daily
        reb_tot += a["rebate"] / n * d
        cps = 100 * a["net"] / a["shares"] if a["shares"] else 0.0
        wr = 100 * sum(1 for x in nets[s] if x > 0) / n
        print(
            f"{s:<9} {n:>5} {a['n']/n:>10.1f} {a['shares']/n:>11.1f} {a['cost']/n:>9.0f} "
            f"{a['spread']/n:>8.2f} {a['adverse']/n:>9.2f} {a['net']/n:>9.2f} {daily:>10.0f} "
            f"{cps:>8.2f} {wr:>4.0f}%"
        )
    allnets = [x for v in nets.values() for x in v]
    lo, hi = bootstrap(allnets)
    d_lo, d_hi = lo * gtot / (sum(allnets) / len(allnets)), hi * gtot / (sum(allnets) / len(allnets))
    print(
        f"{'FLEET':<9} {len(allnets):>5} {'':>10} {'':>11} {'':>9} {'':>8} {'':>9} "
        f"{sum(allnets)/len(allnets):>9.2f} {gtot:>10.0f}"
    )
    print(
        f"   net$/window 95% bootstrap CI: [{lo:+.2f}, {hi:+.2f}]  ->  $/day CI "
        f"[{min(d_lo,d_hi):+.0f}, {max(d_lo,d_hi):+.0f}]"
    )
    print(f"   per-window net p10/p50/p90: {pct(allnets,0.1):+.2f} / {pct(allnets,0.5):+.2f} / {pct(allnets,0.9):+.2f}")
    print(f"   unbooked upside if maker rebates accrue per fill: ${reb_tot:,.0f}/day")
    print()
    return gtot


def bootstrap(xs: list[float], iters: int = 2000) -> tuple[float, float]:
    import random

    rng = random.Random(7)
    n = len(xs)
    means = []
    for _ in range(iters):
        means.append(sum(xs[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def main() -> None:
    wins = load_book_windows()
    truth, _ = build_truth(wins)
    prints = load_prints()
    slugs = [s for s in wins if s in prints and s in truth]

    print("=" * 100)
    print("Q3  CONSERVATIVE MAKER BACKTEST — quoting the first half only (frac 0.0-0.5)")
    print("=" * 100)
    span = max(r[-1]["t"] for r in wins.values()) - min(r[0]["t"] for r in wins.values())
    print(f"windows with book + real prints + outcome: {len(slugs)}   corpus span {span/3600:.2f}h")
    print(f"prints: {sum(len(prints[s]) for s in slugs):,}   maker fee = 0   rebate excluded from net")
    print()

    for size in (50.0, 100.0):
        run(slugs, wins, prints, truth, size=size, back_off_ticks=0, complementary=False,
            max_gross=None, label=f"join the touch, {size:.0f} sh/quote — OWN-LEG fills only (LOWER bound)")
        run(slugs, wins, prints, truth, size=size, back_off_ticks=0, complementary=True,
            max_gross=None, label=f"join the touch, {size:.0f} sh/quote — with complementary matching (UPPER bound)")
    run(slugs, wins, prints, truth, size=100.0, back_off_ticks=1, complementary=True,
        max_gross=None, label="one tick BEHIND the touch, 100 sh/quote — complementary matching")
    run(slugs, wins, prints, truth, size=100.0, back_off_ticks=3, complementary=True,
        max_gross=None, label="three ticks behind the touch, 100 sh/quote — complementary matching")


if __name__ == "__main__":
    main()
