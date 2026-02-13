#!/bin/sh
#@tags: domain:system, type:nightly, os:fedora
set -eu

# shellcheck disable=SC1091
. "$PROJECTS_SCRIPT_DIR/common/proxy.sh"

cleanup() {
    sudo -k
    # shellcheck disable=SC1091
    . "$PROJECTS_SCRIPT_DIR/common/unproxy.sh"
}
trap cleanup EXIT

sudo -AE -- dnf upgrade -y --refresh
