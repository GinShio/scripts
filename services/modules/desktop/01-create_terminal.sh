#!/bin/sh
#@tags: domain:desktop, type:autostart, dep:tmux, dep:systemd
set -eu

tmux new-session -d -s runner -c "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/runner" \
  systemd-run --user --scope \
    -p MemoryMax=32G \
    -p MemorySwapMax=0 \
    -p TasksMax=512 \
    --unit=tmux-runner \
    --shell
# systemctl --user show tmux-runner.scope -p MemoryCurrent -p MemoryMax

tmux new-session -d -s editor -c "$HOME"
tmux send-keys -t editor "emacsclient -nw --eval '(doom/load-session \"${XDG_CONFIG_HOME:-$HOME/.config}/emacs/.local/etc/workspaces/projs\")'" ENTER
