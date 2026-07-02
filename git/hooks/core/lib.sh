#!/bin/sh

# Core git-hooks library: shared state and utilities sourced by every hook
# script. The engine that drives the runner (the enable hierarchy and the
# execution layers) lives beside this file in dispatch.sh.

# --- Configuration ---
#
# Defined ahead of the state block below, which resolves the global kill switch
# through cfg_bool.

# Helper to check boolean values.
is_truthy() {
    case "$1" in
        [Yy][Ee][Ss]|[Yy]|[Tt][Rr][Uu][Ee]|1|[Oo][Nn]) return 0 ;;
        *) return 1 ;;
    esac
}

# The environment-variable twin of a config key is a pure mechanical transform:
# upper-case the whole key and turn every `-` and `.` into `_`. So
# `wits.hooks.pre-commit.formatter-disable` maps to
# WITS_HOOKS_PRE_COMMIT_FORMATTER_DISABLE — no prefix juggling, no special case.
_cfg_env_name() {
    echo "$1" | tr '[:lower:]' '[:upper:]' | tr '.-' '__'
}

# Resolve a setting, environment twin first, then git config. This is the single
# path every script uses, so one rule holds everywhere: env overrides config,
# config is the standing value.
#
#   cfg_bool  <config-key> [default]   -> exit status (0 = true), for --bool keys
#   cfg_value <config-key> [default]   -> echoes the resolved string
cfg_bool() {
    _env=$(_cfg_env_name "$1")
    eval _val="\${$_env:-}"
    [ -n "$_val" ] && { is_truthy "$_val"; return; }
    _val=$(git config --bool "$1" 2>/dev/null)
    [ -n "$_val" ] && { is_truthy "$_val"; return; }
    is_truthy "${2:-false}"
}
cfg_value() {
    _env=$(_cfg_env_name "$1")
    eval _val="\${$_env:-}"
    [ -n "$_val" ] && { printf '%s\n' "$_val"; return; }
    _val=$(git config "$1" 2>/dev/null)
    [ -n "$_val" ] && { printf '%s\n' "$_val"; return; }
    printf '%s\n' "${2:-}"
}

# Repo-scoped state, resolved exactly once per hook run.
#
# The top-level runner sources this file, then execs each hook script as its own
# process. Those children re-source this file only to obtain the shell functions
# defined here (functions cannot be exported). The expensive git queries here are
# guarded and their results exported, so a child inherits them from the
# environment instead of re-forking git on every script — this matters most for
# hot hooks such as reference-transaction.
#
# Caveat: the guard is keyed on the process environment, so a hook script that
# recursively invokes another repository's hooks (e.g. across a submodule) would
# inherit the outer repo's values. That already holds for the exported GIT_DIR,
# and such nested invocations do not occur in this framework.
if [ -z "${_WITS_ENV_LOADED:-}" ]; then
    GIT_DIR=$(git rev-parse --git-dir)
    GIT_COMMON_DIR=$(git rev-parse --git-common-dir)
    GIT_TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null)
    if [ -z "$GIT_TOPLEVEL" ]; then
        # A bare repository has no working tree; fall back to the git dir.
        GIT_TOPLEVEL=$GIT_DIR
    fi
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
    NULL_SHA=$(git hash-object --stdin </dev/null | tr '0-9a-f' '0')

    # Protected branch matcher (ERE for grep -E).
    PROTECTED_BRANCH='^(main|master|dev|release-.*|patch-.*)$'

    # Whole-run kill switch. The global switch and the per-hook switch both apply
    # to the entire run and is_enabled checks them identically, so they collapse
    # into one cached flag (env overrides config, resolved once — never re-forked
    # in the hot path). The hook name is fixed for the run (the runner exports
    # HOOK_NAME before sourcing this file); absent it, only the global switch
    # counts. The per-hook query is skipped once the global switch already applies.
    _WITS_HOOKS_OFF=0
    cfg_bool wits.hooks.disable && _WITS_HOOKS_OFF=1
    [ "$_WITS_HOOKS_OFF" = 0 ] && [ -n "${HOOK_NAME:-}" ] &&
        cfg_bool "wits.hooks.$HOOK_NAME.disable" && _WITS_HOOKS_OFF=1

    _WITS_ENV_LOADED=1
    export GIT_DIR GIT_COMMON_DIR GIT_TOPLEVEL CURRENT_BRANCH NULL_SHA \
           PROTECTED_BRANCH _WITS_HOOKS_OFF _WITS_ENV_LOADED
