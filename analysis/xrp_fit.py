"""xrp 5m fit from the RTDS settlement stream — the stream-fed re-add gate.

xrp's Binance-vs-Chainlink basis was the original blow-up: the model read
Binance while settlement read Chainlink, and a guard wide enough to be safe
never traded. The RTDS recorder now captures the SETTLEMENT STREAM itself
(1Hz), so this fit uses only stream quantities — the world a stream-fed arm
would actually live in. Decidedness: P(final winner == sign of live stream
margin at elapsed frac f, |margin| >= m). Peers on the same tape are the
control: xrp "fits" if its curve at reachable margins matches symbols the
wallet already proved.
"""
import json, sys
from bisect import bisect_right
from collections import defaultdict

CORPUS = "/home/hunter/.pmt/corpus/rtds/rtds-20260823.jsonl"
SYMS = ["xrp/usd", "btc/usd", "eth/usd", "sol/usd", "bnb/usd", "doge/usd"]

series = defaultdict(lambda: ([], []))  # (sym, topic) -> (ts_list, val_list)
with open(CORPUS) as fh:
    for line in fh:
        try: r = json.loads(line)
        except ValueError: continue
        sym, topic = r.get("symbol"), r.get("topic")
        if sym in SYMS and topic in ("crypto_prices_chainlink", "crypto_prices_twap_thirty"):
            ts, vs = series[(sym, topic)]
            ts.append(r["ts"]/1000.0); vs.append(r["value"])
for k in series:
    ts, vs = series[k]
    order = sorted(range(len(ts)), key=ts.__getitem__)
    series[k] = ([ts[i] for i in order], [vs[i] for i in order])

def at(sym, topic, t):
    ts, vs = series[(sym, topic)]
    i = bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > 30: return None
    return vs[i]

def windows(sym):
    ts, _ = series[(sym, "crypto_prices_twap_thirty")]
    t0, t1 = ts[0], ts[-1]
    start = (int(t0) // 300 + 1) * 300
    out = []
    while start + 300 <= t1:
        ref = at(sym, "crypto_prices_twap_thirty", start)
        settle = at(sym, "crypto_prices_twap_thirty", start + 300)
        if ref and settle:
            out.append((start, ref, settle, "up" if settle > ref else "down"))
        start += 300
    return out

print(f"{'sym':>9} {'win':>4} {'|settle m|bp p50/p90':>21} {'1m sig bp':>9}  decidedness at elapsed>=40%, |margin|>= 4 / 6 / 10bp")
for sym in SYMS:
    if (sym, "crypto_prices_twap_thirty") not in series: continue
    ws = windows(sym)
    margins = sorted(abs(s/r-1)*1e4 for _, r, s, _ in ws)
    if not margins: continue
    p50 = margins[len(margins)//2]; p90 = margins[int(len(margins)*0.9)]
    # 1m realized sigma from the chainlink stream
    ts, vs = series[(sym, "crypto_prices_chainlink")]
    rets = []
    last_t, last_v = ts[0], vs[0]
    for t, v in zip(ts, vs):
        if t - last_t >= 60:
            rets.append((v/last_v - 1) * 1e4); last_t, last_v = t, v
    sig = (sum(r*r for r in rets)/len(rets))**0.5 if rets else 0.0
    cells = []
    for mth in (4.0, 6.0, 10.0):
        n = right = 0
        for start, ref, settle, winner in ws:
            for frac in (0.4, 0.5, 0.6, 0.7):
                spot = at(sym, "crypto_prices_chainlink", start + 300*frac)
                if spot is None: continue
                m = (spot/ref - 1) * 1e4
                if abs(m) >= mth:
                    n += 1
                    right += (m > 0) == (winner == "up")
        cells.append(f"{right/n*100:3.0f}% (n={n})" if n else "  — (n=0)")
    print(f"{sym:>9} {len(ws):>4} {p50:>9.1f}/{p90:<9.1f} {sig:>9.1f}  " + "  ".join(cells))
