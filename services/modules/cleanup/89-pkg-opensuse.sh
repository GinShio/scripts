#!/bin/sh
#@tags: domain:cleanup, type:nightly, os:opensuse, dep:zypper
set -u

. "${XDG_CONFIG_HOME:-$HOME/.config}/workflow/.env"

sudo -A -- zypper clean --all >/dev/null 2>&1
