#!/usr/bin/env python3
"""4h up/down late-window book snapshot + expired-window print tape (read-only).

Q3 of the 4h tier fit: the tail-harvest trade only exists if the final ~hour of
a 4h window has a book worth taking. This is a SNAPSHOT, not a study — one
observation per symbol at whatever elapsed state the live 4h windows happen to
be in, plus the printed trade tape of already-EXPIRED 4h windows (which IS
historical, from data-api /trades).

Read-only: gamma event reads, public CLOB /book, public data-api /trades.
It never arms, disarms, or touches the engine.

Run: cd pmtrader && uv run python ../analysis/fourh_book_snapshot.py
     ... --json  (machine-readable dump for the writeup)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pmtrader"))
from polymarket import hosts  # noqa: E402
from polymarket.crypto import eval_updown, parse_semantics  # noqa: E402
from polymarket.scanner import fetch_event  # noqa: E402

SYMBOLS = ["btc", "eth", "sol", "xrp", "doge", "bnb"]
DUR = 14400
TIMEOUT = 15


def slug_for(sym: str, start: int) -> str:
    return f"{sym}-updown-4h-{start}"


def raw_book(token: str) -> dict:
    """Full ladder, not crypto.fetch_book's summary — the harvest needs the
    top-of-book SIZE at the price we'd actually cross, not total depth."""
    r = requests.get(f"{hosts.CLOB}/book", params={"token_id": token},
                     timeout=TIMEOUT, headers=hosts.UA)
    r.raise_for_status()
    b = r.json()
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids") or []),
                  key=lambda p: -p[0])
    asks = sorted(((float(x["price"]), float(x["size"])) for x in b.get("asks") or []),
                  key=lambda p: p[0])
    return {"bids": bids, "asks": asks}


def ladder_usd(levels: list[tuple[float, float]], n: int = 3) -> float:
    return sum(p * s for p, s in levels[:n])


def trades_for(slug: str) -> list[dict]:
    """data-api /trades for one market, paginated. Returns raw rows."""
    ev = fetch_event(slug)
    if not ev:
        return []
    sem = parse_semantics(ev)
    mkts = [m for m in (ev.get("markets") or []) if not m.get("archived")]
    cond = mkts[0].get("conditionId")
    out, offset = [], 0
    while True:
        r = requests.get(f"{hosts.DATA}/trades",
                         params={"market": cond, "limit": 500, "offset": offset,
                                 "takerOnly": "false"},
                         timeout=TIMEOUT, headers=hosts.UA)
        if r.status_code != 200:
            break
        rows = r.json() or []
        out.extend(rows)
        if len(rows) < 500:
            break
        offset += 500
        if offset > 5000:
            break
    for t in out:
        t["_end"] = sem["end"]
        t["_start"] = sem["start"]
    return out


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")


