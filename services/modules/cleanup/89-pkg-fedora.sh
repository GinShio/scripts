#!/bin/sh
#@tags: domain:cleanup, type:nightly, os:fedora
set -u

. "${XDG_CONFIG_HOME:-$HOME/.config}/workflow/.env"

sudo -A -- dnf clean all >/dev/null 2>&1
sudo -A -- dnf autoremove -y >/dev/null 2>&1
