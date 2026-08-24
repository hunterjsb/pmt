#!/usr/bin/env bash
#
# ceremony.sh — the merge/deploy ceremony for pmt, done by hand ~10 times in
# one night, automated. See CLAUDE.md "Private strategies" for the rule this
# script exists to enforce: the pmt-strategies submodule commit MUST be on
# GitHub before any pmt commit records its gitlink, or a fresh clone/CI fetch
# of that pmt commit 404s. Every subcommand below that touches the submodule
# pushes it FIRST, main SECOND — never the other way around.
#
# Subcommands:
#   ceremony.sh submodule <branch> -m "<msg>" [--dry-run]
#       Rebase <branch> (in the private submodule) onto origin/main if it
#       isn't already on top, push it to origin/main, point the main
#       checkout's submodule at that pushed sha, gitlink-commit it, run the
#       FULL private gate, push master.
#   ceremony.sh public <branch> -m "<msg>" [--dry-run]
#       git merge --no-ff <branch> into master, run rust + python gates,
#       push master.
#   ceremony.sh vault <branch> [-m "<msg>"] [--dry-run]
#       In pmt-alpha: git merge --no-ff <branch> into main (auto-resolves a
#       .gitignore-only conflict — the shape that hit us 5+ times tonight:
#       strip conflict markers, de-dupe non-empty lines), push, verify
#       against origin (a silent merge-push failure happened once).
#   ceremony.sh deploy [--dry-run]
#       Release build, preflight, sleep to the next 5-minute boundary + 12s,
#       restart the pmengine user service, verify it is up and answering.
#
# Every commit this script makes carries the trailers in TRAILERS_BLOCK
# below. -m is REQUIRED for submodule/public; optional for vault (falls back
# to git's default merge message, trailers still appended). --dry-run prints
# the plan and runs only read-only git queries (fetch, status, log,
# merge-base, diff --name-only) — no checkout, rebase, merge, commit, or
# push, ever. All paths are absolute; nothing here depends on the caller's
# cwd. Refuses to start against a checkout with tracked (not untracked)
# dirt, never force-pushes or force-resets, and never reads .env/keys.

set -euo pipefail

# ---------- fixed locations (never derived from cwd) ----------
PMT_ROOT="/var/home/hunter/Desktop/code/pmt"
PMENGINE_DIR="$PMT_ROOT/pmengine"
SUB_DIR="$PMENGINE_DIR/src/strategies/private"
FIXTURES_DIR="$SUB_DIR/fixtures"
PMTRADER_DIR="$PMT_ROOT/pmtrader"
VAULT_ROOT="/var/home/hunter/Desktop/code/pmt-alpha"
LOG_ROOT="$PMT_ROOT/logs/ceremony"

# fixtures/ holds one committed JSON fixture per characterized window plus a
# README; this is a fixed tripwire, not a computed count, on purpose — see
# CLAUDE.md "Characterization fixtures". Bump it deliberately (in the same
# commit that adds/removes a fixture), never to paper over a real mismatch.
FIXTURES_EXPECT=20

TRAILERS_BLOCK='Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01F5BK4KHdYX54D5AVHDos4J'

dry_run=0

usage() {
    cat <<'EOF'
usage:
  ceremony.sh submodule <branch> -m "<msg>" [--dry-run]
  ceremony.sh public    <branch> -m "<msg>" [--dry-run]
  ceremony.sh vault     <branch> [-m "<msg>"] [--dry-run]
  ceremony.sh deploy    [--dry-run]

  -m MSG       commit/merge message (required for submodule & public)
  --dry-run    print the plan, execute nothing mutating
  -h, --help   this text
EOF
}

die() { printf 'ceremony: %s\n' "$*" >&2; exit 2; }

