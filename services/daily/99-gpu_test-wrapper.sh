#!/usr/bin/env bash

source $XDG_CONFIG_HOME/workflow/.env
now_timestamps=${1:-${NIGHTLY_TIMESTAMP:-$(date +%s)}}

# Testing every 3 days
[[ 0 -eq $(( 10#$(date +%j) % 3 )) ]] || exit 0

get_power_AC() {
    local online=$(upower -d |awk '/power_AC/ {print $NF}' |xargs -I@ upower -i @ |awk '/online:/ {print $NF}')
    [[ -z "$online" || "yes" = "$online" ]] && return 0 || return 1
}
$(get_power_AC) || exit 0

python3 $PROJECTS_SCRIPT_DIR/gputest.py cleanup

drivers_tuple=(
    radv,vk
    amdvlk,vk
) # drivers tuple declare end

check_driver() {
    IFS=$'\n'; local driver_info=($(python3 $PROJECTS_SCRIPT_DIR/gputest.py list driver $driver)); IFS="$old_ifs"
    for info in ${driver_info[@]}; do
        while IFS=: read -r item value; do
            if [[ "$item" = "Library" ]] && [[ -e "$value" ]] && [[ $now_timestamps -lt $(stat -c "%Y" "$value") ]]; then
                return 0
            fi
        done <<<"$(tr -d '[:space:]' <<<"$info")"
    done
    return 1
}

for elem in ${drivers_tuple[@]}; do
    IFS=',' read driver suite <<< "${elem}"
    check_driver || continue
    tmux send-keys -t runner "python3 $PROJECTS_SCRIPT_DIR/gputest.py run $driver-$suite" ENTER
done
