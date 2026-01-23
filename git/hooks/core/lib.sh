#!/bin/sh

# Core Git Hooks Library - POSIX Compliant

# Resolve Git Locations
GIT_DIR=$(git rev-parse --git-dir)
GIT_COMMON_DIR=$(git rev-parse --git-common-dir)
GIT_TOPLEVEL=$(git rev-parse --show-toplevel 2>/dev/null)
if [ -n "$GIT_TOPLEVEL" ]; then
    # bare repo hasn't GIT_TOPLEVEL
    GIT_TOPLEVEL=$GIT_DIR
fi
export GIT_DIR GIT_COMMON_DIR GIT_TOPLEVEL
export CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
export NULL_SHA="0000000000000000000000000000000000000000"

# Logic for protected branch.
# Matches master, dev, release-*, patch-*
# Using Extended Regex (ERE) for grep -E
export PROTECTED_BRANCH='^(master|dev|release-.*|patch-.*)$'

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

# Logging levels: 0=OFF, 1=ERROR, 2=WARN, 3=INFO, 4=DEBUG
# Default: WARN (2)
# Configured via ENV: GINSHIO_HOOKS_LOG_LEVEL
log_level=${GINSHIO_HOOKS_LOG_LEVEL:-2}

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

# Helper to check boolean values
is_truthy() {
    case "$1" in
        [Yy][Ee][Ss]|[Yy]|[Tt][Rr][Uu][Ee]|1|[Oo][Nn]) return 0 ;;
        *) return 1 ;;
    esac
}

# Cache config to avoid repeated git calls
# Variable name format: _CFG_<hook>_<script>_DISABLE
# We use a simple prefix for global disable
_RAW_CFG_DISABLE_ALL=$(git config --bool hooks.ginshio.disable 2>/dev/null)

# Check if a hook or specific script is enabled
is_enabled() {
    hook_name="$1"
    script_name="$2"

    # 1. Global Disable
    if is_truthy "${GINSHIO_HOOKS_DISABLE_ALL:-false}"; then return 1; fi
    if is_truthy "$_RAW_CFG_DISABLE_ALL"; then return 1; fi

    # 2. Hook Level Disable
    # Construct env var name roughly (convert - to _)
    env_hook_name=$(echo "$hook_name" | tr '-' '_' | tr '[:lower:]' '[:upper:]')
    eval env_val="\$GINSHIO_HOOKS_${env_hook_name}_DISABLE"
    if is_truthy "$env_val"; then return 1; fi

    # Optimization: Only query specific config when needed
    cfg_hook_disable=$(git config --bool "hooks.ginshio.$hook_name.disable" 2>/dev/null)
    if is_truthy "$cfg_hook_disable"; then return 1; fi

    # 3. Script Level Disable
    if [ -n "$script_name" ]; then
        cfg_script_disable=$(git config --bool "hooks.ginshio.$hook_name.$script_name-disable" 2>/dev/null)
        if is_truthy "$cfg_script_disable"; then return 1; fi
    fi

    return 0
}

# Cross-platform sed -i substitute
# Usage: run_sed_i <expression> <file>
run_sed_i() {
    expression="$1"
    file="$2"
    tmp_file="${file}.tmp"

    if sed "$expression" "$file" > "$tmp_file"; then
        mv "$tmp_file" "$file"
    else
        rm -f "$tmp_file"
        return 1
    fi
}

# Run all executable scripts in a directory
run_hook_dir() {
    dir_path="$1"
    hook_name="$2"
    shift 2

    if [ ! -d "$dir_path" ]; then return 0; fi

    for script in "$dir_path"/*; do
        [ -e "$script" ] || continue
        if [ -f "$script" ] && [ -x "$script" ]; then
            script_base=$(basename "$script")
            if is_enabled "$hook_name" "$script_base"; then
                "$script" "$@"
                exit_code=$?
                if [ $exit_code -ne 0 ]; then
                    log_error "Hook script '$script_base' failed with exit code $exit_code"
                    exit $exit_code
                fi
            fi
        fi
    done
}

# Run legacy/local hooks located in the repository's .git/hooks directory
run_local_hook() {
    hook_name="$1"
    shift 1
    local_hooks_dir="$GIT_DIR/hooks"
    local_hook_script="$local_hooks_dir/$hook_name"

    if [ -f "$local_hook_script" ] && [ -x "$local_hook_script" ]; then
        if is_enabled "$hook_name" "local"; then
            "$local_hook_script" "$@"
            exit_code=$?
            if [ $exit_code -ne 0 ]; then
                log_error "Local hook '$hook_name' failed"
                exit $exit_code
            fi
        fi
    fi
}

# --- Common Utilities ---

get_current_branch() {
    echo "$CURRENT_BRANCH"
}

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

check_program() {
    program="$1"
    if ! command -v "$program" >/dev/null 2>&1; then
        log_error "This repository is configured for '$program' but it was not found on your path."
        exit 2
    fi
}

# Resolve build directories for a specific repo/branch using builder.py
# Usage: resolve_build_dirs <repo_name> <branch_name>
resolve_build_dirs() {
    _repo="$1"
    _branch="$2"

    # Locate builder script
     . "$HOME/.config/workflow/.env"
    _builder_script="$PROJECTS_SCRIPT_DIR/builder.py"

    # Query builder script
    # We suppress stderr to avoid spamming usage info if script is weird.
    _output=$(python3 "$_builder_script" list "$_repo" --branch "$_branch" --show-build-dir --no-submodules 2>/dev/null)

    echo "$_output" | while read -r line; do
        # Skip empty lines
        [ -z "$line" ] && continue
        # Skip divider lines
        case "$line" in ---*) continue ;; esac
        # Skip header line (contains "Build Dir")
        case "$line" in *"Build Dir"*) continue ;; esac
        # Skip known non-data lines from builder.py
        case "$line" in *"not found"*) continue ;; esac
        case "$line" in *"No projects found"*) continue ;; esac
        case "$line" in Warning:*) continue ;; esac
        case "$line" in Error:*) continue ;; esac

        # Extract last column (Build Dir) using awk
        _build_dir=$(echo "$line" | awk '{print $NF}')

        # Safety check: Build dir must be an absolute path
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
