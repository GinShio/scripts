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

# --- Lazily-resolved repo facts ---
#
# Each of these costs a `git` (or config) subprocess, and most hooks need only a
# couple of them — a reference-transaction fire that is not a committed branch
# deletion needs none at all. So rather than resolve every fact eagerly on every
# hook run, each is a memoizing getter that fills its well-known variable on
# first use and is a no-op thereafter. A POSIX function shares the caller's
# scope, so `null_sha` (etc.) leaves `$NULL_SHA` populated for the plain
# `$NULL_SHA` reads the scripts already use — no command-substitution fork at the
# call site. The getter also honours a value already in the environment (one git
# itself exported, or one a parent resolved), so it stays free when possible.
#
# The contract: a script calls the getter it needs *after* its early-exit
# guards, then reads the variable as before. That placement is the whole point —
# the fork happens only once the script has committed to doing real work.
git_dir() {
    [ -n "${GIT_DIR:-}" ] || GIT_DIR=$(git rev-parse --git-dir)
}
git_common_dir() {
    [ -n "${GIT_COMMON_DIR:-}" ] || GIT_COMMON_DIR=$(git rev-parse --git-common-dir)
}
git_toplevel() {
    if [ -z "${GIT_TOPLEVEL:-}" ]; then
        GIT_TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null)
        # A bare repository has no working tree; fall back to the git dir.
        [ -n "$GIT_TOPLEVEL" ] || { git_dir; GIT_TOPLEVEL=$GIT_DIR; }
    fi
}
current_branch() {
    [ -n "${CURRENT_BRANCH:-}" ] || CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
}
null_sha() {
    [ -n "${NULL_SHA:-}" ] || NULL_SHA=$(git hash-object --stdin </dev/null | tr '0-9a-f' '0')
}
# Protected-branch matcher (ERE for grep -E). Overridable through
# wits.hooks.protected-branch (env: WITS_HOOKS_PROTECTED_BRANCH); the default
# covers the usual shared branches.
protected_branch() {
    [ -n "${PROTECTED_BRANCH:-}" ] ||
        PROTECTED_BRANCH=$(cfg_value wits.hooks.protected-branch '^(main|master|dev|release-.*|patch-.*)$')
}

# Repo-scoped state that *is* resolved once per run and exported for the children
# to inherit. Only two things earn that: the kill switch (the dispatcher consults
# it for every candidate) and the pre-commit staged cache (shared by every
# content script). Everything else is lazy (above).
#
# The runner sources this file once, then execs each hook script as its own
# process; those children re-source it for the functions (functions cannot be
# exported) and inherit these exported values, so the guard below never re-runs
# in a child. Caveat: the guard keys on the process environment, so a hook that
# recursively drove another repository's hooks would inherit the outer values —
# already true of the GIT_DIR git itself exports, and no hook here does that.
if [ -z "${_WITS_ENV_LOADED:-}" ]; then
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

    # Staged-content cache, pre-commit only. A single `--numstat` both enumerates
    # the staged paths (added/copied/modified) and classifies each as text or
    # binary (a binary blob shows '-' for its line counts). That one query
    # replaces the per-file `git diff`/`git cat-file` that sanity, encoding,
    # formatter, and linter would each otherwise fork per file; the children
    # inherit the result through the environment. Every other hook stages no
    # content, so it skips this and the helpers fall back to a live query.
    _WITS_STAGED_CACHED=""
    STAGED_FILES=""
    STAGED_TEXT_FILES=""
    if [ "$_WITS_HOOKS_OFF" = 0 ] && [ "${HOOK_NAME:-}" = pre-commit ]; then
        _numstat=$(git diff --cached --numstat --diff-filter=ACM 2>/dev/null)
        STAGED_FILES=$(printf '%s\n' "$_numstat" | awk -F'\t' 'NF>=3 { print $3 }')
        STAGED_TEXT_FILES=$(printf '%s\n' "$_numstat" | awk -F'\t' 'NF>=3 && $1 != "-" { print $3 }')
        _WITS_STAGED_CACHED=1
    fi

    _WITS_ENV_LOADED=1
    export _WITS_HOOKS_OFF _WITS_ENV_LOADED \
           STAGED_FILES STAGED_TEXT_FILES _WITS_STAGED_CACHED
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

