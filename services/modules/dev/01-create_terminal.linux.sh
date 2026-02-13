#!/bin/sh
#@tags: domain:dev, type:autostart, os:linux, dep:tmux, dep:systemd-run
set -eu

_work_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/runner"
mkdir -p "$_work_dir"

tmux new-session -d -s runner -c "$_work_dir" \
    systemd-run --user --scope \
    -p MemoryMax=32G \
    -p MemorySwapMax=0 \
    -p TasksMax=512 \
    --unit=tmux-runner \
    --shell
# systemctl --user show tmux-runner.scope -p MemoryCurrent -p MemoryMax

tmux new-session -d -s editor -c "$HOME"
tmux send-keys -t editor "emacsclient -nw --eval '(doom/load-session \"${XDG_CONFIG_HOME:-$HOME/.config}/emacs/.local/etc/workspaces/projs\")'" ENTER
