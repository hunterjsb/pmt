"""Grade the maker step-0 resting-bid experiment against WALLET truth.

Four record sets, joined:

  order tape  ~/.pmt/engine/order-latency-tape.jsonl — one row per order that
              reached the wire, carrying `post_only`. The only place maker and
              taker INTENT is recorded per order.
  eval tape   ~/.pmt/engine/updown-tape.jsonl — `sides[].maker_rest` marks the
              ticks a bid was resting; `ev=fire` marks every taker clip.
  engine log  `maker bid resting` lines: the 0.001-grain model price and the
              safety score. Coverage has holes (a rotation gap 15:43-17:31Z on
              2026-08-23); the order tape does not, so it is the bid count.
  wallet      data-api /activity. Ground truth for the fill and the outcome.

ATTRIBUTING A FILL — three independent signals, all three required:

  1. FEE. Polymarket charges the taker and not the maker, and the charge is
     visible in the wallet: `usdcSize - price*size` is 0.07*min(p,1-p)*p*size
     on a taker fill and EXACTLY zero when we were the resting side. Verified
     to the cent on every fee-bearing updown buy in this wallet. Zero fee is
     necessary, not sufficient: an ordinary crossing GTC order that only
     partly matched leaves its remainder on the book, and that remainder fills
     fee-free too.
  2. POST-ONLY ORDER. An acked `post_only` order on that token at that exact
     wire price, placed BEFORE the fill.
  3. NO TAKER ORDER EXPLAINS IT. No crossing order on the same token at the
     same price stands unfilled ahead of the fill. This is what separates a
     maker-origin fill from a taker clip that happened to rest, and on
     2026-08-23 it is what disqualifies 3 of the 5 fee-free fills.

Run:  cd pmtrader && uv run python ../analysis/maker_grading.py [--cache F]
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import os
import re
from collections import defaultdict

ENGINE_DIR = os.path.expanduser("~/.pmt/engine")
ORDER_TAPE = f"{ENGINE_DIR}/order-latency-tape.jsonl"
UPDOWN_TAPE = f"{ENGINE_DIR}/updown-tape.jsonl"

# A resting bid fills at the tick it was posted at; the CLOB never improves
# it. The tolerance is float noise, not price slack.
PX_EPS = 1e-6
# data-api stamps a trade to the on-chain second and aggregates partials, so a
# fill can time-stamp a hair before the ack that produced it.
FILL_LEAD_S = 3.0
# Fee residual under this is "we paid nothing", i.e. we were the resting side.
FEE_EPS = 1e-6


# --------------------------------------------------------------------------
# loaders
# --------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_REST_LINE = re.compile(
    r"^\W*(\d{4}-\d\d-\d\dT[\d:.]+Z).*maker bid resting.*?"
    r"side\W*=\W*\"(\w+)\".*?px\W*=\W*([\d.]+).*?size\W*=\W*([\d.]+)"
    r".*?safety\W*=\W*([-\d.eE]+).*?slug\W*=\W*(\S+)"
)
_RTDS_LINE = re.compile(r"slug=(\S+)")


def _iso(ts: str) -> float:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def hhmm(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%H:%M:%S")


def _engine_logs():
    for p in sorted(glob.glob(f"{ENGINE_DIR}/engine-*.log")) + \
            sorted(glob.glob(f"{ENGINE_DIR}/engine-*.log.gz")):
        opener = gzip.open if p.endswith(".gz") else open
        with opener(p, "rt", errors="replace") as f:
            for raw in f:
                yield raw


def log_rests() -> list[dict]:
    out = []
    for raw in _engine_logs():
        if "maker bid resting" not in raw:
            continue
        m = _REST_LINE.match(_ANSI.sub("", raw))
        if not m:
            continue
        ts, side, px, size, safety, slug = m.groups()
        out.append({"t": _iso(ts), "side": side, "px": float(px),
                    "size": float(size), "safety": float(safety), "slug": slug})
    out.sort(key=lambda r: r["t"])
    return out


def rtds_windows() -> set[str]:
    """Slugs the engine logged onto the RTDS settlement stream."""
    out = set()
    for raw in _engine_logs():
        if "feeding off the RTDS settlement stream" in raw:
            m = _RTDS_LINE.search(_ANSI.sub("", raw))
            if m:
                out.add(m.group(1))
    return out


def orders() -> list[dict]:
    rows = [json.loads(l) for l in open(ORDER_TAPE)]
    rows.sort(key=lambda r: r["t"])
    return rows


def tape() -> tuple[list[dict], list[dict]]:
    """(maker_rest eval ticks, fire records)."""
    rests, fires = [], []
    with open(UPDOWN_TAPE) as f:
        for line in f:
            r = json.loads(line)
            ev = r.get("ev")
            if ev == "fire":
                fires.append(r)
            elif ev == "eval":
                for s in r.get("sides") or []:
                    if isinstance(s, dict) and s.get("maker_rest") is not None:
                        rests.append({"t": r["t"], "slug": r["slug"],
                                      "side": s["side"], "px": s["maker_rest"],
                                      "size": s.get("maker_size"),
                                      "safety": s.get("safety")})
    return rests, fires


def wallet_rows(cache: str | None) -> list[dict]:
    if cache and os.path.exists(cache):
        return json.load(open(cache))
    from polymarket import wallet
    from polymarket.env import load_project_env
    load_project_env()
    rows = wallet.fetch_wallet_activity(wallet.funder_address(), 0.0)
    if cache:
        json.dump(rows, open(cache, "w"))
    return rows


# --------------------------------------------------------------------------
# joins
# --------------------------------------------------------------------------

def token_index(rows: list[dict]) -> dict[str, tuple[str, str]]:
    """token id -> (slug, side), from the wallet's own naming of both."""
    out = {}
    for a in rows:
        tok, slug = a.get("asset") or "", a.get("slug") or ""
        outcome = (a.get("outcome") or "").lower()
        if tok and slug and outcome:
            out[tok] = (slug, "down" if outcome == "down" else "up")
    return out


