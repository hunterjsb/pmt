#!/usr/bin/env bash
# Resolution-farmer study: run every stage and dump one consolidated text report.
# Network-free except rf_postevent (cached separately) -- all stages read ~/.pmt/resfarm/.
set -u
cd "$(dirname "$0")/.."
OUT=${1:-$HOME/.pmt/resfarm/report.txt}
: > "$OUT"
{
  echo "############ rf_analyze (base rates + hazards) ############"
  python3 analysis/rf_analyze.py
  echo
  echo "############ rf_sim (portfolio economics) ############"
  python3 analysis/rf_sim.py
  echo
  echo "############ rf_ops (supply / hold / redemption lag) ############"
  python3 analysis/rf_ops.py
  echo
  echo "############ rf_filter_search (grid + date holdout) ############"
  python3 analysis/rf_filter_search.py
  echo
  echo "############ rf_stop (exit-rule variant) ############"
  python3 analysis/rf_stop.py
  echo
  echo "############ rf_noise_floor (is the best pocket even measurable) ############"
  python3 analysis/rf_noise_floor.py
  echo
  echo "############ rf_crypto_pocket (interrogate the one cell that survives) ############"
  python3 analysis/rf_crypto_pocket.py
} >> "$OUT" 2>&1
echo "wrote $OUT"
