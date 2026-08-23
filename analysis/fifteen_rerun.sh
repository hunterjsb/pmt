#!/usr/bin/env bash
# The 15m re-opening matrix — one command.
#
#   btc/eth/sol 15m, every replayable window in the corpus, four legs:
#     base            binance + range_avg          (today's posture, real sizes)
#     rtds_range_avg  rtds + settle_tw 60 + range_avg
#     rtds_hybrid     rtds + settle_tw 60 + hybrid
#     rtds_terminal   rtds + settle_tw 60 + terminal
#   plus `asarmed` (the live observer arms, byte-for-byte) and a binance-only
#   `wide` leg over every graded 15m window the book tape holds.
#
# EVERYTHING except the leg's own feed / settle_rule / guard is held identical
# across legs — that is the only way a delta means anything
# (analysis/aggression_sweep.md). decided_k is NOT passed: `Tunables::law`
# bakes 1.25 in for any window longer than 300s, so every leg here already
# runs the 15m carve-out cap (analysis/carveout_ab.md).
#
# ~/.pmt is READ-ONLY. Every input is a frozen copy under $WORK and $HOME is
# shadowed, because `replay --mode full` appends missing 1m klines to
# $HOME/.pmt/corpus by design.
#
# Usage:
#   WORK=~/Desktop/code/pmt-wt-fifteen-work analysis/fifteen_rerun.sh
#   LEGS="base rtds_terminal" SETS="pair" analysis/fifteen_rerun.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${WORK:-$REPO/../pmt-wt-fifteen-work}"
AB="$WORK/ab"
BIN="${BIN:-$REPO/pmengine/target/release/pmengine}"
SHADOW="$WORK/home"
TAPE="$WORK/updown-tape-frozen.jsonl"
BOOK="$WORK/book-tape-frozen.jsonl"
OUTCOMES="${OUTCOMES:-$WORK/outcomes-merged.jsonl}"
RTDS="$SHADOW/.pmt/corpus/rtds"
CAP="${CAP:-500}"
LEGS="${LEGS:-base rtds_range_avg rtds_hybrid rtds_terminal}"
SETS="${SETS:-fleet pair btc eth sol}"
PY="uv run --project $REPO/pmtrader python"

[ -x "$BIN" ] || { echo "no pmengine binary at $BIN (cargo build --release --features ec2)"; exit 1; }
[ -f "$AB/meta.json" ] || { echo "run: $PY $REPO/analysis/fifteen_rerun.py survey --work $AB"; exit 1; }

run() {  # run <params-file> <out-file>
  local params="$1" out="$2"
  [ -f "$params" ] || { echo "  (no $params — skipped)"; return 0; }
  HOME="$SHADOW" "$BIN" replay \
    --mode full --slug "" \
    --tape "$TAPE" --book-tape "$BOOK" \
    --params "$params" --rtds-corpus "$RTDS" --outcomes "$OUTCOMES" \
    --fleet-cap "$CAP" --out "$out" \
    >"${out%.jsonl}.stdout" 2>"${out%.jsonl}.stderr"
  local skipped
  skipped=$(grep -c 'skipping\|refus' "${out%.jsonl}.stderr" || true)
  echo "  $(basename "$out")  $(wc -l <"$out") row(s), ${skipped} skip line(s)"
}

for g in $SETS; do
  echo "== $g =="
  for leg in $LEGS; do
    run "$AB/params-$g-$leg.json" "$AB/out-$g-$leg.jsonl"
  done
done

echo "== reproduction + context =="
run "$AB/params-fleet-asarmed.json" "$AB/out-fleet-asarmed.jsonl"
run "$AB/params-wide-base.json"     "$AB/out-wide-base.jsonl"
run "$AB/params-wide-terminal.json" "$AB/out-wide-terminal.jsonl"
run "$AB/params-census-rtds.json"   "$AB/out-census-rtds.jsonl"

echo "== report =="
$PY "$REPO/analysis/fifteen_rerun.py" report --work "$AB" --cap "$CAP"

# The gate-attribution ladder. A leg that fires zero clips needs a mechanism,
# so each ladder run relaxes exactly ONE gate on top of the leg's live params
# and the row that starts firing names the gate that was binding.
if [ "${LADDER:-1}" = "1" ]; then
  echo "== gate-attribution ladder =="
  rm -f "$AB"/params-ladder-*.json "$AB"/out-ladder-*.jsonl
  $PY "$REPO/analysis/fifteen_rerun.py" ladder --work "$AB"
  for f in "$AB"/params-ladder-*.json; do
    n=$(basename "$f" .json); n=${n#params-ladder-}
    run "$f" "$AB/out-ladder-$n.jsonl" >/dev/null
  done
  $PY "$REPO/analysis/fifteen_rerun.py" ladder-report --work "$AB"
fi

echo "== book depth by remaining time =="
$PY "$REPO/analysis/fifteen_rerun.py" depth --work "$AB" \
  --book-tape "$BOOK" --rtds-dir "$RTDS"
