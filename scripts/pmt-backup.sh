#!/usr/bin/env bash
#
# pmt corpus backup — the irreplaceable half of ~/.pmt, nightly, to S3.
#
# What is irreplaceable is the RECORDED half: the RTDS stream corpus and the
# klines/chainlink pulls every replay reads, plus the decision tapes the
# fixtures and the calibration work are built from. An hour of stream nobody
# recorded is an hour the replay harness can never judge again, and all of it
# sits on one disk in a box that powers off every night on purpose.
#
# What is deliberately NOT backed up: the rotated engine logs (*.log,
# *.log.gz). They are the bulk of the directory, and the tapes already carry
# structurally what the logs carry in prose — paying to ship them nightly
# would crowd out the data that actually can't be rebuilt.
#
# One object per day, skipped if today's is already up there, so re-running
# it (or a Persistent= timer catching up after a dark night) is free.
#
# Env overrides: PMT_HOME, PMT_BACKUP_S3, PMT_AWS, PMT_BACKUP_ZSTD_LEVEL,
# PMT_BACKUP_TMPDIR.

set -euo pipefail

PMT_HOME="${PMT_HOME:-$HOME/.pmt}"
S3_PREFIX="${PMT_BACKUP_S3:-s3://xanmc/pmt-backups}"
AWS="${PMT_AWS:-aws}"
ZSTD_LEVEL="${PMT_BACKUP_ZSTD_LEVEL:-10}"
# NOT /tmp: that is tmpfs here, and a 250MB corpus staged in RAM on a box
# that is about to power off is the wrong trade. /var/tmp is real disk.
STAGE_DIR="${PMT_BACKUP_TMPDIR:-/var/tmp}"

dry_run=0
force=0
date_tag="$(date +%F)"

usage() {
    cat <<'EOF'
usage: pmt-backup.sh [-n|--dry-run] [-f|--force] [--date YYYY-MM-DD]

  -n, --dry-run   list what would be archived and where, upload nothing
  -f, --force     upload even though today's object already exists
      --date D    archive under D's key instead of today's (backfill/tests)
EOF
}

die() { printf 'pmt-backup: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -n|--dry-run) dry_run=1 ;;
        -f|--force)   force=1 ;;
        --date)       shift; [ $# -gt 0 ] || die "--date needs YYYY-MM-DD"; date_tag="$1" ;;
        -h|--help)    usage; exit 0 ;;
        # exit 2, not 1: a mistyped flag is a usage error, and must never be
        # confused with a backup that ran and failed.
        *)            usage >&2
                      printf 'pmt-backup: unknown option: %s\n' "$1" >&2
                      exit 2 ;;
    esac
    shift
done

KEY="${S3_PREFIX}/${date_tag}.tar.zst"

# Relative-to-$PMT_HOME paths of everything the archive should carry, sorted
# so two runs of the same tree produce the same listing.
members() {
    {
        if [ -d "$PMT_HOME/corpus" ]; then
            # The RTDS recorder writes ONE file per day (rtds-YYYYMMDD.jsonl),
            # so restoring a single day's stream never means unpacking the
            # whole corpus. recorder.log/.pid are runtime noise, not data.
            find "$PMT_HOME/corpus" -type f \
                ! -name '*.log' ! -name '*.log.gz' ! -name '*.pid'
        fi
        if [ -d "$PMT_HOME/engine" ]; then
            # Tapes + the arm state that decides what comes back after a
            # restart. maxdepth 1: the engine writes no data subdirectories,
            # and a stray one is not something to ship blind.
            find "$PMT_HOME/engine" -maxdepth 1 -type f \
                \( -name '*.jsonl' -o -name 'arms-state.json' \)
        fi
    } | while IFS= read -r f; do
        printf '%s\n' "${f#"$PMT_HOME"/}"
    done | LC_ALL=C sort
}

