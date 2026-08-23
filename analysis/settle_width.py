"""Which TWAP width do 5m updown markets actually settle on — 30s or 60s?

The engine assumes 30s at 5m (`updown_model::settle_tw_secs`, mirrored by
`outcomes.ck_settlement_width_s`). This regrades every graded window the RTDS
recorder corpus covers under both widths, using the terminal rule the markets
actually use (settlement-stream value at range END vs at range START), and
reports which width agrees with observed truth more.

Truth sources, in the order they are trusted:
  wallet — redemptions. Ground truth, but we only hold windows we FILLED, and
           those are momentum-selected, so they are almost never near-tie:
           in this corpus ZERO wallet windows are width-discriminating.
  book   — the market's own terminal book pinned >= 0.95. Independent of our
           model and of the Chainlink stream. Validated here against the
           wallet before it is leaned on.

The model never grades itself: the only inputs are the recorded stream, the
wallet's redemptions and the market's book.

Run: uv run --project pmtrader python analysis/settle_width.py
"""

from __future__ import annotations

import bisect
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

CORPUS = Path(os.path.expanduser("~/.pmt/corpus"))
RTDS_DIR = CORPUS / "rtds"
OUTCOMES = CORPUS / "outcomes.jsonl"
BOOK_TAPES = [
    Path(os.path.expanduser("~/.pmt/engine/book-tape.jsonl")),
    CORPUS / "book-tape-20260823-snapshot.jsonl",
]

TOPIC_SPOT = "crypto_prices_chainlink"
TOPIC_W = {"crypto_prices_twap_thirty": 30, "crypto_prices_twap_sixty": 60}
DUR_S = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}
TOL_S = 3  # a print may be this stale and still stand in for "the value AT ts"


def parse_slug(slug: str):
    """`btc-updown-5m-1787436000` -> (symbol, dur_s, start, dur). None if not one."""
    parts = slug.split("-")
    if len(parts) != 4 or parts[1] != "updown":
        return None
    sym, dur, ts = parts[0], parts[2], parts[3]
    if dur not in DUR_S or not ts.isdigit():
        return None
    return f"{sym}/usd", DUR_S[dur], int(ts), dur