def label_placements(placements: list[dict], rests: list[dict], logs: list[dict],
                     tokens: dict[str, tuple[str, str]]) -> None:
    """Stamp slug/side on each post-only ack.

    A token we filled on is named by the wallet. A token we only ever rested
    on has no wallet row, so it falls back to the eval tape and then to the
    engine log — one arm at a time posts a maker bid, so the resting record
    nearest the ack names the market unambiguously.
    """
    for p in placements:
        hit = tokens.get(p["token"])
        if hit:
            p["slug"], p["side"], p["attr"] = hit[0], hit[1], "token"
            continue
        for src, tag in ((rests, "tape"), (logs, "log")):
            near = [r for r in src if abs(r["t"] - p["t"]) <= 6.0]
            if near:
                r = min(near, key=lambda r: abs(r["t"] - p["t"]))
                p["slug"], p["side"], p["attr"] = r["slug"], r["side"], tag
                break
        else:
            p["slug"], p["side"], p["attr"] = "?", "?", "none"


def fee_of(a: dict) -> float:
    return float(a.get("usdcSize") or 0.0) - float(a.get("price") or 0.0) * \
        float(a.get("size") or 0.0)


def classify(a: dict, acks: list[dict]) -> dict:
    """Origin of one wallet BUY. See the module docstring for the three signals."""
    ts, px = float(a.get("timestamp") or 0.0), float(a.get("price") or 0.0)
    at_px = [o for o in acks
             if abs(float(o["price"]) - px) < PX_EPS and o["t"] <= ts + FILL_LEAD_S]
    post = [o for o in at_px if o.get("post_only")]
    take = [o for o in at_px if not o.get("post_only")]
    fee = fee_of(a)
    row = {"slug": a.get("slug") or "", "side": (a.get("outcome") or "").lower(),
           "t": ts, "px": px, "shares": float(a.get("size") or 0.0),
           "usd": float(a.get("usdcSize") or 0.0), "fee": fee,
           "token": a.get("asset") or ""}
    if fee > FEE_EPS:
        row["origin"] = "taker"        # we crossed and paid for it
    elif not post:
        row["origin"] = "taker-rested" if take else "unknown"
    elif take and max(o["t"] for o in take) > max(o["t"] for o in post):
        # A crossing order at this price stands AHEAD of our quote — its
        # unmatched remainder is the simpler explanation for a fee-free fill.
        row["origin"] = "taker-rested"
    else:
        row["origin"] = "maker"
        row["order_t"] = max(o["t"] for o in post)
    return row


