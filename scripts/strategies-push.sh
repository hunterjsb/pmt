#!/usr/bin/env bash
# The submodule commit must exist on GitHub BEFORE the superproject records
# its gitlink, or every fresh clone/CI fetch 404s. This script enforces that
# order: commit + push pmt-strategies FIRST, then stage the gitlink in pmt.
# Never rewrite history that has been pushed to pmt-strategies — a pmt commit
# pointing at a discarded sha wedges every private clone.
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
sub="$root/pmengine/src/strategies/private"
msg="${1:?usage: strategies-push.sh \"commit message\"}"

if [ ! -f "$sub/updown.rs" ]; then
    echo "error: pmt-strategies submodule not initialized at $sub" >&2
    echo "run: git submodule update --init --checkout pmengine/src/strategies/private" >&2
    exit 1
fi

git -C "$sub" add -A
git -C "$sub" commit -m "$msg"
git -C "$sub" push origin HEAD:main            # pmt-strategies push IS authorized
git -C "$root" add pmengine/src/strategies/private
echo "gitlink staged in pmt — commit locally; the OPERATOR pushes pm-trade/pmt."
