#!/usr/bin/env bash
# Drive the feed A/B replay matrix. Params come from analysis/feed_ab.py.
#
#   analysis/feed_ab.sh <workdir>
#
# Every run is `pmengine replay --mode full ... --fleet-cap 500`. stdout is
# the JSONL report, stderr is kept beside it because the refusals ("skipping
# 'X': ...") only appear there and are part of the result.
set -uo pipefail
WORK="${1:?usage: feed_ab.sh <workdir>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HERE/../pmengine/target/release/pmengine"
OUTCOMES="${OUTCOMES:-$HOME/.pmt/corpus/outcomes.jsonl}"
CORPUS="${CORPUS:-$HOME/.pmt/corpus/rtds}"
BOOK_TAPE="${BOOK_TAPE:-$HOME/.pmt/engine/book-tape.jsonl}"
TAPE="${TAPE:-$HOME/.pmt/engine/updown-tape.jsonl}"
# Full mode APPENDS any missing 1m klines to $HOME/.pmt/corpus. Point HOME
# at a shadow tree holding a copy of the cache and the study cannot write
# to the live ~/.pmt at all; every other input is passed explicitly.
SHADOW_HOME="${SHADOW_HOME:-$HOME}"

run() { # run <name> <slug-query> <params>
  local name="$1" slug="$2" params="$3"
  [ -f "$params" ] || { echo "skip $name (no $params)"; return; }
  echo "== $name"
  HOME="$SHADOW_HOME" "$BIN" --log-level error replay --mode full --slug "$slug" \
    --params "$params" --outcomes "$OUTCOMES" --rtds-corpus "$CORPUS" \
    --book-tape "$BOOK_TAPE" --tape "$TAPE" \
    --fleet-cap 500 --out "$WORK/out-$name.jsonl" \
    >"$WORK/stdout-$name.txt" 2>"$WORK/stderr-$name.txt"
  echo "   rc=$? windows=$(grep -c aggregate "$WORK/out-$name.jsonl" 2>/dev/null) \
refusals=$(grep -c 'skipping' "$WORK/stderr-$name.txt" 2>/dev/null)"
}

for sym in btc eth sol bnb xrp; do
  for v in base rtds_liveguard rtds_streamguard rtds_floorguard; do
    run "$sym-$v" "$sym-updown-5m" "$WORK/params-$sym-$v.json"
  done
done

for v in base rtds_liveguard rtds_streamguard rtds_floorguard; do
  run "fleet-$v" "" "$WORK/params-fleet-$v.json"
done

# Refusal census — every graded 5m window, including the ones before the
# recorder existed. The binance leg is the control that shows the same set
# replays fine off klines.
run "census-rtds" "" "$WORK/params-fleet-allwindows_rtds.json"
run "census-base" "" "$WORK/params-fleet-allwindows_base.json"
