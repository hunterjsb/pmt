"""Stream-fed 5m hybrid A/B: settle_rule range_avg vs hybrid, both at the
60s settlement width Task 1 established (analysis/settle_width.md).

Every arm is re-armed as `feed=rtds` so the model reads the settlement stream
itself rather than the Binance proxy, and `settle_tw_s=60` so `terminal_lock`
and `twap_topic_for` both work off the width the markets actually settle on.
Tunables are the live per-symbol values out of `arms-state.json` — read only,
never written.

Both variants run through the interleaved fleet driver over ONE filtered 5m
book tape, so the $500 undecided cap is shared across symbols exactly as it is
live (`--slug ''` matches every slug in that filtered tape).

Truth is the wallet, with the terminal book as an optional volume extension —
never the model's own read.

Run: uv run --project pmtrader python analysis/hybrid_5m_ab.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PMENGINE = REPO / "pmengine/target/release/pmengine"
ENGINE_DIR = Path(os.path.expanduser("~/.pmt/engine"))
CORPUS = Path(os.path.expanduser("~/.pmt/corpus"))
RTDS = CORPUS / "rtds"
WORK = Path(os.environ.get("AB_WORK", "/tmp/claude-1000/-var-home-hunter/"
                           "35f80f35-e0c9-4e4d-80ea-9c5602f70444/scratchpad/ab"))

# Live per-symbol tunables, lifted from arms-state.json 2026-08-23 18:40Z.
LIVE = {
    "btc": dict(symbol="BTCUSDT", sigma_bp_per_min=9.076500723663472, size_usdc=1000.0,
                clip_usdc=150.0, basis_guard_bp=6.0, pay_up_max=0.05, maker_bid=False),
    "eth": dict(symbol="ETHUSDT", sigma_bp_per_min=25.064061879321613, size_usdc=900.0,
                clip_usdc=110.0, basis_guard_bp=6.0, pay_up_max=0.05, maker_bid=False),
    "sol": dict(symbol="SOLUSDT", sigma_bp_per_min=20.08802298623181, size_usdc=400.0,
                clip_usdc=50.0, basis_guard_bp=10.0, pay_up_max=0.04, maker_bid=True),
    "xrp": dict(symbol="XRPUSDT", sigma_bp_per_min=17.47947675411223, size_usdc=100.0,
                clip_usdc=10.0, basis_guard_bp=12.0, pay_up_max=0.02, maker_bid=True),
    "bnb": dict(symbol="BNBUSDT", sigma_bp_per_min=6.135096149820948, size_usdc=100.0,
                clip_usdc=10.0, basis_guard_bp=8.0, pay_up_max=0.02, maker_bid=False),
}
COMMON = dict(kind="twap", fee_rate=0.07, min_edge=0.015, max_price=0.985,
              quiesce_secs=20.0, min_fair=0.97, min_elapsed_frac=0.0,
              clip_cooldown_s=2.0, early_frac=0.2, early_min_edge=0.08,
              late_rem_s=120.0, rho_block=-0.25, p_cap=1.0, theta=0.3,
              manip_push_bp=25.0, roll=False, feed="rtds", settle_tw_s=60.0)
FLEET_CAP = "500"
INCIDENT = 1787505300  # the five-arm event, 2026-08-23 17:15:00Z


def rtds_span():
    lo, hi = None, None
    for p in sorted(RTDS.glob("rtds-*.jsonl")):
        with p.open() as f:
            for line in f:
                r = json.loads(line)
                if "topic" not in r:
                    continue
                t = r["ts"] // 1000
                lo = t if lo is None else min(lo, t)
                hi = t if hi is None else max(hi, t)
    return lo, hi


def five_m_slugs(lo: int, hi: int):
    """5m slugs the book tape covers that sit inside the stream's span."""
    seen = set()
    for line in (ENGINE_DIR / "book-tape.jsonl").open():
        try:
            s = json.loads(line).get("slug") or ""
        except Exception:
            continue
        if "-updown-5m-" not in s:
            continue
        sym, start = s.split("-")[0], int(s.split("-")[-1])
        if sym in LIVE and lo <= start and start + 300 <= hi:
            seen.add(s)
    return sorted(seen, key=lambda s: (int(s.split("-")[-1]), s))