# Loud failure: names the step, the repo, and the command that failed —
# never a bare git/cargo stack trace with no context about which of the
# four subcommands' many steps produced it.
abort() {
    local step="$1" repo="$2" cmd="$3" reason="$4"
    {
        printf 'CEREMONY ABORT\n'
        printf '  step:    %s\n' "$step"
        printf '  repo:    %s\n' "$repo"
        printf '  command: %s\n' "$cmd"
        printf '  reason:  %s\n' "$reason"
    } >&2
    exit 1
}

full_message() {  # full_message "<summary/body>" -> "<msg>\n\n<trailers>"
    printf '%s\n\n%s' "$1" "$TRAILERS_BLOCK"
}

# Refuse to start against a checkout with TRACKED dirt. Untracked files are
# always fine (pmt-alpha carries untracked sampler logs on a normal night).
# $2, if given, is a path allowed to show as tracked-dirty (the submodule
# gitlink line, if a previous run left it staged).
require_clean() {
    local dir="$1" allow="${2:-}" status tracked_dirt
    status="$(git -C "$dir" status --porcelain)"
    tracked_dirt="$(printf '%s\n' "$status" | grep -v '^?? ' || true)"
    if [ -n "$allow" ]; then
        tracked_dirt="$(printf '%s\n' "$tracked_dirt" | grep -v -F -- " $allow" || true)"
    fi
    tracked_dirt="$(printf '%s\n' "$tracked_dirt" | sed '/^$/d')"
    if [ -n "$tracked_dirt" ]; then
        abort "preflight: require clean checkout" "$dir" "git status --porcelain" \
"tracked dirt found (untracked files are fine, this is not):
$tracked_dirt"
    fi
}

branch_exists() {  # branch_exists <dir> <branch>
    git -C "$1" show-ref --verify --quiet "refs/heads/$2"
}

# Push <local_ref> to origin/<remote_branch> in <dir>, then verify the
# fetched remote ref actually landed at the sha we pushed before printing
# proof-of-landing — a silent merge/push failure happened once tonight, so
# this check is mandatory everywhere, not just for the vault.
push_and_verify() {
    local dir="$1" local_ref="$2" remote_branch="$3" local_sha remote_sha
    local_sha="$(git -C "$dir" rev-parse "$local_ref")"
    git -C "$dir" push origin "${local_ref}:${remote_branch}"
    git -C "$dir" fetch -q origin "$remote_branch"
    remote_sha="$(git -C "$dir" rev-parse "origin/${remote_branch}")"
    if [ "$local_sha" != "$remote_sha" ]; then
        abort "push verification" "$dir" "git push origin ${local_ref}:${remote_branch}" \
            "pushed $local_sha but origin/${remote_branch} now reads $remote_sha — the push did \
not appear to land as expected. Do not retry blindly; inspect the remote by hand."
    fi
    git -C "$dir" log --oneline "origin/${remote_branch}" -1
}

new_logdir() {  # new_logdir <label> -> prints the dir it created
    mkdir -p "$LOG_ROOT"
    mktemp -d "$LOG_ROOT/${1}-XXXXXX"
}

plan() {  # plan "<human description>" <argv...> — dry-run preview line only
    printf '[dry-run] would run: %s\n' "$1"
    shift
    if [ "$#" -gt 0 ]; then
        printf '           $ '
        printf '%q ' "$@"
        printf '\n'
    fi
}

# ---------- gates (each RC captured directly off its own command, never
# through a pipe — a `cmd | tee log` reports tee's exit status, not cmd's) ----------

