#!/bin/sh
set -u

# Usage: runner.sh <log_prefix> <module_name> [module_name2] ...
# Example: runner.sh nightly core dev
# Example: runner.sh autostart desktop

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <log_prefix> <module_name> [module_name] ..." >&2
    exit 1
fi

#shellcheck disable=SC1091
. "$XDG_CONFIG_HOME/workflow/.env"

MODULES_BASE="$PROJECTS_SCRIPT_DIR/services/modules"

LOG_PREFIX="$1"
shift

# Resolve module names to absolute paths
TARGET_DIRS=""
MISSING_MODULES=0

for module in "$@"; do
    # 1. Check if it's a module name in the default modules directory
    if [ -d "$MODULES_BASE/$module" ]; then
        TARGET_DIRS="$TARGET_DIRS $MODULES_BASE/$module"
    # 2. Check if it's already a valid path (absolute or relative)
    elif [ -d "$module" ]; then
        TARGET_DIRS="$TARGET_DIRS $module"
    else
        printf '[%s] error: module or directory not found: %s\n' "$LOG_PREFIX" "$module" >&2
        MISSING_MODULES=1
    fi
done

if [ "$MISSING_MODULES" -eq 1 ]; then
    exit 1
fi

run_script() {
    _script_path="$1"
    _script_name=$(basename "$_script_path")

    printf '[%s] running %s\n' "$LOG_PREFIX" "$_script_name"
    
    # Execute with standard sh
    if ! /bin/sh "$_script_path"; then
        printf '[%s] %s failed\n' "$LOG_PREFIX" "$_script_name" >&2
        return 1
    fi

    return 0
}

# Core logic:
# We pass the resolved $TARGET_DIRS to find.
# Note: We intentionally leave $TARGET_DIRS unquoted here to allow word splitting 
# into multiple arguments for 'find'.
# shellcheck disable=SC2086
find $TARGET_DIRS -maxdepth 1 -name "*.sh" -printf "%f\t%p\n" 2>/dev/null | sort -k1 | cut -f2 | while IFS= read -r script; do
    if ! run_script "$script"; then
        # Log failure but continue
        :
    fi
done

exit 0