fi

# Colors
if [ -t 1 ]; then
    COLOR_RED=$(printf '\033[0;31m')
    COLOR_GREEN=$(printf '\033[0;32m')
    COLOR_YELLOW=$(printf '\033[0;33m')
    COLOR_CYAN=$(printf '\033[0;36m')
    COLOR_RESET=$(printf '\033[0m')
else
    COLOR_RED=""
    COLOR_GREEN=""
    COLOR_YELLOW=""
    COLOR_CYAN=""
    COLOR_RESET=""
fi

# Logging levels: 0=OFF, 1=ERROR, 2=WARN, 3=INFO, 4=DEBUG (default WARN).
# Configured via ENV: WITS_HOOKS_LOG_LEVEL
log_level=${WITS_HOOKS_LOG_LEVEL:-2}

# Enable shell tracing for debug level
if [ "$log_level" -ge 4 ]; then
    set -x
fi

log_debug() {
    if [ "$log_level" -ge 4 ]; then
        printf "%s[DEBUG]%s %s\n" "$COLOR_CYAN" "$COLOR_RESET" "$*"
    fi
}
log_info() {
    if [ "$log_level" -ge 3 ]; then
        printf "%s[INFO]%s %s\n" "$COLOR_GREEN" "$COLOR_RESET" "$*"
    fi
}
log_warn() {
    if [ "$log_level" -ge 2 ]; then
        printf "%s[WARN]%s %s\n" "$COLOR_YELLOW" "$COLOR_RESET" "$*"
    fi
}
log_error() {
    if [ "$log_level" -ge 1 ]; then
        printf "%s[ERROR]%s %s\n" "$COLOR_RED" "$COLOR_RESET" "$*";
    fi
}

# --- Common utilities ---

prompt_confirm() {
    msg="${1:-Are you sure want to continue? [y/N] }"
    if [ ! -t 0 ]; then
       exec < /dev/tty
    fi
    printf "%s%s " "$COLOR_YELLOW" "$msg" "$COLOR_RESET"
    read -r response
    case "$response" in
        [yY][eE][sS]|[yY]) return 0 ;;
        *) return 1 ;;
    esac
}

