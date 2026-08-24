#!/usr/bin/env bash
# ship-eu.sh — build the PRIVATE pmengine flavor for aarch64 and install it on
# the eu-west-1 box. Re-runnable: every step is idempotent, and the script
# never enables, starts, or funds anything.
#
# Why cross-compile at all: the target is a t4g.micro with 1GB of RAM. rustc
# OOMs there. The binary is therefore always built here and shipped in.
#
# Why a presigned URL instead of `aws s3 cp` on the box: the instance role
# (pmt-eu-ssm) is AmazonSSMManagedInstanceCore ONLY — it has no S3 rights, and
# handing a trading host standing bucket access to buy one download is a worse
# trade than a URL that dies in 15 minutes.
#
#   ./deploy/eu/ship-eu.sh              # build + upload + install + smoke
#   ./deploy/eu/ship-eu.sh --skip-build # reuse the existing target-eu/ binary
#   ./deploy/eu/ship-eu.sh --restart    # ...then restart at the next 5m boundary
#
set -euo pipefail

INSTANCE="${PMT_EU_INSTANCE:-i-0426f1d5e68cdee60}"   # pmt-alpha/infra/ec2-eu-runbook.md
REGION="${PMT_EU_REGION:-eu-west-1}"
BUCKET="${PMT_DEPLOY_BUCKET:-xanmc}"
PREFIX="${PMT_DEPLOY_PREFIX:-pmt-deploy}"
BUCKET_REGION="${PMT_DEPLOY_BUCKET_REGION:-us-east-1}"
TARGET="aarch64-unknown-linux-gnu"
REMOTE_BIN="/home/ec2-user/pmt/bin/pmengine"

# Amazon Linux 2023 ships glibc 2.34. cross's `main` image is a recent Ubuntu
# and links against 2.38/2.39, which produces a binary that builds cleanly here
# and dies on the box with "GLIBC_2.39 not found". glibc symbol versioning is
# backward- but NOT forward-compatible, so the build image's glibc must be <=
# the box's. The 0.2.5 tag is Ubuntu 20.04 / glibc 2.31.
CROSS_IMAGE="${CROSS_IMAGE:-ghcr.io/cross-rs/aarch64-unknown-linux-gnu:0.2.5}"
BOX_GLIBC="${BOX_GLIBC:-2.34}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENGINE="$REPO_ROOT/pmengine"
SKIP_BUILD=0
RESTART=0
for a in "$@"; do
  case "$a" in
    --skip-build) SKIP_BUILD=1 ;;
    --restart)    RESTART=1 ;;
    *) echo "unknown arg: $a" >&2; exit 1 ;;
  esac
done

say() { printf '\n=== %s\n' "$*"; }
die() { printf '\nFATAL: %s\n' "$*" >&2; exit 1; }

# --- 1. flavor preflight ----------------------------------------------------
# build.rs probes this exact FILE, not the directory: an uninitialized
# submodule leaves an empty dir behind and would silently build the public,
# strategy-less engine. Catch that here rather than on the box.
say "preflight: private strategies submodule"
PROBE="$ENGINE/src/strategies/private/updown.rs"
[ -f "$PROBE" ] || die "submodule not mounted ($PROBE missing).
  Worktrees never populate submodules automatically. Run:
    git -C $REPO_ROOT submodule update --init --checkout pmengine/src/strategies/private"
echo "ok: $(git -C "$ENGINE/src/strategies/private" rev-parse --short HEAD) mounted"

SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
DIRTY=""
git -C "$REPO_ROOT" diff --quiet HEAD -- pmengine || DIRTY="-dirty"
ARTIFACT="pmengine-aarch64-${SHA}${DIRTY}.tar.gz"
KEY="$PREFIX/$ARTIFACT"

# Cross artifacts are isolated from the native target/ (see incident note
# below); defined here so --skip-build resolves the binary path too.
CROSS_TARGET_DIR="$ENGINE/target-eu"

