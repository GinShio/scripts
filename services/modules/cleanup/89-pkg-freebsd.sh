#!/bin/sh
#@tags: domain:cleanup, type:nightly, os:freebsd
set -u

. "${XDG_CONFIG_HOME:-$HOME/.config}/workflow/.env"

sudo -A -- pkg clean -y >/dev/null 2>&1
sudo -A -- pkg autoremove -y >/dev/null 2>&1
