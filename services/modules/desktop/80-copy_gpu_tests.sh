#!/bin/sh
#@tags: domain:desktop, type:autostart, gpu:any, dep:python3
set -eu

# shellcheck disable=SC1091
. "${XDG_CONFIG_HOME:-$HOME/.config}/workflow/.env"
python3 "$PROJECTS_SCRIPT_DIR/gputest.py" install
python3 "$PROJECTS_SCRIPT_DIR/gputest.py" restore --days 10