run_rust_gate() {  # run_rust_gate <logdir>
    local logdir="$1" logf rc pass_n on_disk

    logf="$logdir/01-cargo-test.log"
    ( cd "$PMENGINE_DIR" && PMENGINE_EXPECT_PRIVATE=1 cargo test --features ec2 ) \
        >"$logf" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || abort "rust gate: cargo test" "$PMENGINE_DIR" \
        "PMENGINE_EXPECT_PRIVATE=1 cargo test --features ec2" \
        "exit $rc — tail of $logf:
$(tail -n 25 "$logf")"

    logf="$logdir/02-clippy.log"
    ( cd "$PMENGINE_DIR" && cargo clippy --features ec2 --all-targets -- -D warnings ) \
        >"$logf" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || abort "rust gate: clippy" "$PMENGINE_DIR" \
        "cargo clippy --features ec2 --all-targets -- -D warnings" \
        "exit $rc — tail of $logf:
$(tail -n 25 "$logf")"

    logf="$logdir/03-build-release.log"
    ( cd "$PMENGINE_DIR" && cargo build --release --features ec2 ) \
        >"$logf" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || abort "rust gate: release build" "$PMENGINE_DIR" \
        "cargo build --release --features ec2" \
        "exit $rc — tail of $logf:
$(tail -n 25 "$logf")"

    # Run FROM pmengine dir with a relative --fixtures path on purpose: a
    # relative-path invocation from elsewhere hit a 127 tonight.
    logf="$logdir/04-fixtures-replay.log"
    ( cd "$PMENGINE_DIR" && ./target/release/pmengine replay --fixtures src/strategies/private/fixtures ) \
        >"$logf" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || abort "rust gate: fixture replay" "$PMENGINE_DIR" \
        "./target/release/pmengine replay --fixtures src/strategies/private/fixtures (from $PMENGINE_DIR)" \
        "exit $rc — tail of $logf:
$(tail -n 40 "$logf")"

    pass_n="$(grep -c '^PASS ' "$logf" || true)"
    if [ "$pass_n" -ne "$FIXTURES_EXPECT" ]; then
        on_disk="$(find "$FIXTURES_DIR" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')"
        abort "rust gate: fixture PASS-count check" "$PMENGINE_DIR" \
            "grep -c '^PASS ' $logf" \
            "expected $FIXTURES_EXPECT PASS lines, got $pass_n ($on_disk *.json files on disk in \
$FIXTURES_DIR). The suite exited 0, so this is a COUNT mismatch, not a failing fixture — diff the \
PASS lines in $logf against the fixture filenames before touching anything. NEVER regenerate \
fixtures to make this pass."
    fi
}

run_python_gate() {  # run_python_gate <logdir>
    local logdir="$1" rc
    local logf="$logdir/05-pytest.log"
    ( cd "$PMTRADER_DIR" && uv run pytest tests/ -q ) >"$logf" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || abort "python gate: pytest" "$PMTRADER_DIR" \
        "uv run pytest tests/ -q" \
        "exit $rc — tail of $logf:
$(tail -n 25 "$logf")"
}

# ---------- vault's .gitignore auto-resolution ----------
#
# Every branch off pmt-alpha's main tends to append its own cache-dir line
# near the end of .gitignore, so two branches merging in the same night
# collide there and nowhere else. Applied by hand 5+ times tonight: strip
# the three marker line shapes, then drop duplicate NON-empty lines
# (blank lines pass through untouched) keeping first-seen order.
resolve_gitignore_conflict() {  # resolve_gitignore_conflict <file>
    local f="$1" tmp
    tmp="$(mktemp "${f}.XXXXXX")"
    awk '
        /^<<<<<<< / { next }
        /^\|\|\|\|\|\|\| / { next }
        /^=======$/ { next }
        /^>>>>>>> / { next }
        NF == 0 { print; next }
        !seen[$0]++ { print }
    ' "$f" >"$tmp"
    mv "$tmp" "$f"
}

