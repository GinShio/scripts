#!/bin/sh
#@tags: domain:cleanup, type:nightly, os:alpine
set -u

. "${XDG_CONFIG_HOME:-$HOME/.config}/workflow/.env"

sudo -A -- apk cache clean >/dev/null 2>&1