def snapshot(now: int, out: dict) -> None:
    start = (now // DUR) * DUR
    print(f"\n{'='*100}\nLIVE 4h BOOK SNAPSHOT — window {hhmm(start)}-{hhmm(start+DUR)}Z, "
          f"elapsed {(now-start)/60:.0f}min, rem {(start+DUR-now)/60:.0f}min "
          f"(SINGLE SNAPSHOT at {hhmm(now)}Z — not a study)\n{'='*100}")
    hdr = (f"{'sym':<5s} {'side':<5s} {'fair':>6s} {'bid':>6s} {'ask':>6s} {'spr':>5s} "
           f"{'ask@top$':>9s} {'ask3$':>8s} {'bid@top$':>9s} {'net_edge':>9s} {'margin_bp':>10s}")
    print(hdr)
    for sym in SYMBOLS:
        slug = slug_for(sym, start)
        try:
            ev = eval_updown(slug)
        except Exception as e:  # noqa: BLE001
            print(f"{sym:<5s} ERROR {e}")
            continue
        m = ev["model"]
        row = {"slug": slug, "rem_s": ev["rem_s"], "margin_bp": m.get("margin_bp"),
               "p_up": m.get("p_up"), "sigma_bp_per_min": ev["sigma_bp_per_min"],
               "banked_s": m.get("banked_s"), "sides": {}}
        for side in ("up", "down"):
            tok = ev["tokens"][side]
            try:
                bk = raw_book(tok) if tok else {"bids": [], "asks": []}
            except Exception as e:  # noqa: BLE001
                print(f"{sym:<5s} {side:<5s} book error {e}")
                continue
            e_ = ev["edges"][side]
            bid = bk["bids"][0] if bk["bids"] else (None, 0.0)
            ask = bk["asks"][0] if bk["asks"] else (None, 0.0)
            spr = (ask[0] - bid[0]) if (ask[0] is not None and bid[0] is not None) else None
            row["sides"][side] = {
                "fair": e_["fair"], "bid": bid[0], "bid_sz_usd": (bid[0] or 0) * bid[1],
                "ask": ask[0], "ask_sz_usd": (ask[0] or 0) * ask[1],
                "ask3_usd": ladder_usd(bk["asks"]), "bid3_usd": ladder_usd(bk["bids"]),
                "spread": spr, "net_edge": e_["net_edge"],
                "asks": bk["asks"][:5], "bids": bk["bids"][:5],
            }
            s = row["sides"][side]
            bid_s = f"{bid[0]:.3f}" if bid[0] is not None else "  -  "
            ask_s = f"{ask[0]:.3f}" if ask[0] is not None else "  -  "
            spr_s = f"{spr:.3f}" if spr is not None else "  -  "
            net_s = f"{s['net_edge']:+.3f}" if s["net_edge"] is not None else "   -   "
            print(f"{sym:<5s} {side:<5s} {s['fair']:>6.3f} {bid_s:>6s} {ask_s:>6s} {spr_s:>5s} "
                  f"{s['ask_sz_usd']:>9.0f} {s['ask3_usd']:>8.0f} {s['bid_sz_usd']:>9.0f} "
                  f"{net_s:>9s} {(m.get('margin_bp') or 0):>+10.1f}")
        out.setdefault("live", []).append(row)


def expired_tape(now: int, back: int, out: dict) -> None:
    """Printed notional by phase. /trades emits ONE ROW PER COUNTERPARTY: a
    complete-set mint of N shares shows as BUY Down @0.99 + BUY Up @0.01, so
    sum(size*price) over all rows = the USDC that actually moved. The column
    that matters for a tail-harvest is `rich$` — dollars paid for the side
    trading above 50c, i.e. the decided side we would be buying."""
    print(f"\n{'='*100}\nEXPIRED 4h WINDOWS — printed volume by phase (data-api /trades, "
          f"historical)\n{'='*100}")
    print(f"{'slug':<28s} {'trades':>7s} {'vol$':>9s} {'final60m$':>10s} {'final30m$':>10s} "
          f"{'final10m$':>10s} {'last-hr%':>9s} {'n_final60':>10s} {'rich60m$':>9s} {'rich30m$':>9s}")
    for k in range(1, back + 1):
        start = (now // DUR) * DUR - k * DUR
        for sym in SYMBOLS:
            slug = slug_for(sym, start)
            try:
                rows = trades_for(slug)
            except Exception as e:  # noqa: BLE001
                print(f"{slug:<28s} ERROR {e}")
                continue
            if not rows:
                print(f"{slug:<28s} {'0':>7s}")
                continue
            end = rows[0]["_end"]
            tot = n60 = n30 = n10 = rich60 = rich30 = 0.0
            cnt60 = 0
            for t in rows:
                px = float(t.get("price") or 0)
                usd = float(t.get("size") or 0) * px
                ts = float(t.get("timestamp") or 0)
                tot += usd
                rem = end - ts
                if 0 <= rem <= 3600:
                    n60 += usd
                    cnt60 += 1
                    if px >= 0.5:
                        rich60 += usd
                if 0 <= rem <= 1800:
                    n30 += usd
                    if px >= 0.5:
                        rich30 += usd
                if 0 <= rem <= 600:
                    n10 += usd
            pct = 100.0 * n60 / tot if tot else 0.0
            print(f"{slug:<28s} {len(rows):>7d} {tot:>9.0f} {n60:>10.0f} {n30:>10.0f} "
                  f"{n10:>10.0f} {pct:>8.1f}% {cnt60:>10d} {rich60:>9.0f} {rich30:>9.0f}")
            out.setdefault("expired", []).append(
                {"slug": slug, "n": len(rows), "vol": tot, "final60m": n60,
                 "final30m": n30, "final10m": n10, "n_final60": cnt60,
                 "rich60m": rich60, "rich30m": rich30})


def resolved_tokens(slug: str) -> tuple[str | None, str | None, dict]:
    """(winning token id, losing token id, semantics) for a resolved market."""
    ev = fetch_event(slug)
    if not ev:
        return None, None, {}
    mkts = [m for m in (ev.get("markets") or []) if not m.get("archived")]
    if not mkts:
        return None, None, {}
    m = mkts[0]
    sem = parse_semantics(ev)
    try:
        prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        tokens = json.loads(m["clobTokenIds"])
    except (ValueError, KeyError, TypeError):
        return None, None, sem
    if len(prices) != len(tokens) or max(prices, default=0) < 0.99:
        return None, None, sem       # not resolved yet
    win = tokens[prices.index(max(prices))]
    lose = next((t for t in tokens if t != win), None)
    return win, lose, sem


def harvest_tape(now: int, back: int, out: dict) -> None:
    """What the DECIDED side actually cost in the final hour of expired 4h
    windows. Books aren't backfillable, but prints are: every BUY row on the
    winning token is a fill a taker like us could have had. VWAP vs the $1
    payout is the gross harvest edge that was really on the table."""
    print(f"\n{'='*118}\nHARVEST TAPE — BUY prints on the WINNING token, by phase "
          f"(data-api /trades, historical)\n"
          f"gross edge = 1 - VWAP (before the {0.07:.2f}*min(p,1-p) taker fee)\n{'='*118}")
    print(f"{'slug':<28s} {'ph':<7s} {'n':>5s} {'usd':>8s} {'vwap':>6s} {'p10':>6s} "
          f"{'p90':>6s} {'gross edge':>11s}")
    agg: dict[tuple[str, str], list] = {}
    for k in range(1, back + 1):
        start = (now // DUR) * DUR - k * DUR
        for sym in SYMBOLS:
            slug = slug_for(sym, start)
            try:
                win, _lose, sem = resolved_tokens(slug)
                if not win:
                    print(f"{slug:<28s} (unresolved / no outcome prices)")
                    continue
                rows = trades_for(slug)
            except Exception as e:  # noqa: BLE001
                print(f"{slug:<28s} ERROR {e}")
                continue
            end = sem["end"]
            for label, lo, hi in (("60-30m", 1800, 3600), ("30-10m", 600, 1800),
                                  ("last10m", 0, 600)):
                sel = [(float(t["price"]), float(t["size"])) for t in rows
                       if t.get("asset") == win and t.get("side") == "BUY"
                       and lo <= end - float(t.get("timestamp") or 0) < hi]
                if not sel:
                    print(f"{slug:<28s} {label:<7s} {'0':>5s}")
                    continue
                usd = sum(p * s for p, s in sel)
                vwap = usd / sum(s for _p, s in sel)
                px = sorted(p for p, _s in sel)
                p10 = px[int(0.1 * (len(px) - 1))]
                p90 = px[int(0.9 * (len(px) - 1))]
                print(f"{slug:<28s} {label:<7s} {len(sel):>5d} {usd:>8.0f} {vwap:>6.3f} "
                      f"{p10:>6.3f} {p90:>6.3f} {(1 - vwap) * 100:>10.1f}c")
                cell = agg.setdefault((sym, label), [0.0, 0.0, 0])
                cell[0] += usd
                cell[1] += sum(s for _p, s in sel)
                cell[2] += len(sel)
                out.setdefault("harvest", []).append(
                    {"slug": slug, "phase": label, "n": len(sel), "usd": usd, "vwap": vwap})
    print(f"\n{'POOLED':<28s} {'ph':<7s} {'n':>5s} {'usd':>8s} {'vwap':>6s} "
          f"{'gross edge':>11s}  (all sampled windows)")
    for (sym, label), (usd, shares, n) in sorted(agg.items()):
        vwap = usd / shares if shares else 0.0
        print(f"{sym:<28s} {label:<7s} {n:>5d} {usd:>8.0f} {vwap:>6.3f} "
              f"{(1 - vwap) * 100:>10.1f}c")


def gated_harvest(now: int, back: int, theta: float, out: dict) -> None:
    """The harvest price CONDITIONED on our own entry gate, print by print.

    Books aren't backfillable but prints are, and every BUY print is an ask
    somebody crossed — liquidity we could have competed for. For each print
    this replays eval_model at THAT print's timestamp (no ex-post outcome
    conditioning: the side is the model's side at the time, not the winner)
    and keeps only prints on the side the gate would have let us buy. The
    resulting VWAP is what the tail-harvest would actually have paid; `won%`
    is the realised outcome of those same windows.

    Reported by rem bucket, so the shape of the convergence (how much edge is
    left 60/30/10 minutes out) is visible instead of averaged away."""
    import fourh_fit as ff  # noqa: PLC0415 — optional, only this section needs it

    series: dict[str, dict] = {}
    for sym in SYMBOLS:
        long = next(k for k, v in ff.CK_SHORT.items() if v == sym)
        series[sym] = ff.r6.build_series(ff.r6.load_cache(long))
    print(f"\n{'='*118}\nGATE-CONDITIONED HARVEST — every BUY print on the side our gate would "
          f"have been buying (theta={theta}), by rem\n"
          f"gate = |proj margin| >= guard AND side-signed safety >= theta, replayed from klines "
          f"at each print's own timestamp\n{'='*118}")
    phases = (("60-30m", 1800, 3600), ("30-10m", 600, 1800), ("last10m", 0, 600))
    # (symbol, phase) -> [usd, shares, prints, windows, usd_at_1.5c+, wins, window_ids]
    agg: dict[tuple[str, str], list] = {}
    # same keys -> [sum of true edge x size, shares, prints] under the CORRECT model
    agg_t: dict[tuple[str, str], list] = {}
    for k in range(1, back + 1):
        start = (now // DUR) * DUR - k * DUR
        for sym in SYMBOLS:
            slug = slug_for(sym, start)
            long = next(k2 for k2, v in ff.CK_SHORT.items() if v == sym)
            s = series[sym]
            end = start + DUR
            if not s["ts"] or not ff.r6.window_ready(s, start, end):
                continue
            try:
                win, _lose, sem = resolved_tokens(slug)
                if not win:
                    continue
                rows = trades_for(slug)
            except Exception as e:  # noqa: BLE001
                print(f"{slug:<28s} ERROR {e}")
                continue
            tok = {"up": sem["token_up"], "down": sem["token_down"]}
            cache: dict[int, dict | None] = {}
            tcache: dict[int, dict | None] = {}
            for t in rows:
                if t.get("side") != "BUY":
                    continue
                ts = float(t.get("timestamp") or 0)
                rem = end - ts
                phase = next((p for p, lo, hi in phases if lo <= rem < hi), None)
                if phase is None:
                    continue
                bucket = int(ts // 15) * 15      # 15s model-eval cache
                if bucket not in cache:
                    cache[bucket] = ff.tick_state(s, ff.GUARD_BP[long], start, end, bucket)
                st = cache[bucket]
                if st is None or not st["gate_ok"] or st["safety"] < theta:
                    continue
                if t.get("asset") != tok[st["side"]]:
                    continue                      # print on the side we would NOT buy
                px, sz = float(t["price"]), float(t["size"])
                fee = 0.07 * min(px, 1 - px)
                # what the CORRECT (terminal-TWAP digital) model says that same
                # side is worth at that instant — the edge that actually exists
                tst = tcache.get(bucket, "miss")
                if tst == "miss":
                    tst = ff.terminal_state(s, ff.GUARD_BP[long], start, end, bucket)
                    tcache[bucket] = tst
                if tst is not None:
                    fair_t = tst["p_up"] if st["side"] == "up" else 1.0 - tst["p_up"]
                    cell_t = agg_t.setdefault((sym, phase), [0.0, 0.0, 0])
                    cell_t[0] += (fair_t - px - fee) * sz     # size-weighted true edge
                    cell_t[1] += sz
                    cell_t[2] += 1
                cell = agg.setdefault((sym, phase), [0.0, 0.0, 0, set(), 0.0, 0, 0])
                cell[0] += px * sz
                cell[1] += sz
                cell[2] += 1
                cell[3].add(start)
                if 1.0 - px - fee >= 0.015:       # clears the live min_edge
                    cell[4] += px * sz
                cell[5] += 1 if tok[st["side"]] == win else 0
                cell[6] += 1
    print(f"{'symbol':<7s} {'rem':<8s} {'wins':>5s} {'prints':>7s} {'usd':>9s} {'vwap':>6s} "
          f"{'gross':>7s} {'net of fee':>11s} {'$ at >=1.5c net':>16s} {'side won%':>10s}")
    for (sym, phase), (usd, shares, n, wids, usd_edge, wins, tot) in sorted(
            agg.items(), key=lambda kv: (kv[0][0], [p for p, _l, _h in phases].index(kv[0][1]))):
        vwap = usd / shares if shares else 0.0
        fee = 0.07 * min(vwap, 1 - vwap)
        print(f"{sym:<7s} {phase:<8s} {len(wids):>5d} {n:>7d} {usd:>9.0f} {vwap:>6.3f} "
              f"{(1 - vwap) * 100:>6.1f}c {(1 - vwap - fee) * 100:>10.1f}c {usd_edge:>16.0f} "
              f"{100.0 * wins / tot if tot else 0:>9.1f}%")
    print(f"\nSAME PRINTS, PRICED BY THE MODEL THAT ACTUALLY SETTLES (terminal 60s-TWAP "
          f"digital): mean net edge per share, size-weighted")
    print(f"{'symbol':<7s} {'rem':<8s} {'prints':>7s} {'shares':>9s} {'true net edge/share':>20s}")
    for (sym, phase), (edge, shares, n) in sorted(
            agg_t.items(), key=lambda kv: (kv[0][0], [p for p, _l, _h in phases].index(kv[0][1]))):
        print(f"{sym:<7s} {phase:<8s} {n:>7d} {shares:>9.0f} "
              f"{(edge / shares if shares else 0) * 100:>19.1f}c")
    out["gated_harvest"] = {f"{s}|{p}": [v[0], v[1], v[2], len(v[3]), v[4], v[5], v[6]]
                            for (s, p), v in agg.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--back", type=int, default=2, help="how many expired 4h windows back")
    ap.add_argument("--json", dest="as_json", action="store_true")
    ap.add_argument("--skip-live", action="store_true")
    ap.add_argument("--skip-expired", action="store_true")
    ap.add_argument("--gated-harvest", action="store_true",
                    help="harvest prints split by whether our entry gate was open")
    ap.add_argument("--theta", type=float, default=1.0, help="entry gate theta for --gated-harvest")
    ap.add_argument("--harvest", action="store_true",
                    help="BUY-print tape on the winning token of expired windows")
    ap.add_argument("--repeat", type=int, default=1, help="live snapshots to take")
    ap.add_argument("--every", type=float, default=600.0, help="seconds between live snapshots")
    args = ap.parse_args()
    now = int(time.time())
    out: dict = {"now": now}
    if not args.skip_live:
        for i in range(max(args.repeat, 1)):
            if i:
                time.sleep(args.every)
            snapshot(int(time.time()), out)
    if not args.skip_expired:
        expired_tape(now, args.back, out)
    if args.harvest:
        harvest_tape(now, args.back, out)
    if args.gated_harvest:
        gated_harvest(now, args.back, args.theta, out)
    if args.as_json:
        print("\nJSON\n" + json.dumps(out))


if __name__ == "__main__":
    main()