# ================================================================
# submodule <branch>
# ================================================================
cmd_submodule() {
    local branch="$1" msg="$2"

    if [ "$dry_run" -eq 1 ]; then
        require_clean "$PMT_ROOT" "pmengine/src/strategies/private"
        require_clean "$SUB_DIR"
        git -C "$SUB_DIR" fetch -q origin
        branch_exists "$SUB_DIR" "$branch" || die "submodule: no local branch '$branch' in $SUB_DIR"

        printf 'ceremony submodule %s — DRY RUN\n' "$branch"
        printf '  submodule dir   %s\n' "$SUB_DIR"
        printf '  main checkout   %s\n' "$PMT_ROOT"
        if git -C "$SUB_DIR" merge-base --is-ancestor origin/main "$branch"; then
            printf '  rebase          not needed — %s is already on top of origin/main\n' "$branch"
        else
            printf '  rebase          needed — %s is behind origin/main:\n' "$branch"
            git -C "$SUB_DIR" log --oneline "${branch}..origin/main" | sed 's/^/    /'
        fi
        echo
        plan "checkout + rebase $branch onto origin/main (only if behind)" \
            git -C "$SUB_DIR" rebase origin/main
        plan "push the submodule branch to origin/main FIRST" \
            git -C "$SUB_DIR" push origin "${branch}:main"
        plan "fetch + checkout the pushed sha in the mounted submodule dir" \
            git -C "$SUB_DIR" checkout '<pushed-sha>'
        plan "stage the gitlink in the main checkout" \
            git -C "$PMT_ROOT" add pmengine/src/strategies/private
        plan "commit the gitlink" \
            git -C "$PMT_ROOT" commit -m "$(full_message "submodule gitlink: $msg")"
        plan "run the FULL private gate (cargo test, clippy, release build, fixture replay + \
PASS-count check; plus pytest IFF the rebased commits touch pmtrader-shaped paths — \
undeterminable without a real rebase, so not run here)"
        plan "push master" \
            git -C "$PMT_ROOT" push origin master:master
        printf '\n(no mutating command above was executed)\n'
        return 0
    fi

    require_clean "$PMT_ROOT" "pmengine/src/strategies/private"
    require_clean "$SUB_DIR"

    local old_sha
    old_sha="$(git -C "$SUB_DIR" rev-parse HEAD)"

    git -C "$SUB_DIR" fetch origin
    branch_exists "$SUB_DIR" "$branch" || abort "submodule: branch check" "$SUB_DIR" \
        "git show-ref --verify refs/heads/$branch" "no local branch named '$branch'"

    local new_sha
    if git -C "$SUB_DIR" merge-base --is-ancestor origin/main "$branch"; then
        new_sha="$(git -C "$SUB_DIR" rev-parse "$branch")"
    else
        git -C "$SUB_DIR" checkout "$branch"
        if ! git -C "$SUB_DIR" rebase origin/main; then
            local conflicts
            conflicts="$(git -C "$SUB_DIR" diff --name-only --diff-filter=U || true)"
            abort "submodule: rebase onto origin/main" "$SUB_DIR" "git rebase origin/main" \
"rebase stopped with conflicts in: ${conflicts:-<unknown>}
Resolve by hand in $SUB_DIR (git rebase --continue, or --abort), then re-run:
  ceremony.sh submodule $branch -m \"$msg\""
        fi
        new_sha="$(git -C "$SUB_DIR" rev-parse "$branch")"
    fi

    # push-order invariant: submodule sha on GitHub BEFORE any pmt commit
    # records it in the gitlink.
    push_and_verify "$SUB_DIR" "$new_sha" "main"

    git -C "$SUB_DIR" fetch -q origin
    git -C "$SUB_DIR" checkout "$new_sha"

    git -C "$PMT_ROOT" add pmengine/src/strategies/private

    local run_pytest=0 changed
    changed="$(git -C "$SUB_DIR" diff --name-only "$old_sha" "$new_sha" 2>/dev/null || true)"
    if printf '%s\n' "$changed" | grep -Eq '(^|/)pmtrader(/|$)|\.py$'; then
        run_pytest=1
    fi

    if ! git -C "$PMT_ROOT" commit -m "$(full_message "submodule gitlink: $msg")"; then
        abort "submodule: gitlink commit" "$PMT_ROOT" "git commit -m ..." \
            "commit failed — see git's own output above"
    fi

    local logdir
    logdir="$(new_logdir "submodule-${branch}")"
    run_rust_gate "$logdir"
    if [ "$run_pytest" -eq 1 ]; then
        run_python_gate "$logdir"
    fi

    push_and_verify "$PMT_ROOT" master master
}

