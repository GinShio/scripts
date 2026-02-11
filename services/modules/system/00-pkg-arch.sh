#!/bin/sh
#@tags: domain:system, type:nightly, os:arch
set -eu

# shellcheck disable=SC1091
. "${XDG_CONFIG_HOME:-$HOME/.config}/workflow/.env"
# shellcheck disable=SC1091
. "$PROJECTS_SCRIPT_DIR/common/proxy.sh"

cleanup() {
    sudo -k
    # shellcheck disable=SC1091
    . "$PROJECTS_SCRIPT_DIR/common/unproxy.sh"
}
trap cleanup EXIT

if command -v yay >/dev/null 2>&1; then
    # yay handles both repo and AUR updates
    yay -Syu --noconfirm
elif command -v paru >/dev/null 2>&1; then
    paru -Syu --noconfirm
else
    sudo -A -- pacman -Syu --noconfirm
fi
