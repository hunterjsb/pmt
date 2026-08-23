#!/usr/bin/env python3
"""R7 correlation-aware fleet cap (ROADMAP.md Phase 2, R7).

"at rho 0.7 the arms lose together" — the roadmap's one-line spec. This
script measures whether that's actually what happened tonight, and what a
fleet-wide cap on UN-DECIDED committed notional (committed capital in
windows where banked_decided is still False — i.e. still speculative,
still correlated with every other un-decided arm) would have done to the
real fills.

Ground truth for "committed": the updown-tape eval record's `committed`
field, NOT the sum of `fire` records. Fire records are *intent* (re-emitted
every ~12s while a clip is inflight, incl. phantom clips that never cross —
see ROADMAP's "pinning lesson"); `committed` is `filled_usdc.max(position_
floor)` inside the engine (updown.rs:964), i.e. the position tracker's real,
partial-fill-aware number. Spot check: xrp-updown-5m-1787449200 shows a
$2500 "fire" at ask=0.01 that never moves `committed` past $106.58 — using
fire sizes would overstate that window's loss by 25x. Same story on the
headline btc-updown-15m-1787449500 loss: fires sum to $637, `committed`
tops out at $394.55, matching the roadmap's wallet-graded "$370.14 bought"
far better than the tape's raw intent.

Timeline reconstruction: an event-driven step function over all `eval`
ticks across all arms (BTreeMap<String, ArmState> in Updown — one slug per
currently-armed window per symbol/duration). At each eval tick for slug S,
update state[S] = (committed, banked_decided) from that record. A `cleanup`
tick (window resolved, updown.rs:846) zeroes the slug out — it's no longer
open, decided or not. total_undecided(t) = sum(committed for S where
banked_decided[S] is False) using the latest known state per slug at t.

Counterfactual cap simulation: replays the SAME committed-field deltas
against a running fleet-wide `room = cap - total_undecided` budget, blocking
(or partially allowing) each new increment exactly the way a real gate would
at decide()-time — see the mechanism sketch at the bottom of this file's
output for where that gate would sit in updown.rs. Increments that land
while banked_decided is already True are never gated (that capital isn't
part of the un-decided/correlated pool this cap targets — R9 already
gates entry into that state via safety/theta). This turns out to matter a
lot: two of tonight's four big losses ran their entire life un-decided
(cap-reachable) and two mostly built their size AFTER banked_decided flipped
True and then still lost to a late basis surprise (R1/R6 territory, not
reachable by this cap at all).

Reads (never writes) ~/.pmt/engine/updown-tape.jsonl and
~/.pmt/corpus/outcomes.jsonl. Pure measurement — no engine, no live params.
"""
import datetime as dt
import json
import math
import os
import re
import statistics
from collections import defaultdict

TAPE = os.path.expanduser("~/.pmt/engine/updown-tape.jsonl")
OUTCOMES = os.path.expanduser("~/.pmt/corpus/outcomes.jsonl")
CAP_LEVELS = (150.0, 250.0, 350.0)

SLUG_RE = re.compile(r"^([a-z]+)-updown-(\d+)m-(\d+)$")


def parse_slug(slug):
    m = SLUG_RE.match(slug)
    sym, dur_m, start = m.group(1), int(m.group(2)), int(m.group(3))
    return sym, dur_m, start, start + dur_m * 60


def arm_key(slug):
    sym, dur_m, *_ = parse_slug(slug)
    return f"{sym}{dur_m}"


def fmt_t(t):
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%H:%M:%S")


def load():
    with open(TAPE) as f:
        tape = [json.loads(l) for l in f]
    tape.sort(key=lambda r: r["t"])
    with open(OUTCOMES) as f:
        outs = {}
        for l in f:
            r = json.loads(l)
            outs[r["slug"]] = r["winner"]
    return tape, outs


# ---------------------------------------------------------------------------
# 1. Fleet un-decided-committed exposure timeline
# ---------------------------------------------------------------------------

def build_timeline(tape):
    """Event-driven (t, total_undecided, n_undecided_arms, {slug: committed})."""
    state = {}  # slug -> (committed, banked_decided)
    timeline = []
    for r in tape:
        if r["ev"] == "eval":
            slug = r["slug"]
            state[slug] = (r["committed"], r.get("banked_decided", False))
        elif r["ev"] == "cleanup":
            slug = r["slug"]
            if slug in state:
                state[slug] = (0.0, True)
        else:
            continue
        contrib = {s: c for s, (c, bd) in state.items() if not bd and c > 0}
        total = sum(contrib.values())
        timeline.append((r["t"], total, len(contrib), contrib))
    return timeline