# ================================================================
# public <branch>
# ================================================================
cmd_public() {
    local branch="$1" msg="$2"

    if [ "$dry_run" -eq 1 ]; then
        require_clean "$PMT_ROOT"
        git -C "$PMT_ROOT" fetch -q origin
        branch_exists "$PMT_ROOT" "$branch" || die "public: no local branch '$branch' in $PMT_ROOT"

        printf 'ceremony public %s — DRY RUN\n' "$branch"
        printf '  repo   %s\n' "$PMT_ROOT"
        printf '  ahead  %s commit(s) not yet on master:\n' \
            "$(git -C "$PMT_ROOT" log --oneline "master..$branch" | wc -l | tr -d ' ')"
        git -C "$PMT_ROOT" log --oneline "master..$branch" | sed 's/^/    /'
        echo
        plan "checkout master" git -C "$PMT_ROOT" checkout master
        plan "merge --no-ff $branch into master" \
            git -C "$PMT_ROOT" merge --no-ff -m "$(full_message "$msg")" "$branch"
        plan "run rust gate (cargo test, clippy, release build, fixture replay)"
        plan "run python gate (uv run pytest tests/ -q)"
        plan "push master" \
            git -C "$PMT_ROOT" push origin master:master
        printf '\n(no mutating command above was executed)\n'
        return 0
    fi

    require_clean "$PMT_ROOT"
    git -C "$PMT_ROOT" fetch origin
    branch_exists "$PMT_ROOT" "$branch" || abort "public: branch check" "$PMT_ROOT" \
        "git show-ref --verify refs/heads/$branch" "no local branch named '$branch'"

    git -C "$PMT_ROOT" checkout master

    local full_msg
    full_msg="$(full_message "$msg")"
    if ! git -C "$PMT_ROOT" merge --no-ff -m "$full_msg" "$branch"; then
        local conflicts
        conflicts="$(git -C "$PMT_ROOT" diff --name-only --diff-filter=U || true)"
        git -C "$PMT_ROOT" merge --abort || true
        abort "public: merge conflict" "$PMT_ROOT" "git merge --no-ff $branch" \
"conflicts in: ${conflicts:-<unknown>} — public has no auto-resolution (that's vault's \
.gitignore-only special case). Merge aborted, master left clean. Resolve by hand, then re-run."
    fi

    local logdir
    logdir="$(new_logdir "public-${branch}")"
    run_rust_gate "$logdir"
    run_python_gate "$logdir"

    push_and_verify "$PMT_ROOT" master master
}