# --- 2. cross-compile -------------------------------------------------------
if [ "$SKIP_BUILD" -eq 0 ]; then
  say "cross build --release --target $TARGET --features ec2"
  command -v cross >/dev/null || die "cross absent: cargo install cross --git https://github.com/cross-rs/cross"
  LOG="$(mktemp)"

  # Cross builds live in their OWN target dir. The 2026-08-23 incident: the
  # image-change wipe below deleted target/release/pmengine — the binary the
  # live systemd unit execs — leaving the fleet one restart from dark. An
  # isolated CARGO_TARGET_DIR makes that impossible: the native binary and
  # the cross cache can never share a directory again.
  export CARGO_TARGET_DIR="$CROSS_TARGET_DIR"

  # Build scripts are compiled for the HOST and cached in the cross target
  # dir. Swap the container image for one with an older glibc and cargo
  # happily reuses those host binaries — which the new container then cannot
  # exec ("GLIBC_2.39 not found" from a *build script*, not from our binary).
  # Stamp the image and wipe the CROSS cache (never the native one) on change.
  STAMP="$CROSS_TARGET_DIR/.cross-image"
  if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$CROSS_IMAGE" ]; then
    say "cross image changed → clearing stale cross build artifacts"
    rm -rf "$CROSS_TARGET_DIR/release" "$CROSS_TARGET_DIR/$TARGET"
    mkdir -p "$CROSS_TARGET_DIR"
    printf '%s' "$CROSS_IMAGE" > "$STAMP"
  fi

  (
    cd "$ENGINE"
    # AWS_LC_SYS_CMAKE_BUILDER dodges a GCC memcmp bug in aws-lc-sys; it has to
    # cross the container boundary too, hence CROSS_CONTAINER_OPTS. Mirrors
    # .github/workflows/publish-pmengine.yml.
    CROSS_TARGET_AARCH64_UNKNOWN_LINUX_GNU_IMAGE="$CROSS_IMAGE" \
    CROSS_CONTAINER_ENGINE="${CROSS_CONTAINER_ENGINE:-podman}" \
    CROSS_CONTAINER_OPTS="-e AWS_LC_SYS_CMAKE_BUILDER=1" \
    AWS_LC_SYS_CMAKE_BUILDER=1 \
      cross build --release --target "$TARGET" --features ec2 2>&1 | tee "$LOG"
  ) || die "build failed"

  # build.rs shouts when the private strategies are missing. If that warning is
  # in the log we just built the public engine and must not ship it.
  if grep -q "private strategies absent" "$LOG"; then
    die "build produced the PUBLIC flavor (build.rs warned 'private strategies absent'). Fix the submodule and rebuild."
  fi
  echo "ok: no 'private strategies absent' warning — private flavor"
  rm -f "$LOG"
fi

BIN="$CROSS_TARGET_DIR/$TARGET/release/pmengine"
[ -x "$BIN" ] || die "no binary at $BIN"
file "$BIN" | grep -q "ARM aarch64" || die "binary is not aarch64: $(file "$BIN")"

# Catch the glibc-too-new failure HERE. Otherwise it surfaces as a runtime
# "GLIBC_2.39 not found" after a full build-upload-install round trip.
NEED="$(objdump -T "$BIN" 2>/dev/null | grep -o 'GLIBC_[0-9]\+\.[0-9]\+' | sort -uV | tail -1 | cut -d_ -f2)"
if [ -n "$NEED" ]; then
  if [ "$(printf '%s\n%s\n' "$NEED" "$BOX_GLIBC" | sort -V | tail -1)" != "$BOX_GLIBC" ]; then
    die "binary needs glibc $NEED but the box has $BOX_GLIBC.
  The build image's glibc must be <= the box's. Pin an older one:
    CROSS_IMAGE=ghcr.io/cross-rs/$TARGET:0.2.5 $0"
  fi
  echo "ok: needs glibc <= $NEED, box has $BOX_GLIBC"
fi
say "built $(file "$BIN" | cut -d, -f1-3)"

# --- 3. upload --------------------------------------------------------------
say "upload s3://$BUCKET/$KEY"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
# Tar from $BIN's own directory. The 2026-08-24 incident: this line still read
# the pre-isolation target/ path, so a stale binary shipped under a fresh SHA's
# artifact name and smoke-tested green (smoke proves flavor, not version).
tar -czf "$STAGE/$ARTIFACT" -C "$(dirname "$BIN")" pmengine
LOCAL_SHA="$(sha256sum "$BIN" | cut -d' ' -f1)"
echo "local binary sha256: $LOCAL_SHA"
aws s3 cp "$STAGE/$ARTIFACT" "s3://$BUCKET/$KEY" --region "$BUCKET_REGION"

URL="$(aws s3 presign "s3://$BUCKET/$KEY" --region "$BUCKET_REGION" --expires-in 900)"