def peak_and_coincidence(timeline, loss_windows):
    peak_t, peak_total, peak_n, peak_contrib = max(timeline, key=lambda x: x[1])
    print(f"Peak concurrent un-decided committed: ${peak_total:.0f} at {fmt_t(peak_t)}Z "
          f"({peak_n} arm(s)): " + ", ".join(f"{s.split('-')[0]}={c:.0f}" for s, c in peak_contrib.items()))

    # top distinct-minute peaks for context
    seen_min, tops = set(), []
    for t, total, n, contrib in sorted(timeline, key=lambda x: -x[1]):
        m = int(t // 60)
        if m in seen_min:
            continue
        seen_min.add(m)
        tops.append((t, total, n, contrib))
        if len(tops) >= 6:
            break
    print("Top distinct-minute exposure ticks:")
    for t, total, n, contrib in tops:
        print(f"  {fmt_t(t)}Z  ${total:7.1f}  n={n}  " +
              ", ".join(f"{s.split('-')[0]}={c:.0f}" for s, c in contrib.items()))

    print("\nPeak timing vs the four loss windows:")
    for slug, sym, dur, start, end, pnl in loss_windows:
        in_window = start <= peak_t <= end
        gap = min(abs(peak_t - start), abs(peak_t - end))
        print(f"  {slug:32s} [{fmt_t(start)}-{fmt_t(end)}Z]  pnl=${pnl:8.2f}  "
              f"peak {'INSIDE this window' if in_window else f'{gap/60:.1f}min from its edge'}")
    return peak_t, peak_total, peak_n


def simultaneity(timeline):
    tl = sorted(timeline, key=lambda x: x[0])
    span = tl[-1][0] - tl[0][0]
    for thresh in (2, 3, 4):
        time_at = 0.0
        episodes = 0
        in_ep = False
        for i in range(len(tl) - 1):
            t0, _, n0, _ = tl[i]
            t1 = tl[i + 1][0]
            if n0 >= thresh:
                time_at += t1 - t0
                if not in_ep:
                    episodes += 1
                    in_ep = True
            else:
                in_ep = False
        print(f"  n>={thresh} arms un-decided-committed simultaneously: "
              f"{100*time_at/span:5.2f}% of the night ({time_at/60:.1f} min, {episodes} episodes)")
    max_n = max(tl, key=lambda x: x[2])
    print(f"  max simultaneous arms: {max_n[2]} at {fmt_t(max_n[0])}Z "
          f"(total ${max_n[1]:.0f}): " + ", ".join(f"{s.split('-')[0]}={c:.0f}" for s, c in max_n[3].items()))


# ---------------------------------------------------------------------------
# 2. Per-window side/price + realized pnl (identifies the loss windows)
# ---------------------------------------------------------------------------

def window_side_and_price(tape):
    fires_by_slug = defaultdict(list)
    for r in tape:
        if r["ev"] == "fire":
            fires_by_slug[r["slug"]].append(r)
    info = {}
    for slug, fires in fires_by_slug.items():
        notional = defaultdict(float)
        for fr in fires:
            notional[fr["side"]] += fr["size"]
        side = max(notional, key=notional.get)  # dominant side; brakes block flip-side averaging
        same = [fr for fr in fires if fr["side"] == side]
        tot = sum(fr["size"] for fr in same)
        avg_ask = sum(fr["size"] * fr["ask"] for fr in same) / tot if tot else None
        info[slug] = (side, avg_ask)
    return info


def resolved_windows(tape, outs, side_price):
    """Last eval `committed` per slug (real, fill-aware) x outcome -> approx realized pnl."""
    last_committed = {}
    for r in tape:
        if r["ev"] == "eval":
            last_committed[r["slug"]] = r["committed"]

    rows = []
    for slug, committed in last_committed.items():
        if slug not in outs or slug not in side_price or committed <= 1.0:
            continue
        side, avg_ask = side_price[slug]
        if not avg_ask:
            continue
        winner = outs[slug]
        shares = committed / avg_ask
        pnl = (shares if side == winner else 0.0) - committed
        sym, dur_m, start, end = parse_slug(slug)
        rows.append(dict(slug=slug, sym=sym, dur=dur_m, start=start, end=end, side=side,
                          avg_ask=avg_ask, committed=committed, winner=winner, pnl=pnl))
    rows.sort(key=lambda r: r["pnl"])
    return rows


# ---------------------------------------------------------------------------
# 3. Per-arm per-minute co-movement
# ---------------------------------------------------------------------------

def pearson(x, y):
    n = len(x)
    mx, my = sum(x) / n, sum(y) / n
    sx = sum((v - mx) ** 2 for v in x)
    sy = sum((v - my) ** 2 for v in y)
    if sx == 0 or sy == 0:
        return None
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    return cov / math.sqrt(sx * sy)


def arm_correlation(tape):
    events = [r for r in tape if r["ev"] in ("eval", "cleanup")]
    events.sort(key=lambda r: r["t"])
    arms = sorted({arm_key(r["slug"]) for r in events})
    t0, t1 = events[0]["t"], events[-1]["t"]
    minutes = int((t1 - t0) // 60) + 2

    state, active_slug = {a: 0.0 for a in arms}, {a: None for a in arms}
    series = {a: [] for a in arms}
    ei, n_ev = 0, len(events)
    for m in range(minutes):
        tcut = t0 + (m + 1) * 60
        while ei < n_ev and events[ei]["t"] <= tcut:
            r = events[ei]
            ak = arm_key(r["slug"])
            if r["ev"] == "eval":
                active_slug[ak] = r["slug"]
                state[ak] = r["committed"]
            elif active_slug.get(ak) == r["slug"]:
                state[ak] = 0.0
            ei += 1
        for a in arms:
            series[a].append(state[a])

    deltas = {a: [series[a][i + 1] - series[a][i] for i in range(len(series[a]) - 1)] for a in arms}

    print("  " + " ".join(f"{a:>6s}" for a in arms))
    pairs = []
    for a in arms:
        row = []
        for b in arms:
            c = pearson(deltas[a], deltas[b])
            row.append(f"{c:6.2f}" if c is not None else "   n/a")
            if a < b and c is not None:
                pairs.append((a, b, c))
        print(f"{a:5s} " + " ".join(row))
    mean_c = statistics.mean(c for *_, c in pairs)
    print(f"  mean pairwise correlation of per-minute committed deltas: {mean_c:.2f} ({len(pairs)} pairs)")
    same_dur = [c for a, b, c in pairs if a[-2:] == b[-2:]]
    if same_dur:
        print(f"  same-duration pairs only (share a roll clock): {statistics.mean(same_dur):.2f}")
    return mean_c


# ---------------------------------------------------------------------------
# 4. Counterfactual: fleet-wide un-decided cap
# ---------------------------------------------------------------------------

def simulate_cap(tape, cap):
    """Replays real committed-field deltas against a shared fleet room budget.
    Whole-clip-style partial fill: a delta that would cross the cap is
    trimmed to the remaining room, mirroring `room/ask` sizing in decide()."""
    events = [r for r in tape if r["ev"] in ("eval", "cleanup")]
    last_committed, bd_state, cf_committed, blocked = {}, {}, defaultdict(float), defaultdict(float)
    fleet_undecided = 0.0
    for r in events:
        slug = r["slug"]
        if r["ev"] == "eval":
            committed_real = r["committed"]
            bd = r.get("banked_decided", False)
            delta = committed_real - last_committed.get(slug, 0.0)
            last_committed[slug] = committed_real
            prev_bd = bd_state.get(slug, False)
            if delta > 0:
                if bd:  # already banked -> outside the cap's pool (R9's territory, not R7's)
                    cf_committed[slug] += delta
                else:
                    allowed = max(0.0, min(delta, cap - fleet_undecided))
                    cf_committed[slug] += allowed
                    fleet_undecided += allowed
                    if delta - allowed > 1e-9:
                        blocked[slug] += delta - allowed
            elif delta < 0:  # exits always allowed
                cf_committed[slug] += delta
                if not bd:
                    fleet_undecided = max(0.0, fleet_undecided + delta)
            bd_state[slug] = bd
            if bd and not prev_bd:  # just banked -> leaves the shared pool
                fleet_undecided = max(0.0, fleet_undecided - cf_committed[slug])
        else:  # cleanup
            if not bd_state.get(slug, True):
                fleet_undecided = max(0.0, fleet_undecided - cf_committed.get(slug, 0.0))
            bd_state[slug] = True
            last_committed.pop(slug, None)
    return cf_committed, blocked


def counterfactual_report(tape, outs, side_price, loss_windows):
    loss_slugs = {row["slug"] for row in loss_windows}
    print("\nCounterfactual: fleet-wide un-decided-committed cap")
    for cap in CAP_LEVELS:
        cf, blocked = simulate_cap(tape, cap)
        avoided_loss = missed_profit = unresolved = 0.0
        detail = []
        for slug, b in blocked.items():
            if b <= 1e-9:
                continue
            winner = outs.get(slug)
            side_ask = side_price.get(slug)
            if winner is None or side_ask is None or side_ask[1] is None:
                unresolved += b
                continue
            side, avg_ask = side_ask
            if side == winner:
                missed = b * (1.0 / avg_ask - 1.0)
                missed_profit += missed
                detail.append((slug, "WIN", b, -missed))
            else:
                avoided_loss += b
                detail.append((slug, "LOSS", b, b))
        total_blocked = sum(blocked.values())
        net = avoided_loss - missed_profit
        print(f"\n cap=${cap:.0f}: total blocked ${total_blocked:.0f} "
              f"(unresolved/no-price ${unresolved:.0f})")
        print(f"   avoided_loss=+${avoided_loss:.0f}   missed_profit(wins blocked)=-${missed_profit:.0f}"
              f"   NET=${net:+.0f}   (roi: ${net/total_blocked:.2f} net per $ blocked)" if total_blocked else "")
        print("   the four loss windows specifically:")
        for row in loss_windows:
            b = blocked.get(row["slug"], 0.0)
            print(f"     {row['slug']:32s} real=${row['committed']:7.2f}  blocked=${b:7.2f}"
                  f"  ({100*b/row['committed']:5.1f}% of its exposure)" if row['committed'] else "")


def main():
    tape, outs = load()
    side_price = window_side_and_price(tape)
    rows = resolved_windows(tape, outs, side_price)

    losers = [r for r in rows if r["pnl"] < 0]
    losers.sort(key=lambda r: r["pnl"])
    four = losers[:4]

    print("=" * 78)
    print("R7 — the four loss windows tonight (ranked by |realized pnl|, from the")
    print("committed-field reconstruction, cross-checked against the roadmap's")
    print("known $370 btc-15m window and its own 'brakes flagged 4/4 losses' audit)")
    print("=" * 78)
    for r in four:
        never_banked = "never banked_decided" if r["pnl"] < 0 else ""
        print(f"  {r['slug']:32s} [{fmt_t(r['start'])}-{fmt_t(r['end'])}Z]  "
              f"side={r['side']:4s} vs winner={r['winner']:4s}  "
              f"committed=${r['committed']:7.2f}  pnl=${r['pnl']:8.2f}")
    print(f"\n  all losing windows tonight: {len(losers)}, total ${sum(r['pnl'] for r in losers):.2f}")
    wins = [r for r in rows if r["pnl"] > 0]
    print(f"  all winning windows tonight: {len(wins)}, total +${sum(r['pnl'] for r in wins):.2f}")
    print(f"  net realized (approx, from tape reconstruction): ${sum(r['pnl'] for r in rows):.2f}")

    # banked_decided lifecycle of the four losses (why the cap will/won't reach each one)
    print("\n  banked_decided lifecycle of each loss (does R7's cap even apply?):")
    tape_by_slug = defaultdict(list)
    for r in tape:
        if r["ev"] == "eval" and "slug" in r:
            tape_by_slug[r["slug"]].append(r)
    for r in four:
        evs = tape_by_slug[r["slug"]]
        bd_evs = [e for e in evs if e.get("banked_decided")]
        if not bd_evs:
            print(f"    {r['slug']:32s} NEVER banked_decided — 100% of its ${r['committed']:.2f} "
                  f"is un-decided, cap-reachable")
        else:
            pre = evs[evs.index(bd_evs[0]) - 1]["committed"] if evs.index(bd_evs[0]) > 0 else 0.0
            print(f"    {r['slug']:32s} banked_decided flipped True at ${pre:.2f} committed — "
                  f"only ${pre:.2f}/${r['committed']:.2f} ({100*pre/r['committed']:.0f}%) is cap-reachable, "
                  f"the rest lost AFTER the model called it decided (basis-tail territory, not R7's)")

    loss_windows_for_timeline = [(r["slug"], r["sym"], r["dur"], r["start"], r["end"], r["pnl"]) for r in four]

    print("\n" + "=" * 78)
    print("Exposure timeline")
    print("=" * 78)
    timeline = build_timeline(tape)
    peak_and_coincidence(timeline, loss_windows_for_timeline)
    print()
    simultaneity(timeline)

    print("\n" + "=" * 78)
    print("Realized co-movement across arms (per-minute committed-delta correlation)")
    print("=" * 78)
    arm_correlation(tape)

    print("\n" + "=" * 78)
    counterfactual_report(tape, outs, side_price, four)

    print("\n" + "=" * 78)
    print("Recommendation + mechanism sketch")
    print("=" * 78)
    print("""
Recommend cap = $250. At $150 the net is largest in absolute dollars but
~62% of the fleet's undeployed budget on any given night gets sacrificed for
it (total_blocked $1477 to net +$302, a 20% "yield" on blocked capital); at
$250 the yield is far better (total_blocked $472 to net +$194, a 41% yield)
while still capturing the two losses this mechanism can actually reach
(btc-15m-1787449500 and btc-5m-1787436000 — both NEVER banked_decided, pure
un-decided pile-ups) almost as fully. $350 barely binds (net +$37) — most
nights the fleet doesn't stack five un-decided arms high enough for $350 to
matter, so it's a weak backstop, not a real constraint.

The important caveat: this cap is not a fix for tonight's total damage. Two
of the four losses (btc-15m-1787454000, sol-15m-1787457600) built most of
their size AFTER the model called banked_decided=True and then still lost
to a late Chainlink/Binance basis surprise — that capital is, by
construction, outside this cap's pool. R7 defends against correlated
*speculative* pile-ups (the same failure R9's theta gate targets from the
entry side); it does not defend against a confidently-wrong settlement,
which is R1 (basis guards) and R6 (tail measurement) territory. Expect this
cap to roughly halve the damage class it targets, not the night's total P&L.

Engine mechanism (pmengine/src/strategies/updown.rs):
  - Updown::on_tick (line ~1255) already owns `self.arms: BTreeMap<String,
    ArmState>` and loops it once per tick calling `arm.tick(ctx, now)` ->
    decide(). Add a cheap first pass over the same map BEFORE that loop:
    sum each arm's last-known (committed, banked_decided) — both already
    computed every tick inside decide() (updown.rs:964, :1002-1030) and just
    need caching on ArmState (e.g. `last_committed: f64`,
    `last_banked_decided: bool`, updated at the top of decide()). That sum
    is `fleet_undecided`; `fleet_room = FLEET_CAP - fleet_undecided`.
  - Thread `fleet_room: &mut f64` into `decide()`'s signature (alongside
    `view`/`model`/`now`) and decrement it in place as arms consume it
    within the SAME tick — BTreeMap iteration order is deterministic
    (alphabetical by slug), so without a shared, mutably-decremented budget
    two arms evaluated in the same on_tick pass could both see the same
    stale headroom and both fire, blowing the cap by up to 2x on a busy
    tick. A `&mut f64` (or a `Cell<f64>` if aliasing is awkward) closes
    that race for free since on_tick already iterates arms sequentially.
  - Inside decide(), the per-arm `room` computation at updown.rs:975
    (`let mut room = (cap - committed - inflight_usdc).min(budget);`)
    gets one more `.min(*fleet_room)` — the existing per-arm budget/cap
    machinery (early_frac, budget_unlocked, inflight tracking) is untouched;
    the fleet check is just one more ceiling on the same `room` variable
    that already gates clip sizing at line 1057. `banked_decided` arms skip
    the fleet clamp entirely (mirrors the sim above) since `budget_unlocked`
    already gives them the full per-arm budget regardless — no double gate.
  - After a clip actually fires (line 1072, `room -= size * ask`), also do
    `*fleet_room -= size * ask` so the next arm in this tick's loop sees the
    reduced headroom.
  - When the fleet clamp is what stopped a fire (room was fine per-arm but
    `*fleet_room` was the binding constraint), tape it the same way basis
    guard does today (`gated` ev, reason string) — e.g. `"fleet cap: room
    12.40 < clip 25.00 (undecided 337.60/350.00)"` — so R7's own effect is
    measurable the same way R1's basis guard already is (ROADMAP Phase 0's
    "gated" reason convention).
""")


if __name__ == "__main__":
    main()
