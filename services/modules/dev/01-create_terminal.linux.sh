#!/bin/sh
#@tags: domain:dev, type:autostart, os:linux, dep:tmux, dep:systemd-run
set -eu

. "$PROJECTS_SCRIPT_DIR/common/detect.sh"
mem_total=$(detect_memory_mb)

_work_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/runner"
mkdir -p "$_work_dir"

tmux new-session -d -s runner -c "$_work_dir" \
    systemd-run --user --scope \
    -p MemoryMax=$(echo "m=$mem_total/1024*0.75; if(m>4) m else 4" | bc)G \
    -p MemorySwapMax=0 \
    -p TasksMax=512 \
    -p OOMPolicy=continue \
    --unit=tmux-runner \
    --shell
# systemctl --user show tmux-runner.scope -p MemoryCurrent -p MemoryMax

tmux new-session -d -s editor -c "$HOME"
tmux send-keys -t editor "emacsclient -nw --eval '(doom/load-session \"${XDG_CONFIG_HOME:-$HOME/.config}/emacs/.local/etc/workspaces/projs\")'" ENTER
