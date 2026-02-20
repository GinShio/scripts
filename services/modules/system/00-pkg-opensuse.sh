#!/bin/sh
#@tags: domain:system, type:nightly, os:opensuse, dep:zypper, schedule:5d
set -eu

# shellcheck disable=SC1091
. "$PROJECTS_SCRIPT_DIR/common/proxy.sh"

cleanup() {
    sudo -k
    # shellcheck disable=SC1091
    . "$PROJECTS_SCRIPT_DIR/common/unproxy.sh"
}
trap cleanup EXIT

sudo -AE -- zypper ref
# Update specific openSUSE repositories if they exist
zypper lr | awk 'NR > 4 && $3~/openSUSE:/ {print $3}' | xargs -r -I@ sudo -AE -- zypper up -y --repo @
sudo -A -- zypper up -y --allow-vendor-change
sudo -A -- zypper dup -y --allow-vendor-change