# aws s3 ls matches on PREFIX, so confirm the exact object name came back.
# The listing is captured before it is matched: `aws | grep` under pipefail
# would report the miss-exit-1 aws returns for an absent key even when grep
# found the object, which is the silent way to re-upload every night.
object_exists() {
    local listing
    listing="$("$AWS" s3 ls "$KEY" 2>/dev/null || true)"
    printf '%s\n' "$listing" | grep -q "${date_tag}\.tar\.zst\$"
}

[ -d "$PMT_HOME" ] || die "no such directory: $PMT_HOME"

list_file="$(mktemp "${TMPDIR:-/tmp}/pmt-backup-list.XXXXXX")"
stage=""
cleanup() { rm -f "$list_file" ${stage:+"$stage"}; }
trap cleanup EXIT

members > "$list_file"
n_files="$(wc -l < "$list_file" | tr -d ' ')"
[ "$n_files" -gt 0 ] || die "nothing to archive under $PMT_HOME"

# Summed per file rather than `du -c`: xargs may split a long list into
# several invocations, and only a per-file sum survives that.
raw_bytes="$( (cd "$PMT_HOME" && xargs -d '\n' -r stat -c '%s' 2>/dev/null < "$list_file" \
    | awk '{ s += $1 } END { print s + 0 }') || echo 0 )"
raw_mib="$(awk -v b="${raw_bytes:-0}" 'BEGIN { printf "%.1f", b / 1048576 }')"

exists=0
if object_exists; then exists=1; fi

if [ "$dry_run" -eq 1 ]; then
    if [ "$exists" -eq 1 ]; then
        status="skip — ${date_tag}.tar.zst is already in S3"
    else
        status="would upload"
    fi
    printf 'pmt-backup — DRY RUN\n'
    printf '  source    %s\n' "$PMT_HOME"
    printf '  dest      %s\n' "$KEY"
    printf '  excludes  *.log  *.log.gz  *.pid\n'
    printf '  status    %s\n' "$status"
    printf '\nmembers (%s files, %s MiB uncompressed):\n' "$n_files" "$raw_mib"
    sed 's/^/  /' "$list_file"
    exit 0
fi

if [ "$exists" -eq 1 ] && [ "$force" -eq 0 ]; then
    printf 'pmt-backup: %s already in S3 — nothing to do (--force to overwrite)\n' \
        "${date_tag}.tar.zst"
    exit 0
fi

command -v zstd >/dev/null 2>&1 || die "zstd not found"

stage="$(mktemp "${STAGE_DIR}/pmt-backup-${date_tag}.XXXXXX.tar.zst")"

set +e
tar -C "$PMT_HOME" -c --files-from="$list_file" \
    | zstd -q -T0 "-${ZSTD_LEVEL}" > "$stage"
# The whole array in one read: PIPESTATUS is rebuilt by the next command,
# and that includes the assignment that reads the first element.
rc=("${PIPESTATUS[@]}")
set -e
tar_rc=${rc[0]}
zstd_rc=${rc[1]}

# tar exits 1 when a file grew underneath it — which is the NORMAL case here:
# the engine is appending to the tapes while we read them. A JSONL tail torn
# mid-line is exactly what tape.iter_records already skips, so the archive
# stays restorable. Only a real failure (2) is fatal.
[ "$tar_rc" -le 1 ] || die "tar failed (exit $tar_rc)"
[ "$zstd_rc" -eq 0 ] || die "zstd failed (exit $zstd_rc)"

out_bytes="$(wc -c < "$stage" | tr -d ' ')"
out_mib="$(awk -v b="$out_bytes" 'BEGIN { printf "%.1f", b / 1048576 }')"

"$AWS" s3 cp "$stage" "$KEY" --only-show-errors \
    || die "upload to $KEY failed"

printf 'pmt-backup: %s  (%s files, %s MiB -> %s MiB)\n' \
    "$KEY" "$n_files" "$raw_mib" "$out_mib"
