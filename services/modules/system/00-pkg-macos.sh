#!/bin/sh
#@tags: domain:system, type:nightly, os:darwin
set -eu

# shellcheck disable=SC1091
. "${XDG_CONFIG_HOME:-$HOME/.config}/workflow/.env"
# shellcheck disable=SC1091
. "$PROJECTS_SCRIPT_DIR/common/proxy.sh"

cleanup() {
    # shellcheck disable=SC1091
    . "$PROJECTS_SCRIPT_DIR/common/unproxy.sh"
}
trap cleanup EXIT

if command -v brew >/dev/null 2>&1; then
    brew update
    brew upgrade
fi