def windows(rows: list[dict]) -> dict[str, dict]:
    """Per-slug wallet ledger: buys/sells/redeem and the winning outcome.

    A WON window pays a redeem row naming the winning outcome. A LOST window
    still gets a redeem row, for $0 — which names no winner, so the loser is
    inferred from the only side we were holding. `pmt crypto stats` reaches
    the same verdict via polymarket.outcomes; this stays self-contained so the
    grading can be re-run offline against a cached wallet dump.
    """
    w: dict[str, dict] = {}
    for a in rows:
        slug = a.get("slug") or ""
        if "-updown-" not in slug:
            continue
        d = w.setdefault(slug, {"buy": 0.0, "sell": 0.0, "redeem": 0.0,
                                "buy_shares": 0.0, "won": None,
                                "redeem_seen": False, "sides": defaultdict(float)})
        usd = float(a.get("usdcSize") or 0.0)
        if a["type"] == "TRADE":
            if a.get("side") == "BUY":
                d["buy"] += usd
                d["buy_shares"] += float(a.get("size") or 0.0)
                d["sides"][(a.get("outcome") or "").lower()] += float(a.get("size") or 0.0)
            else:
                d["sell"] += usd
        elif a["type"] == "REDEEM":
            d["redeem"] += usd
            d["redeem_seen"] = True
            if usd > 0.5:
                d["won"] = (a.get("outcome") or "").lower()
    for d in w.values():
        d["resolved"] = d["redeem_seen"]
        if d["won"] is None and d["redeem_seen"] and d["redeem"] <= 0.5:
            held = [s for s, n in d["sides"].items() if n > 0]
            if len(held) == 1:
                d["won"] = "down" if held[0] == "up" else "up"
    return w


def fill_pnl(f: dict, won: str | None) -> float | None:
    """One fill's held-to-resolution P&L. A binary pays 1 or 0, so a fill's own
    P&L is exact once the winner is known — no allocating a window's total
    across fills, which is what lets a maker fill be graded inside a window
    that also took taker clips."""
    if won is None:
        return None
    return f["shares"] * ((1.0 - f["px"]) if f["side"] == won else -f["px"])


