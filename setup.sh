#!/usr/bin/env bash

# Best Practice: Better Bash standard mode
# -e: Exit immediately if a command exits with a non-zero status.
# -u: Treat unset variables as an error when substituting.
# -o pipefail: the return value of a pipeline is the status of the last command to exit with a non-zero status.
set -euo pipefail
trap "sudo -k" EXIT

# ==========================================
# Portability Helpers
# ==========================================

# Robust way to resolve script directory without 'readlink -f' (Linux specific)
resolve_script_dir() {
    local source="${BASH_SOURCE[0]}"
    # Resolve symlinks
    while [ -h "$source" ]; do
        local dir="$(cd -P "$(dirname "$source")" && pwd)"
        source="$(readlink "$source")"
        [[ $source != /* ]] && source="$dir/$source"
    done
    echo "$(cd -P "$(dirname "$source")" && pwd)"
}

SCRIPT_DIR=$(resolve_script_dir)

show_help() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Description:
  Automated environment setup script for Linux systems.
  Handles system initialization, package installation, and configuration.
  Supports modular profiles and usages (e.g., dev, personal, server).

Options:
  -h, --help            Show this help message and exit
  --profile <name>      Set usage profile (default: personal)
  --usage <type>        Set usage type (e.g., dev, server) (default: dev)
  --swapsize <GB>       Set swapfile size in GB (default: auto, ~2x RAM)
  --hostname <name>     Set system hostname (default: auto-generated)

Environment Variables:
  Required (Non-interactive):
    ROOT_PASSPHRASE     Root/Sudo password for unattended installation.
                        If not set in interactive mode, script will ask.
EOF

    # Dynamic Help Registration: Scan usages for help files or hooks
    # Convention: setup/usages/<usage>/HELP.md or similar
    # For now, we look for a common help text if available.
    if [ -d "$SCRIPT_DIR/setup/usages" ]; then
        echo -e "\nRegistered Usage Modules:"
        for usage_dir in "$SCRIPT_DIR/setup/usages"/*; do
            [ -d "$usage_dir" ] || continue
            usage_name=$(basename "$usage_dir")
            echo "  * $usage_name"

            # Check for optional env var definition file
            if [ -f "$usage_dir/ENV_VARS" ]; then
                echo "    Environment Variables for '$usage_name':"
                sed 's/^/      /' "$usage_dir/ENV_VARS"
            fi
        done
    fi

    cat <<EOF

Examples:
  ./setup.sh --profile work --usage server --hostname build-node-01
  ROOT_PASSPHRASE="secret" ./setup.sh --swapsize 16
EOF
}

# Argument Parsing
# Check if we are using GNU getopt (Linux standard).
# MacOS/BSD getopt does not support long arguments in this format.
if ! getopt -T >/dev/null 2>&1; then
    if [ $? -ne 4 ]; then
        echo "Warning: Extended getopt not detected. Long options might fail."
    fi
fi
TEMP=$(getopt -o h --long help,swapsize:,hostname:,tidever:,profile:,usage: -- "$@")
eval set -- "$TEMP"

SETUP_PROFILE="personal"
SETUP_USAGE="dev"
SETUP_SWAPSIZE=""
SETUP_HOSTNAME=""

while true; do
    case "$1" in
        -h|--help) show_help; exit 0 ;;
        --swapsize) SETUP_SWAPSIZE=$2; shift 2;;
        --hostname) SETUP_HOSTNAME=$2; shift 2;;
        --profile) SETUP_PROFILE=$2; shift 2;;
        --usage) SETUP_USAGE=$2; shift 2;;
        --) shift 2; break;;
        *) echo "Internal error!"; exit 1;;
    esac
done

ASKPASS_SCRIPT="$SCRIPT_DIR/common/get-root-passphrase.sh"

if [ -z "${ROOT_PASSPHRASE:-}" ] && [ "$USER" != "root" ]; then
    echo "Error: ROOT_PASSPHRASE not set and running non-interactively."
    exit 1
fi

if [ -f "$ASKPASS_SCRIPT" ]; then
    export SUDO_ASKPASS="$ASKPASS_SCRIPT"
else
    echo "Error: Missing SUDO_ASKPASS script at ${ASKPASS_SCRIPT}"
    exit 1
fi

# Check for OS type - We are essentially Linux specific
OS_TYPE=$(uname -s)
if [[ "$OS_TYPE" != "Linux" ]]; then
    echo "Error: This script is explicitly designed for Linux systems."
    echo "Detected OS: $OS_TYPE"
    exit 1
fi

# OS Detection
. /etc/os-release
DISTRO_NAME="${NAME:-${DISTRIB_ID}} ${VERSION_ID:-${DISTRIB_RELEASE}}"
DISTRO_ID="${ID}"

# Calculate Swap Size if not provided
if [[ -z "$SETUP_SWAPSIZE" ]]; then
    # Calculate 2x RAM in GiB (approximately)
    SETUP_SWAPSIZE=$(awk '/MemTotal/{print int($2 / 1048576 * 2)}' /proc/meminfo)
    # Fallback to 4 GiB
    if [[ "$SETUP_SWAPSIZE" -lt 1 ]]; then SETUP_SWAPSIZE=4; fi
fi

# Calculate Hostname
if [[ -z "$SETUP_HOSTNAME" ]]; then
    PREFIX=""
    if [[ "$SETUP_PROFILE" != "personal" ]]; then
        PREFIX="$SETUP_PROFILE-"
    fi
    DISTRO_SUFFIX=$(echo "$DISTRO_NAME" | awk '{ print $1 }')
    SETUP_HOSTNAME="${PREFIX}${USER}-${DISTRO_SUFFIX}"
fi

# Verify sudo access early
if ! sudo -A true; then
    echo "Error: Incorrect Password or 'sudo -A' failure."
    echo "Ensure SUDO_ASKPASS script is working correctly."
    exit 1
fi

# ==========================================
# Modular Phased Bringup
# ==========================================

# Export necessary variables for child scripts
export SCRIPT_DIR
export SETUP_PROFILE SETUP_USAGE SETUP_SWAPSIZE SETUP_HOSTNAME
# Export Distro info so phases don't need to re-detect (though phase_system has fallback)
export DISTRO_ID DISTRO_NAME

# Runner logic for Usage-based architecture
run_usage_phase() {
    local usage="$1"
    local phase="$2"
    local phase_dir="$SCRIPT_DIR/setup/usages/$usage/$phase"

    echo ">>> [Phase: $phase] Checking configuration..."

    if [ ! -d "$phase_dir" ]; then
        echo "Info: No configuration directory for phase '$phase' in usage '$usage' ($phase_dir)."
        return 0
    fi

    # Execute scripts in sort order
    for script in "$phase_dir"/*.sh; do
        [ -e "$script" ] || continue
        echo ":: Running $(basename "$script")..."
        bash "$script"
    done
}

echo "Starting Modular Bringup..."
# Fixed Order Phases
# system: Root level setups (distro, groups, swap)
# apps: Package managers (flatpak, pipx)
# user: User level configs (dotfiles, shell)
# services: Systemd units
PHASES=(system apps user services)

for phase in "${PHASES[@]}"; do
    run_usage_phase "common" "$phase"
    run_usage_phase "$SETUP_USAGE" "$phase"
done

echo "Bringup script completed successfully."