# ================================================================
# vault <branch>
# ================================================================
cmd_vault() {
    local branch="$1" msg="$2"

    if [ "$dry_run" -eq 1 ]; then
        require_clean "$VAULT_ROOT"
        git -C "$VAULT_ROOT" fetch -q origin
        branch_exists "$VAULT_ROOT" "$branch" || die "vault: no local branch '$branch' in $VAULT_ROOT"

        printf 'ceremony vault %s — DRY RUN\n' "$branch"
        printf '  repo   %s\n' "$VAULT_ROOT"
        printf '  ahead  %s commit(s) not yet on main:\n' \
            "$(git -C "$VAULT_ROOT" log --oneline "main..$branch" | wc -l | tr -d ' ')"
        git -C "$VAULT_ROOT" log --oneline "main..$branch" | sed 's/^/    /'
        echo
        plan "checkout main" git -C "$VAULT_ROOT" checkout main
        if [ -n "$msg" ]; then
            plan "merge --no-ff $branch into main" \
                git -C "$VAULT_ROOT" merge --no-ff -m "$msg" "$branch"
        else
            plan "merge --no-ff $branch into main (default merge message)" \
                git -C "$VAULT_ROOT" merge --no-ff --no-edit "$branch"
        fi
        plan "if .gitignore is the ONLY conflicted file: strip markers, de-dupe non-empty \
lines, git add, commit. Any other conflict aborts the merge for by-hand resolution."
        plan "amend the merge commit to append the standard trailers"
        plan "push main, then verify origin/main == local HEAD (mandatory — a silent push \
failure happened once)" \
            git -C "$VAULT_ROOT" push origin main:main
        printf '\n(no mutating command above was executed)\n'
        return 0
    fi

    require_clean "$VAULT_ROOT"
    git -C "$VAULT_ROOT" fetch origin
    branch_exists "$VAULT_ROOT" "$branch" || abort "vault: branch check" "$VAULT_ROOT" \
        "git show-ref --verify refs/heads/$branch" "no local branch named '$branch'"

    git -C "$VAULT_ROOT" checkout main

    local merge_rc=0
    if [ -n "$msg" ]; then
        git -C "$VAULT_ROOT" merge --no-ff -m "$msg" "$branch" || merge_rc=$?
    else
        git -C "$VAULT_ROOT" merge --no-ff --no-edit "$branch" || merge_rc=$?
    fi

    if [ "$merge_rc" -ne 0 ]; then
        local conflicts
        conflicts="$(git -C "$VAULT_ROOT" diff --name-only --diff-filter=U || true)"
        if [ "$conflicts" = ".gitignore" ]; then
            resolve_gitignore_conflict "$VAULT_ROOT/.gitignore"
            git -C "$VAULT_ROOT" add .gitignore
            local remaining
            remaining="$(git -C "$VAULT_ROOT" diff --name-only --diff-filter=U || true)"
            if [ -n "$remaining" ]; then
                git -C "$VAULT_ROOT" merge --abort
                abort "vault: merge conflict" "$VAULT_ROOT" "git merge --no-ff $branch" \
"conflicts remained after the .gitignore auto-resolve: $remaining. Merge aborted, main left \
clean. Resolve by hand, then re-run."
            fi
            if ! git -C "$VAULT_ROOT" commit --no-edit; then
                abort "vault: merge commit (post .gitignore resolve)" "$VAULT_ROOT" \
                    "git commit --no-edit" "commit failed — see git's own output above"
            fi
        else
            git -C "$VAULT_ROOT" merge --abort || true
            abort "vault: merge conflict" "$VAULT_ROOT" "git merge --no-ff $branch" \
"conflicts in: ${conflicts:-<unknown>} — auto-resolution only covers a .gitignore-ONLY \
conflict. Merge aborted, main left clean. Resolve by hand, then re-run."
        fi
    fi

    local orig_msg amended_msg
    orig_msg="$(git -C "$VAULT_ROOT" log -1 --format=%B)"
    amended_msg="$(full_message "$orig_msg")"
    if ! git -C "$VAULT_ROOT" commit --amend -m "$amended_msg"; then
        abort "vault: append trailers" "$VAULT_ROOT" "git commit --amend -m ..." \
            "amend failed — see git's own output above"
    fi

    # Mandatory: a silent merge/push failure happened once tonight.
    push_and_verify "$VAULT_ROOT" main main
}