def write_params(slugs, rule: str, path: Path):
    out = []
    for s in slugs:
        sym = s.split("-")[0]
        start = float(s.split("-")[-1])
        out.append({**COMMON, **LIVE[sym], "slug": s, "start": start, "end": start + 300.0,
                    "token_up": f"{s}-up", "token_down": f"{s}-down",
                    "settle_rule": rule})
    path.write_text(json.dumps(out))


def filter_tape(src: Path, dst: Path, slugs: set):
    n = 0
    with src.open() as f, dst.open("w") as o:
        for line in f:
            if "-updown-5m-" not in line:
                continue
            try:
                s = json.loads(line).get("slug")
            except Exception:
                continue
            if s in slugs:
                o.write(line)
                n += 1
    return n


def truth_rows(slugs: set, sources: tuple):
    rows = []
    for line in (CORPUS / "outcomes.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r["slug"] in slugs and r["source"] in sources:
            rows.append({"slug": r["slug"], "winner": r["winner"]})
    return rows


def stream_truth(slugs, lo, hi):
    """Terminal rule at the 60s width, straight off the recorded stream —
    used ONLY to grade the incident window, which the engine's own book tape
    stops 84s short of and which no wallet redemption or book pin covers."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from settle_width import Stream  # noqa: PLC0415

    st = Stream()
    out = []
    for s in slugs:
        sym = f"{s.split('-')[0]}/usd"
        start = int(s.split("-")[-1])
        v = st.verdict(sym, start, start + 300, 60)
        if v:
            out.append({"slug": s, "winner": v[0], "margin_bp": v[1]})
    return out


def run(rule: str, params: Path, outcomes: Path, book: Path, tape: Path, out: Path):
    cmd = [str(PMENGINE), "replay", "--mode", "full", "--slug", "",
           "--params", str(params), "--outcomes", str(outcomes),
           "--book-tape", str(book), "--tape", str(tape),
           "--rtds-corpus", str(RTDS), "--fleet-cap", FLEET_CAP,
           "--out", str(out), "--log-level", "warn"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    (out.with_suffix(".stdout")).write_text(r.stdout)
    (out.with_suffix(".stderr")).write_text(r.stderr)
    print(f"  [{rule}] exit={r.returncode}  report={out}")
    return r


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    lo, hi = rtds_span()
    print(f"rtds span {lo}..{hi} ({(hi - lo) / 3600:.2f}h)")
    slugs = five_m_slugs(lo, hi)
    print(f"5m slugs in span with book coverage: {len(slugs)}")
    print(f"incident {INCIDENT} present: "
          f"{sum(1 for s in slugs if s.endswith(str(INCIDENT)))} arms")

    sset = set(slugs)
    nb = filter_tape(ENGINE_DIR / "book-tape.jsonl", WORK / "book-5m.jsonl", sset)
    nt = filter_tape(ENGINE_DIR / "updown-tape.jsonl", WORK / "eval-5m.jsonl", sset)
    print(f"filtered tapes: book {nb} rows, eval {nt} rows")

    wallet = truth_rows(sset, ("wallet",))
    walbook = truth_rows(sset, ("wallet", "book"))
    incident = [r for r in stream_truth([s for s in slugs if s.endswith(str(INCIDENT))], lo, hi)]
    print(f"truth: wallet {len(wallet)}  wallet+book {len(walbook)}  "
          f"incident(stream) {len(incident)}")
    for r in incident:
        print(f"   {r['slug']:<32} {r['winner']}  {r['margin_bp']:+.2f}bp")

    graded = {r["slug"] for r in walbook}
    combo = walbook + [{"slug": r["slug"], "winner": r["winner"]}
                       for r in incident if r["slug"] not in graded]
    for name, rows in (("wallet", wallet), ("walbook", combo)):
        (WORK / f"truth-{name}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    for rule in ("range_avg", "hybrid"):
        write_params(slugs, rule, WORK / f"params-{rule}.json")
    print("\nrunning replays...")
    for name in ("wallet", "walbook"):
        for rule in ("range_avg", "hybrid"):
            run(rule, WORK / f"params-{rule}.json", WORK / f"truth-{name}.jsonl",
                WORK / "book-5m.jsonl", WORK / "eval-5m.jsonl",
                WORK / f"report-{name}-{rule}.jsonl")
