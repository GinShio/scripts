#!/bin/sh
set -eu

# shellcheck disable=SC1091
. "$XDG_CONFIG_HOME/workflow/.env"
# shellcheck disable=SC1091
. "$PROJECTS_SCRIPT_DIR/common/proxy.sh"

cleanup() {
    sudo -k
    # shellcheck disable=SC1091
    . "$PROJECTS_SCRIPT_DIR/common/unproxy.sh"
}
trap cleanup EXIT

sudo -AE -- zypper ref
zypper lr | awk 'NR > 4 && $3~/openSUSE:/ {print $3}' | xargs -I@ sudo -AE -- zypper up -y --repo @
sudo -A -- zypper up -y --allow-vendor-change
sudo -A -- zypper dup -y --allow-vendor-change