# ================================================================
# deploy
# ================================================================
cmd_deploy() {
    local now_epoch boundary target_epoch wait_s target_human

    now_epoch="$(date +%s)"
    boundary=$(( (now_epoch / 300 + 1) * 300 ))
    target_epoch=$(( boundary + 12 ))
    wait_s=$(( target_epoch - now_epoch ))
    target_human="$(date -u -d "@$target_epoch" +%Y-%m-%dT%H:%M:%SZ)"

    if [ "$dry_run" -eq 1 ]; then
        printf 'ceremony deploy — DRY RUN\n'
        plan "cargo build --release --features ec2 (from $PMENGINE_DIR)"
        printf '  running preflight-private.sh for real — it is read-only:\n'
        if bash "$PMT_ROOT/scripts/preflight-private.sh"; then
            printf '  preflight: PASS\n'
        else
            printf '  preflight: FAIL (see output above) — deploy would abort here\n'
        fi
        printf '  next 5-minute boundary + 12s is %s (in %ds)\n' "$target_human" "$wait_s"
        plan "sleep ${wait_s}s until $target_human"
        plan "systemctl --user restart pmengine"
        plan "poll systemctl --user is-active pmengine until 'active' (15s budget)"
        plan "poll curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7531/tape until 200 (15s budget)"
        printf '\n(no mutating command above was executed)\n'
        return 0
    fi

    if ! ( cd "$PMENGINE_DIR" && cargo build --release --features ec2 ); then
        abort "deploy: release build" "$PMENGINE_DIR" "cargo build --release --features ec2" \
            "build failed — see cargo output above"
    fi

    if ! bash "$PMT_ROOT/scripts/preflight-private.sh"; then
        abort "deploy: preflight" "$PMT_ROOT" "bash scripts/preflight-private.sh" \
            "preflight failed — see its own output above"
    fi

    printf 'deploy: sleeping %ds until %s (next 5-minute boundary + 12s)\n' "$wait_s" "$target_human"
    sleep "$wait_s"

    local restart_ts
    restart_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if ! systemctl --user restart pmengine; then
        abort "deploy: restart" "systemctl --user" "systemctl --user restart pmengine" \
            "restart command itself failed"
    fi

    local attempt active ok
    ok=0
    active="<never checked>"
    for attempt in $(seq 1 15); do
        : "$attempt"  # bounded-retry counter only; value itself is unused
        if active="$(systemctl --user is-active pmengine 2>&1)"; then
            if [ "$active" = "active" ]; then
                ok=1
                break
            fi
        fi
        sleep 1
    done
    if [ "$ok" -ne 1 ]; then
        abort "deploy: is-active check" "systemctl --user" "systemctl --user is-active pmengine" \
            "service did not reach 'active' within 15s of the restart (last state: $active)"
    fi

    local http_code
    ok=0
    http_code="<no response>"
    for attempt in $(seq 1 15); do
        : "$attempt"  # bounded-retry counter only; value itself is unused
        http_code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7531/tape || true)"
        if [ "$http_code" = "200" ]; then
            ok=1
            break
        fi
        sleep 1
    done
    if [ "$ok" -ne 1 ]; then
        abort "deploy: /tape health check" "http://127.0.0.1:7531" \
            "curl -s http://127.0.0.1:7531/tape" \
            "expected HTTP 200 within 15s, last got '$http_code'"
    fi

    printf 'deploy OK — pmengine restarted at %s, is-active=active, GET /tape=200\n' "$restart_ts"
}

# ================================================================
# arg parsing / dispatch
# ================================================================
[ "$#" -ge 1 ] || { usage >&2; exit 2; }

subcmd="$1"; shift

case "$subcmd" in
    -h|--help) usage; exit 0 ;;
esac

case "$subcmd" in
    submodule|public|vault)
        [ "$#" -ge 1 ] || { usage >&2; die "$subcmd needs a <branch> argument"; }
        branch="$1"; shift
        msg=""
        while [ "$#" -gt 0 ]; do
            case "$1" in
                -m)
                    shift
                    [ "$#" -gt 0 ] || die "-m needs a message"
                    msg="$1"
                    ;;
                --dry-run) dry_run=1 ;;
                -h|--help) usage; exit 0 ;;
                *) usage >&2; die "unknown option: $1" ;;
            esac
            shift
        done
        if [ "$subcmd" != "vault" ] && [ -z "$msg" ]; then
            die "$subcmd requires -m \"<message>\""
        fi
        ;;
    deploy)
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --dry-run) dry_run=1 ;;
                -h|--help) usage; exit 0 ;;
                *) usage >&2; die "unknown option: $1" ;;
            esac
            shift
        done
        ;;
    *)
        usage >&2
        die "unknown subcommand: $subcmd"
        ;;
esac

case "$subcmd" in
    submodule) cmd_submodule "$branch" "$msg" ;;
    public)    cmd_public "$branch" "$msg" ;;
    vault)     cmd_vault "$branch" "$msg" ;;
    deploy)    cmd_deploy ;;
esac
