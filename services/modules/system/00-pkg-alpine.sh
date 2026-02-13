#!/bin/sh
#@tags: domain:system, type:nightly, os:alpine
set -eu

# shellcheck disable=SC1091
. "$PROJECTS_SCRIPT_DIR/common/proxy.sh"

cleanup() {
    sudo -k
    # shellcheck disable=SC1091
    . "$PROJECTS_SCRIPT_DIR/common/unproxy.sh"
}
trap cleanup EXIT

sudo -A -- apk update
sudo -A -- apk upgrade