def load_stream(want_spot: bool = False):
    """(marks, spot, gaps) — marks[(symbol, width)][ts_s] = value."""
    marks: dict[tuple[str, int], dict[int, float]] = defaultdict(dict)
    spot: dict[str, dict[int, float]] = defaultdict(dict)
    gaps: list[tuple[float, float]] = []
    for path in sorted(RTDS_DIR.glob("rtds-*.jsonl")):
        with path.open() as f:
            for line in f:
                r = json.loads(line)
                topic = r.get("topic")
                if topic is None:
                    # recorder meta: a reconnect row names the dark span.
                    if r.get("ev") == "reconnect" and r.get("down_s"):
                        t = r["t_recv"]
                        gaps.append((t - r["down_s"], t))
                    continue
                if want_spot and topic == TOPIC_SPOT:
                    spot[r["symbol"]][r["ts"] // 1000] = r["value"]
                    continue
                w = TOPIC_W.get(topic)
                if w is not None:
                    marks[(r["symbol"], w)][r["ts"] // 1000] = r["value"]
    return marks, spot, gaps


def at(series: dict[int, float], keys: list[int], ts: int, tol: int = TOL_S):
    """The print AT `ts` (the TWAP over the window ending there), or the
    nearest earlier one within `tol`. Never looks forward: a later print
    averages seconds the settlement could not have seen."""
    v = series.get(ts)
    if v is not None:
        return v, 0
    i = bisect.bisect_right(keys, ts) - 1
    if i < 0:
        return None, None
    lag = ts - keys[i]
    return (series[keys[i]], lag) if lag <= tol else (None, None)


class Stream:
    def __init__(self, want_spot: bool = False):
        self.marks, self.spot, self.gaps = load_stream(want_spot)
        self.keys = {k: sorted(v) for k, v in self.marks.items()}
        self.cover = {k: (v[0], v[-1]) for k, v in self.keys.items() if v}

    def verdict(self, sym: str, start: int, end: int, w: int):
        """(winner, margin_bp) under a `w`-second settlement TWAP, or None."""
        k = (sym, w)
        c = self.cover.get(k)
        if not c or not (c[0] <= start and end <= c[1]):
            return None
        a, _ = at(self.marks[k], self.keys[k], start)
        b, _ = at(self.marks[k], self.keys[k], end)
        if a is None or b is None:
            return None
        return ("up" if b > a else "down"), (b / a - 1.0) * 1e4


def load_outcomes():
    out = []
    for line in OUTCOMES.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        p = parse_slug(r["slug"])
        if p:
            out.append({"slug": r["slug"], "truth": r["winner"], "src": r["source"],
                        "sym": p[0], "dur_s": p[1], "start": p[2], "dur": p[3]})
    return out


# ---------- (1) is the terminal-book grader safe to lean on? ----------

def validate_book(rows):
    """Book grader vs wallet truth on the windows the wallet actually graded.
    A single mismatch here would disqualify the book as a truth proxy."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pmtrader"))
    from polymarket.outcomes import book_outcome  # noqa: PLC0415

    wallet = {r["slug"]: r for r in rows if r["src"] == "wallet"}
    recs: dict[str, list[dict]] = defaultdict(list)
    for path in BOOK_TAPES:
        if not path.exists():
            continue
        with path.open() as f:
            for line in f:
                r = json.loads(line)
                if r.get("slug") in wallet:
                    recs[r["slug"]].append(r)
    agree = dis = drop = 0
    bad = []
    for slug, w in wallet.items():
        winner, _ = book_outcome({"end": w["start"] + w["dur_s"]}, recs.get(slug, []))
        if winner is None:
            drop += 1
        elif winner == w["truth"]:
            agree += 1
        else:
            dis += 1
            bad.append((slug, w["truth"], winner))
    return agree, dis, drop, bad


# ---------- (2) do the stream topics carry the widths they claim? ----------

def validate_topics(st: Stream, syms=("btc/usd", "eth/usd")):
    """Trailing W-second means rebuilt from the 1Hz chainlink topic, matched
    against both TWAP topics. Guards against the alternative explanation for
    everything below: a mislabeled or lagged twap_thirty relay."""
    out = []
    for sym in syms:
        s = st.spot.get(sym) or {}
        ks = sorted(s)
        for w in (30, 60):
            errs = {30: [], 60: []}
            for t in ks[600:5600:7]:
                vals = [s[u] for u in range(t - w + 1, t + 1) if u in s]
                if len(vals) < w - 2:
                    continue
                m = sum(vals) / len(vals)
                for tw in (30, 60):
                    v = st.marks[(sym, tw)].get(t)
                    if v:
                        errs[tw].append(abs(m / v - 1) * 1e4)
            if errs[30] and errs[60]:
                out.append((sym, w, statistics.median(errs[30]),
                            statistics.median(errs[60]), len(errs[30])))
    return out


# ---------- (3) the regrade ----------

def regrade(st: Stream, rows, src: str, dur: str):
    graded, flips, misses = [], [], []
    for r in rows:
        if r["src"] != src or r["dur"] != dur:
            continue
        end = r["start"] + r["dur_s"]
        v = {w: st.verdict(r["sym"], r["start"], end, w) for w in (30, 60)}
        if v[30] is None or v[60] is None:
            continue
        rec = {**r, "w30": v[30], "w60": v[60]}
        graded.append(rec)
        if v[30][0] != v[60][0]:
            flips.append(rec)
        if v[30][0] != r["truth"] or v[60][0] != r["truth"]:
            misses.append(rec)
    return graded, flips, misses


def fmt(r):
    return (f"{r['slug']:<32} truth={r['truth']:<5} "
            f"30s={r['w30'][0]:<5}{r['w30'][1]:>8.2f}bp  "
            f"60s={r['w60'][0]:<5}{r['w60'][1]:>8.2f}bp")


if __name__ == "__main__":
    st = Stream(want_spot=True)
    rows = load_outcomes()

    print("== topic sanity: does twap_thirty really average 30s? ==")
    for sym, w, e30, e60, n in validate_topics(st):
        print(f"  {sym} recon{w}s: |err| vs twap30 {e30:.3f}bp  vs twap60 {e60:.3f}bp  (n={n})")

    print("\n== book grader vs wallet ground truth ==")
    a, d, drop, bad = validate_book(rows)
    print(f"  agree {a}  disagree {d}  ungradable {drop}")
    for b in bad:
        print(f"  MISMATCH {b}")

    print("\n== regrade: terminal rule at 30s vs 60s ==")
    for src in ("wallet", "book"):
        for dur in ("5m", "15m"):
            graded, flips, misses = regrade(st, rows, src, dur)
            if not graded:
                continue
            n = len(graded)
            a30 = sum(r["w30"][0] == r["truth"] for r in graded)
            a60 = sum(r["w60"][0] == r["truth"] for r in graded)
            print(f"  {src:<7} {dur:<4} n={n:<4} 30s={a30}/{n} ({a30/n*100:.1f}%)  "
                  f"60s={a60}/{n} ({a60/n*100:.1f}%)  width-discriminating={len(flips)}")
            for r in sorted(misses, key=lambda r: r["start"]):
                who = ("60s right" if r["w60"][0] == r["truth"]
                       else "30s right" if r["w30"][0] == r["truth"] else "BOTH wrong")
                print(f"      {fmt(r)}  -> {who}")
