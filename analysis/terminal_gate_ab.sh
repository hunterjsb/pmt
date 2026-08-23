#!/usr/bin/env bash
# The terminal-gate matrix — one command.
#
#   (a) late_terminal_agree  : stream fleet + the binance control
#   (b) settle_rule=terminal : 5m + 15m, theta 0.3 and a matched-selectivity sweep
#   (c) latch_release_on_proof
#
# EVERYTHING except the leg's own knob is held identical across legs — that is
# the only way a delta means anything (analysis/aggression_sweep.md).
#
# ~/.pmt is READ-ONLY. Every input is a frozen copy under $WORK and $HOME is
# shadowed, because `replay --mode full` appends missing 1m klines to
# $HOME/.pmt/corpus by design.
#
# Usage:
#   WORK=~/Desktop/code/pmt-wt-termgate-work analysis/terminal_gate_ab.sh
#   SETS="stream" analysis/terminal_gate_ab.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${WORK:-$REPO/../pmt-wt-termgate-work}"
AB="$WORK/ab"
BIN="${BIN:-$REPO/pmengine/target/release/pmengine}"
SHADOW="$WORK/home"
TAPE="$WORK/updown-tape-frozen.jsonl"
BOOK="$WORK/book-tape-frozen.jsonl"
OUTCOMES="${OUTCOMES:-$WORK/outcomes-extended.jsonl}"
RTDS="$SHADOW/.pmt/corpus/rtds"
CAP="${CAP:-500}"
SETS="${SETS:-stream binctl rule15 rule5}"

[ -x "$BIN" ] || { echo "no pmengine binary at $BIN"; exit 1; }
[ -f "$AB/meta.json" ] || { echo "run the survey first"; exit 1; }

# Which binary produced a given output. A study that mixes two engines'
# results in one table is not an A/B, and this exact mistake was made once
# already here (the matrix ran against a pre-change binary left in
# target/release by the fixture-baseline build). A cached output is reused
# ONLY when the binary that wrote it still hashes the same.
BINSUM="$(md5sum "$BIN" | cut -d' ' -f1)"
echo "engine $BIN  md5 $BINSUM"

run() {  # run <params-file> <out-file>
  local params="$1" out="$2" stamp="${2%.jsonl}.bin"
  [ -f "$params" ] || { echo "  (no $(basename "$params") — skipped)"; return 0; }
  if [ -s "$out" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$BINSUM" ]; then
    echo "  $(basename "$out")  (cached, $(wc -l <"$out") rows)"; return 0
  fi
  HOME="$SHADOW" "$BIN" replay \
    --mode full --slug "" \
    --tape "$TAPE" --book-tape "$BOOK" \
    --params "$params" --rtds-corpus "$RTDS" --outcomes "$OUTCOMES" \
    --fleet-cap "$CAP" --out "$out" ${TRACE:+--trace "${out%.jsonl}.trace.jsonl"} \
    >"${out%.jsonl}.stdout" 2>"${out%.jsonl}.stderr"
  echo "$BINSUM" >"$stamp"
  local skipped
  skipped=$(grep -c 'skipping\|refus' "${out%.jsonl}.stderr" || true)
  echo "  $(basename "$out")  $(wc -l <"$out") row(s), ${skipped} skip line(s)"
}

for g in $SETS; do
  echo "== $g =="
  for params in "$AB"/params-"$g"-*.json; do
    [ -f "$params" ] || continue
    leg="$(basename "$params" .json)"; leg="${leg#params-$g-}"
    run "$params" "$AB/out-$g-$leg.jsonl"
  done
done