def sym(slug: str) -> str:
    return slug.split("-updown-")[0]


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=None, help="wallet activity JSON cache")
    args = ap.parse_args()

    wal = wallet_rows(args.cache)
    ords = orders()
    rests, _fires = tape()
    logs = log_rests()

    placements = [dict(o) for o in ords
                  if o.get("stage") == "ack" and o.get("post_only")]
    label_placements(placements, rests, logs, token_index(wal))

    acks_by_token: dict[str, list[dict]] = defaultdict(list)
    for o in ords:
        if o.get("stage") == "ack":
            acks_by_token[o["token"]].append(o)

    lo, hi = min(o["t"] for o in ords), max(o["t"] for o in ords)
    fills = [classify(a, acks_by_token.get(a.get("asset") or "", []))
             for a in wal
             if a.get("type") == "TRADE" and a.get("side") == "BUY"
             and lo <= float(a.get("timestamp") or 0) <= hi + 300]
    maker_fills = [f for f in fills if f["origin"] == "maker"]
    wins = windows(wal)

    print(f"order tape covers {hhmm(lo)}Z-{hhmm(hi)}Z ({(hi - lo) / 3600:.2f}h), "
          f"{len(ords)} orders; engine-log resting lines {len(logs)}; "
          f"eval-tape resting ticks {len(rests)}")
    print()

    print("=== RESTED BIDS (post-only acks) ===")
    print(f"{'time':>8}  {'slug':<28} {'side':<4} {'wire':>5} {'sh':>4}  {'attr':<5} outcome")
    prev: dict[str, dict] = {}
    churn = 0
    for p in placements:
        was = prev.get(p["token"])
        same = was is not None and abs(float(was["price"]) - float(p["price"])) < PX_EPS
        churn += same
        prev[p["token"]] = p
        got = [f for f in maker_fills
               if f["token"] == p["token"] and f.get("order_t") == p["t"]]
        mark = (f"FILLED {sum(f['shares'] for f in got):.0f}sh" if got
                else ("re-quote at the same wire price" if same else "-"))
        print(f"{hhmm(p['t']):>8}  {p['slug']:<28} {p['side']:<4} "
              f"{float(p['price']):>5.2f} {float(p['size']):>4.0f}  {p['attr']:<5} {mark}")
    quotes = {(p["slug"], p["side"], round(float(p["price"]), 4)) for p in placements}
    print()
    print(f"placements on the wire:            {len(placements)}")
    print(f"  of which replaced an identical   {churn}   <- 0.001 model grid vs 0.01 "
          f"wire tick: a re-quote that changes nothing but the queue position")
    print(f"distinct (slug, side, wire price):  {len(quotes)}")
    print(f"windows carrying a resting bid:     {len({p['slug'] for p in placements})}")
    print()

    print("=== WALLET BUYS IN THE ORDER-TAPE ERA, BY ORIGIN ===")
    c = defaultdict(int)
    for f in fills:
        c[f["origin"]] += 1
    for k in sorted(c):
        print(f"  {k:<14} {c[k]}")
    print()
    print("  fee-free fills (we were the resting side):")
    for f in sorted((f for f in fills if abs(f["fee"]) <= FEE_EPS), key=lambda f: f["t"]):
        won = wins.get(f["slug"], {}).get("won")
        pnl = fill_pnl(f, won)
        print(f"   {hhmm(f['t'])} {f['slug']:<28} {f['side']:<4} {f['px']:.2f} x "
              f"{f['shares']:>5.1f} = ${f['usd']:>6.2f}  {f['origin']:<12} "
              f"won={won or '?':<5} pnl={('$%.2f' % pnl) if pnl is not None else '?'}")
    print()

    print("=== MAKER SCOREBOARD (wallet-graded) ===")
    print(f"{'sym':<5} {'rested':>6} {'quotes':>6} {'fills':>5} {'fill%':>6} "
          f"{'shares':>7} {'notional':>9} {'pnl':>8} {'$/rest':>8} {'$/fill':>8} {'c/sh':>6}")
    tot = defaultdict(lambda: defaultdict(float))
    for p in placements:
        tot[sym(p["slug"])]["rested"] += 1
    for q in quotes:
        tot[sym(q[0])]["quotes"] += 1
    for f in maker_fills:
        s = sym(f["slug"])
        tot[s]["fills"] += 1
        tot[s]["shares"] += f["shares"]
        tot[s]["usd"] += f["usd"]
        pnl = fill_pnl(f, wins.get(f["slug"], {}).get("won"))
        tot[s]["pnl" if pnl is not None else "open"] += pnl or 1
    grand = defaultdict(float)
    for s in sorted(tot) + ["ALL"]:
        d = grand if s == "ALL" else tot[s]
        if s != "ALL":
            for k, v in d.items():
                grand[k] += v
        r = d["rested"] or 1
        fl = d["fills"] or 1
        sh = d["shares"] or 1
        print(f"{s:<5} {d['rested']:>6.0f} {d['quotes']:>6.0f} {d['fills']:>5.0f} "
              f"{d['fills'] / r * 100:>5.1f}% {d['shares']:>7.1f} ${d['usd']:>8.2f} "
              f"${d['pnl']:>7.2f} ${d['pnl'] / r:>7.2f} ${d['pnl'] / fl:>7.2f} "
              f"{d['pnl'] / sh * 100:>5.2f}")
    print()

    print("=== XRP 5m, SPLIT AT THE RTDS CUTOVER ===")
    rtds = rtds_windows()
    xrp_rtds = sorted(int(s.rsplit("-", 1)[1]) for s in rtds
                      if s.startswith("xrp-updown-5m"))
    cut = xrp_rtds[0] if xrp_rtds else float("inf")
    # Membership in `rtds` has log-rotation holes; the cutover EPOCH does not,
    # because every arm rolls and the feed param rides the roll chain.
    print(f"first xrp window on the stream: {hhmm(cut)}Z")
    era = {False: defaultdict(float), True: defaultdict(float)}
    rowsx = []
    for slug, d in wins.items():
        if not slug.startswith("xrp-updown-5m") or not d["resolved"]:
            continue
        start = int(slug.rsplit("-", 1)[1])
        pnl = d["redeem"] + d["sell"] - d["buy"]
        e = era[start >= cut]
        e["w" if pnl > 0 else "l"] += 1
        e["pnl"] += pnl
        e["usd"] += d["buy"]
        rowsx.append((start, slug, pnl, d["buy"], start >= cut))
    for on in (False, True):
        e = era[on]
        tag = "rtds  " if on else "binance"
        u = e["usd"] or 1
        print(f"  {tag}  {e['w']:>3.0f}W-{e['l']:<3.0f} ${e['pnl']:>8.2f} on "
              f"${e['usd']:>8.2f} notional  ({e['pnl'] / u * 100:+.1f}%)")
    print("  losses:")
    for start, slug, pnl, buy, on in sorted(rowsx):
        if pnl <= 0:
            print(f"    {slug:<28} [{'rtds' if on else 'bnce'}] ${pnl:>8.2f} "
                  f"on ${buy:.2f}")


if __name__ == "__main__":
    main()
