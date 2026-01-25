#!/bin/sh
set -eu

tmux new-session -d -s runner -c "$XDG_RUNTIME_DIR/runner" \
  systemd-run --user --scope \
    -p MemoryMax=32G \
    -p MemorySwapMax=0 \
    -p TasksMax=512 \
    --unit=tmux-runner \
    --shell
# systemctl --user show tmux-runner.scope -p MemoryCurrent -p MemoryMax

tmux new-session -d -s editor -c "$HOME"
tmux send-keys -t editor "emacsclient -nw --eval '(doom/load-session \"$XDG_CONFIG_HOME/emacs/.local/etc/workspaces/projs\")'" ENTER
