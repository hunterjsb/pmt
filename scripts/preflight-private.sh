#!/usr/bin/env bash
# Preflight for any pmt branch handoff and for the engine-restart checklist:
# the checkout must be the PRIVATE flavor and its gitlinked pmt-strategies
# sha must be reachable on the private remote (push-order invariant held) —
# otherwise a fresh clone/CI fetch of this branch 404s, or a rebuild silently
# produces the public engine and `run updown` dies at the next restart.
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
sub="$root/pmengine/src/strategies/private"

if [ ! -f "$sub/updown.rs" ]; then
    echo "FAIL: private strategies absent — submodule not initialized." >&2
    echo "run: git submodule update --init --checkout pmengine/src/strategies/private" >&2
    exit 1
fi

git -C "$sub" fetch -q origin
if ! git -C "$sub" merge-base --is-ancestor HEAD origin/main; then
    echo "FAIL: the mounted pmt-strategies commit $(git -C "$sub" rev-parse --short HEAD)" >&2
    echo "is NOT reachable on origin/main — push it first (scripts/strategies-push.sh)," >&2
    echo "or every fresh clone/CI fetch of the pmt commit recording it will 404." >&2
    exit 1
fi

echo "preflight OK: private flavor mounted, gitlinked sha reachable on pmt-strategies origin/main."
