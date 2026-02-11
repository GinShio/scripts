#!/bin/sh
#@tags: domain:system, type:nightly, os:debian
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

sudo -A -- apt update
sudo -AE -- apt full-upgrade -y