# --- 4. install on the box via SSM ------------------------------------------
# The payload is base64'd into the parameter JSON. Inline quoting through
# `--parameters commands=[...]` mangles anything with a quote or a $ in it.
ssm_run() {
  local script="$1" timeout="${2:-300}" work cmd_id status
  work="$(mktemp -d)"
  # base64 portably: BSD (macOS) takes no -w/positional file, GNU wraps at 76
  # cols by default — stdin + tr covers both.
  python3 - "$(base64 < "$script" | tr -d '\n')" "$work/params.json" <<'PY'
import json, sys
b64, out = sys.argv[1], sys.argv[2]
json.dump({"commands": [
    "set -euo pipefail",
    f"echo {b64} | base64 -d > /tmp/ssm-payload.sh",
    "bash /tmp/ssm-payload.sh",
    "rm -f /tmp/ssm-payload.sh",
]}, open(out, "w"))
PY
  cmd_id="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE" \
    --document-name AWS-RunShellScript --parameters "file://$work/params.json" \
    --timeout-seconds "$timeout" --query "Command.CommandId" --output text)"
  # Poll for the payload's whole timeout, not a fixed 5 minutes — the boundary
  # restart legitimately sleeps up to ~5.5 minutes before it even starts.
  for _ in $(seq 1 $((timeout / 5 + 12))); do
    sleep 5
    status="$(aws ssm get-command-invocation --region "$REGION" --command-id "$cmd_id" \
      --instance-id "$INSTANCE" --query Status --output text 2>/dev/null || echo Pending)"
    case "$status" in Success|Failed|Cancelled|TimedOut) break ;; esac
  done
  aws ssm get-command-invocation --region "$REGION" --command-id "$cmd_id" \
    --instance-id "$INSTANCE" --query StandardOutputContent --output text
  aws ssm get-command-invocation --region "$REGION" --command-id "$cmd_id" \
    --instance-id "$INSTANCE" --query StandardErrorContent --output text >&2
  rm -rf "$work"
  [ "$status" = Success ]
}

say "install on $INSTANCE"
INSTALL="$STAGE/install.sh"
cat > "$INSTALL" <<EOSH
set -euo pipefail
install -d -o ec2-user -g ec2-user -m 0755 /home/ec2-user/pmt/bin
install -d -o ec2-user -g ec2-user -m 0700 /home/ec2-user/.pmt/engine
cd "\$(mktemp -d)"
curl -fsSL -o pmengine.tar.gz '$URL'
tar -xzf pmengine.tar.gz
install -o ec2-user -g ec2-user -m 0755 pmengine '$REMOTE_BIN'
echo "installed:"; ls -l '$REMOTE_BIN'
# The version proof: what landed is bit-for-bit what was built.
echo '$LOCAL_SHA  $REMOTE_BIN' | sha256sum -c -
EOSH
ssm_run "$INSTALL" 300 || die "install failed"

# --- 5. smoke: the flavor proof --------------------------------------------
# `list` starts no strategy and places no order. If it prints updown, the
# aarch64 build runs AND the private strategies made it aboard. If it prints
# only `example`, we shipped the public engine.
say "smoke: $REMOTE_BIN list"
SMOKE="$STAGE/smoke.sh"
cat > "$SMOKE" <<EOSH
set -euo pipefail
sudo -u ec2-user '$REMOTE_BIN' list
EOSH
OUT="$(ssm_run "$SMOKE" 120)" || die "smoke failed"
echo "$OUT"
echo "$OUT" | grep -qw updown || die "PUBLIC FLAVOR ON BOX — 'list' has no updown. Do not proceed."
say "OK — updown present. Private flavor confirmed on aarch64."

# --- 6. optional boundary restart -------------------------------------------
# A restart mid-window would yank management out from under a live position;
# every 5m rollover leaves the box flat, so activation waits for one.
if [ "$RESTART" -eq 1 ]; then
  say "boundary restart: next 5m rollover +15s"
  RESTART_SH="$STAGE/restart.sh"
  cat > "$RESTART_SH" <<'EOSH'
set -euo pipefail
now=$(date +%s); wait=$(( (300 - now % 300) % 300 + 15 ))
echo "restarting in ${wait}s (boundary +15s)"; sleep "$wait"
systemctl restart pmengine
sleep 6
systemctl is-active pmengine
tail -5 /home/ec2-user/.pmt/engine/engine-systemd.log
EOSH
  ssm_run "$RESTART_SH" 600 || die "boundary restart failed"
  say "OK — engine restarted on the shipped binary at a window boundary"
else
  cat <<'EOF'

Installed and smoke-tested. NOT restarted — the running engine still execs the
old binary. Re-run with --restart, or follow deploy/eu/README.md.
EOF
fi