# Resolve build directories for a specific repo/branch using builder.py.
# Usage: resolve_build_dirs <repo_name> <branch_name>
resolve_build_dirs() {
    _repo="$1"
    _branch="$2"

    # Locate builder.py through the workflow environment; do nothing if either
    # the env file or the builder is absent.
    _env_file="${XDG_CONFIG_HOME:-$HOME/.config}/workflow/.env"
    [ -f "$_env_file" ] || return 0
    . "$_env_file"
    _builder_script="$PROJECTS_SCRIPT_DIR/builder.py"
    [ -f "$_builder_script" ] || return 0

    # stderr suppressed so a stray usage message doesn't leak into the output.
    _output=$(python3 "$_builder_script" list "$_repo" --branch "$_branch" --show-build-dir --no-submodules 2>/dev/null)

    echo "$_output" | while read -r line; do
        [ -z "$line" ] && continue
        case "$line" in ---*) continue ;; esac
        case "$line" in *"Build Dir"*) continue ;; esac
        case "$line" in *"not found"*) continue ;; esac
        case "$line" in *"No projects found"*) continue ;; esac
        case "$line" in Warning:*) continue ;; esac
        case "$line" in Error:*) continue ;; esac

        # Last column is the build dir; only emit absolute paths.
        _build_dir=$(echo "$line" | awk '{print $NF}')
        case "$_build_dir" in /*) echo "$_build_dir";; *) continue ;; esac
    done
}

# Resolve Main/Default Branch Name
# Usage: get_main_branch [remote_name]
get_main_branch() {
    _remote="${1:-origin}"

    # 0. Check User Configuration (Highest Priority)
    # Useful for monorepos or non-standard layouts.
    _cfg_branch=$(git config ginshio.workflow.main-branch 2>/dev/null)
    if [ -n "$_cfg_branch" ]; then
        echo "$_cfg_branch"
        return
    fi

    # 1. Check local tracking info (fastest)
    if _remote_head=$(git symbolic-ref "refs/remotes/$_remote/HEAD" 2>/dev/null); then
        echo "${_remote_head#refs/remotes/$_remote/}"
        return
    fi

    # 1.1 Verify if 'refs/remotes/origin/HEAD' is missing, try to detect it once?
    # This invokes network and is slow, so we only implicitly trust if cached.
    # Alternatively, users should run `git remote set-head origin -a`

    # 2. Guess common names
    for _candidate in main master trunk development; do
        if git show-ref --verify --quiet "refs/heads/$_candidate"; then
            echo "$_candidate"
            return
        fi
        if git show-ref --verify --quiet "refs/remotes/$_remote/$_candidate"; then
            echo "$_candidate"
            return
        fi
    done

    # 3. Fallback
    echo "master"
}

# --- Staged content ---
#
# A pre-commit hook judges what is *being committed* — the staged blob — not the
# working tree, which may carry unstaged edits. These helpers let every script
# speak in terms of the index consistently.

# The staged paths a pre-commit script cares about: added, copied, or modified.
staged_files() {
    git diff --cached --name-only --diff-filter=ACM
}

# The staged content of a file, straight from the index.
staged_blob() {
    git cat-file blob ":$1" 2>/dev/null
}

# Size of the staged blob, in bytes.
staged_size() {
    git cat-file -s ":$1" 2>/dev/null
}

# True when the staged blob is text (git's own heuristic: a diff against a
# binary blob reports '-' additions instead of a line count).
is_staged_text() {
    [ "$(git diff --cached --numstat -- "$1" | cut -f1)" != "-" ]
}

# True when a file is managed by an encrypting clean/smudge filter (transcrypt,
# git-crypt): its staged blob is ciphertext, not content we should format or
# inspect, so content hooks skip it.
is_encrypted() {
    case "$(git check-attr filter -- "$1" 2>/dev/null)" in
        *": filter: transcrypt"|*": filter: git-crypt"|*": filter: crypt") return 0 ;;
        *) return 1 ;;
    esac
}

# True when the working tree differs from the index for a file — i.e. it is only
# partially staged, so rewriting the whole file would capture unstaged edits.
has_unstaged_changes() {
    ! git diff --quiet -- "$1"
}

# Format a file's *staged content* in place in the index, leaving unstaged edits
# untouched. The command reads the blob on stdin and writes the result to
# stdout; if it changes anything, the new content is written back to the index,
# and to the working tree too when that is safe (no unstaged edits to clobber).
# Usage: apply_to_staged <file> <formatter> [args...]
apply_to_staged() {
    _f="$1"
    shift
    _in=$(mktemp) || return 1
    _out=$(mktemp) || { rm -f "$_in"; return 1; }

    staged_blob "$_f" > "$_in"
    if "$@" < "$_in" > "$_out" 2>/dev/null && ! cmp -s "$_in" "$_out"; then
        # Decide whether to sync the working tree *before* rewriting the index —
        # afterwards the (still-unformatted) working copy would always look like
        # it differs from the freshly-updated index.
        _sync_worktree=1
        has_unstaged_changes "$_f" && _sync_worktree=0

        _mode=$(git ls-files --stage -- "$_f" | cut -d' ' -f1)
        _sha=$(git hash-object -w "$_out")
        git update-index --cacheinfo "$_mode" "$_sha" "$_f"

        # Update the working copy only when it held nothing we'd overwrite;
        # otherwise the index is fixed and the working copy is left for the next
        # `git add` to pick up, so unstaged edits survive untouched.
        [ "$_sync_worktree" -eq 1 ] && git checkout-index -f -- "$_f"
    fi

    rm -f "$_in" "$_out"
}