# A literal newline, for building and matching newline-delimited lists in the
# portable subset (no arrays, no `read -d`).
LF='
'

# Logging levels: 0=OFF, 1=ERROR, 2=WARN, 3=INFO, 4=DEBUG (default WARN).
# Configured via ENV: WITS_HOOKS_LOG_LEVEL
log_level=${WITS_HOOKS_LOG_LEVEL:-2}

# Enable shell tracing for debug level
if [ "$log_level" -ge 4 ]; then
    set -x
fi

# All diagnostics go to stderr: a hook's stdout can be meaningful (or piped),
# and by convention progress/errors belong on fd 2 so they stay visible even
# when stdout is captured or redirected.
log_debug() {
    if [ "$log_level" -ge 4 ]; then
        printf "%s[DEBUG]%s %s\n" "$COLOR_CYAN" "$COLOR_RESET" "$*" >&2
    fi
}
log_info() {
    if [ "$log_level" -ge 3 ]; then
        printf "%s[INFO]%s %s\n" "$COLOR_GREEN" "$COLOR_RESET" "$*" >&2
    fi
}
log_warn() {
    if [ "$log_level" -ge 2 ]; then
        printf "%s[WARN]%s %s\n" "$COLOR_YELLOW" "$COLOR_RESET" "$*" >&2
    fi
}
log_error() {
    if [ "$log_level" -ge 1 ]; then
        printf "%s[ERROR]%s %s\n" "$COLOR_RED" "$COLOR_RESET" "$*" >&2
    fi
}

# --- Common utilities ---

prompt_confirm() {
    _msg="${1:-Are you sure want to continue? [y/N] }"
    # Read the answer straight from the controlling terminal for this one prompt,
    # rather than `exec < /dev/tty`, which would permanently reassign fd 0 and
    # swallow whatever the hook is still streaming on stdin (e.g. the pre-push
    # ref list the caller loops over). No terminal (CI, no tty) means we cannot
    # ask, so decline safely.
    [ -r /dev/tty ] || return 1
    printf "%s%s%s " "$COLOR_YELLOW" "$_msg" "$COLOR_RESET" >&2
    read -r _response < /dev/tty || return 1
    case "$_response" in
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
# Served from the pre-commit cache when present (resolved once in the state
# block above), otherwise a live query so the helper still works in any hook.
staged_files() {
    if [ -n "${_WITS_STAGED_CACHED:-}" ]; then
        [ -n "$STAGED_FILES" ] && printf '%s\n' "$STAGED_FILES"
        return 0
    fi
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
# binary blob reports '-' additions instead of a line count). When the
# pre-commit cache is populated this is a membership test against the precomputed
# text set (no fork); otherwise it falls back to a live per-file query.
is_staged_text() {
    if [ -n "${_WITS_STAGED_CACHED:-}" ]; then
        case "$LF$STAGED_TEXT_FILES$LF" in
            *"$LF$1$LF"*) return 0 ;;
            *) return 1 ;;
        esac
    fi
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

# Echo the staged text paths whose extension matches one of the given suffixes,
# skipping binary and encrypted blobs. This is the one line every per-language
# formatter/linter shares, so a new language is just a new one-concern script
# that calls this with its extensions. Usage: staged_lang_files .py .pyi
staged_lang_files() {
    staged_files | while IFS= read -r _slf; do
        is_staged_text "$_slf" || continue
        is_encrypted "$_slf" && continue
        for _ext in "$@"; do
            case "$_slf" in
                *"$_ext") printf '%s\n' "$_slf"; break ;;
            esac
        done
    done
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
