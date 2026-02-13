#!/bin/sh
#
# Service Runner
# Smart scheduler that executes scripts based on tags and system environment.
# Usage: ./runner.sh <type>
# Example: ./runner.sh autostart
#

set -u

# ==============================================================================
# 1. Initialization
# ==============================================================================

. "${XDG_CONFIG_HOME:-$HOME/.config}/workflow/.env"
export PROJECTS_ROOT_DIR PROJECTS_SCRIPT_DIR
export DOTFILES_ROOT_DIR DOTFILES_CURRENT_PROFILE

# Load Libraries
# shellcheck source=../common/tags.sh
. "$PROJECTS_SCRIPT_DIR/common/tags.sh"
# shellcheck source=../common/detect.sh
. "$PROJECTS_SCRIPT_DIR/common/detect.sh"

# Validate Input
if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <type>" >&2
    echo "  <type>: e.g., 'autostart', 'nightly'" >&2
    exit 1
fi

TARGET_TYPE="type:$1"
MODULES_DIR="$PROJECTS_SCRIPT_DIR/services/modules"

# ==============================================================================
# 2. Environment Detection
# ==============================================================================

# Gather System Info
CURRENT_OS=$(get_os)         # linux, darwin, freebsd...
CURRENT_DISTRO=""
if [ "$CURRENT_OS" = "linux" ]; then
    CURRENT_DISTRO=$(detect_distro) # opensuse, debian...
fi

# Desktop Environment Detection
CURRENT_DE=$(detect_desktop) # gnome, kde, sway, headless...

# Hardware Detection via detect.sh
GPU_VENDORS=$(detect_gpu_vendor)
CPU_VENDOR=$(detect_cpu_vendor)

IS_LAPTOP=0
if is_laptop; then IS_LAPTOP=1; fi

# ==============================================================================
# 3. Filtering Logic
# ==============================================================================

# Helper: Check if a script's specific tag matches current environment
# Returns 0 (pass) or 1 (skip)
check_tag_constraint() {
    _tag="$1"
    
    # Prefix: os:*
    case "$_tag" in
        os:*) 
            _req_os="${_tag#os:}"
            # Check OS Family
            if [ "$_req_os" = "$CURRENT_OS" ]; then return 0; fi
            # Check Distro (Linux specific)
            if [ "$CURRENT_OS" = "linux" ] && [ "$_req_os" = "$CURRENT_DISTRO" ]; then return 0; fi
            return 1
            ;;
    esac

    # Prefix: gpu:* (GPU Vendor/Presence)
    case "$_tag" in
        gpu:any)
            if [ -n "$GPU_VENDORS" ]; then return 0; fi
            return 1 ;;
        gpu:*)
            _req_gpu="${_tag#gpu:}"
            case "$GPU_VENDORS" in *"$_req_gpu"*) return 0 ;; esac
            return 1 ;;
    esac

    # Prefix: cpu:* (CPU Vendor)
    case "$_tag" in
        cpu:*)
            _req_cpu="${_tag#cpu:}"
            if [ "$_req_cpu" = "$CPU_VENDOR" ]; then return 0; fi
            return 1 ;;
    esac

    # Prefix: de:* (Desktop Environment)
    case "$_tag" in
        de:*)
            _req_de="${_tag#de:}"
            # Multi-value check? For now simple exact match or substring
            if [ "$_req_de" = "$CURRENT_DE" ]; then return 0; fi
            return 1 ;;
    esac

    # Prefix: hw:* (Other Hardware)
    case "$_tag" in
        hw:laptop)
            [ "$IS_LAPTOP" -eq 1 ] && return 0
            return 1 ;;
    esac

    # Prefix: chassis:* (Future proofing example, alias to hw usually)
    case "$_tag" in
        chassis:laptop)
            [ "$IS_LAPTOP" -eq 1 ] && return 0
            return 1 ;;
        # chassis:desktop not strictly implemented yet but structure allows it
    esac

    # Prefix: dep:* (Dependency Check)
    case "$_tag" in
        dep:*)
            _cmd="${_tag#dep:}"
            if command -v "$_cmd" >/dev/null 2>&1; then return 0; fi
            return 1
            ;;
    esac

    # Tag is not a constraint type we know, so it doesn't block execution.
    return 0
}

# Helper: Decide if script should run based on ALL its tags
should_run_script() {
    _file="$1"
    _file_tags=$(tags_get "$_file")
    
    # Iterate over all tags in the file
    for _t in $_file_tags; do
        
        # Identify constraint tags
        # We explicitly list all prefixes that impose an execution constraint
        case "$_t" in
            os:*|hw:*|dep:*|gpu:*|cpu:*|chassis:*|de:*)
                if ! check_tag_constraint "$_t"; then
                    # Constraint failed -> Skip execution
                    return 1
                fi
                ;;
            # Future expansion for env:* (e.g. kde/gnome) can go here
        esac
    done
    
    # All constraints passed (or none existed)
    return 0
}


# ==============================================================================
# 4. Main Execution Loop
# ==============================================================================

echo "[Runner] scanning modules for $TARGET_TYPE..."

# 1. Find candidates (files matching the requested type)
# We use sort to ensure deterministic order (NN-name.sh) based on filename
# Sort by filename (field 1) then by full path (field 2) to handle same-named files deterministically
tags_find_all "$MODULES_DIR" "$TARGET_TYPE" | awk -F/ '{print $NF "\t" $0}' | sort -k1,1 -k2,2 | cut -f2- | while read -r script; do
    
    script_name=$(basename "$script")
    
    # 2. Check Constraints
    if should_run_script "$script"; then
        echo "[Runner] executing: $script_name"

        # 3. Execute
        # Run in subshell to protect runner's environment
        (
            # Pass the trigger type as argument just in case
            sh "$script" "$1"
        ) 
        _status=$?
        
        if [ $_status -ne 0 ]; then
            echo "[Runner] warning: $script_name exited with error $_status" >&2
        fi
    else
        # Verbose debug (optional, currently commented out)
        # echo "[Runner] skipping: $script_name (constraints unmet)"
        :
    fi
done

echo "[Runner] finished."
