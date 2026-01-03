#!/usr/bin/env bash

source $XDG_CONFIG_HOME/workflow/.env

trap "sudo -k; source $PROJECTS_SCRIPT_DIR/common/unproxy.sh" EXIT
source $PROJECTS_SCRIPT_DIR/common/proxy.sh

sudo -AE -- zypper ref
zypper lr |awk 'NR > 4 && $3~/openSUSE:/ {print $3}' |xargs -I@ sudo -AE -- zypper up -y --repo @
sudo -A -- zypper up -y --allow-vendor-change
sudo -A -- zypper dup -y --allow-vendor-change
