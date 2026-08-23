#!/usr/bin/env bash
# 15m stream-fed settle-rule A/B — one command.
#
#   settle_rule hybrid vs range_avg, feed=rtds, --mode full, 15m windows on
#   btc/eth/sol at the 15m-parked-era sizes (400/50, 300/40, 200/25), theta 0.3,
#   per-symbol guards 6/6/10bp. Everything except settle_rule is held identical
#   between the two runs — that is the only way the delta means anything
#   (analysis/aggression_sweep.md).
#
# PREFLIGHT IS A GATE, NOT A COURTESY. `--mode full` walks BOOK-TAPE windows,
# and a feed=rtds window also needs the RTDS corpus to span [start, end]. If the
# two tapes do not overlap there is no A/B to run, and this script stops and
# says so rather than producing a table of zeros that reads like a result.
#
# Usage:
#   analysis/fifteen_stream_ab.sh                    # default tapes
#   BOOK=... RTDS=... WORK=... analysis/fifteen_stream_ab.sh
#   FORCE=1 analysis/fifteen_stream_ab.sh            # run anyway (expect empty)
#   DUR=-5m- analysis/fifteen_stream_ab.sh           # wiring check on 5m, whose
#                                                    # two tapes DO overlap
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${BIN:-$REPO/pmengine/target/release/pmengine}"
BOOK="${BOOK:-$HOME/.pmt/engine/book-tape.jsonl}"
TAPE="${TAPE:-$HOME/.pmt/engine/updown-tape.jsonl}"
RTDS="${RTDS:-$HOME/.pmt/corpus/rtds}"
OUTCOMES="${OUTCOMES:-$HOME/.pmt/corpus/outcomes.jsonl}"
WORK="${WORK:-$HOME/.pmt/corpus/fifteen-ab}"
DUR="${DUR:--15m-}"
PY="uv run --project $REPO/pmtrader python"
AB="$REPO/analysis/fifteen_stream_ab.py"

mkdir -p "$WORK"

echo "== preflight =="
set +e
$PY "$AB" preflight --book "$BOOK" --rtds "$RTDS" --dur "$DUR"
rc=$?
set -e
if [ "$rc" -ne 0 ] && [ "${FORCE:-0}" != "1" ]; then
  echo
  echo "STOPPED: no ${DUR//-/} window has both a book tape and RTDS corpus coverage."
  echo "The decidedness study (analysis/fifteen_stream_fit.py) is what today's"
  echo "corpus can answer; this A/B needs 15m BOOKS recorded alongside the stream."
  echo "Re-run after a session where both tapes cover the same hours."
  exit "$rc"
fi

[ -x "$BIN" ] || { echo "no pmengine binary at $BIN (cargo build --release)"; exit 1; }

for rule in range_avg hybrid; do
  echo "== $rule =="
  $PY "$AB" params --rule "$rule" --out "$WORK/params-$rule.json" --book "$BOOK" --dur "$DUR"
  "$BIN" replay \
    --mode full \
    --slug "" \
    --tape "$TAPE" \
    --book-tape "$BOOK" \
    --params "$WORK/params-$rule.json" \
    --rtds-corpus "$RTDS" \
    --outcomes "$OUTCOMES" \
    --out "$WORK/full-$rule.jsonl" \
    >"$WORK/replay-$rule.stdout" 2>"$WORK/replay-$rule.stderr"
  # Skips are the interesting failure mode here, so surface them.
  grep -c 'skipping' "$WORK/replay-$rule.stderr" | xargs -I{} echo "  {} window(s) skipped (see $WORK/replay-$rule.stderr)"
done

echo "== report =="
$PY "$AB" report --a "$WORK/full-range_avg.jsonl" --b "$WORK/full-hybrid.jsonl" \
  --label-a range_avg --label-b hybrid
