#!/bin/sh

# shellcheck disable=SC1091
. "$XDG_CONFIG_HOME/workflow/.env"
now_timestamps=${1:-${NIGHTLY_TIMESTAMP:-$(date +%s)}}

# Testing every 3 days
if [ "$(date +%j | awk '{print $1 % 3}')" -ne 0 ]; then
    exit 0
fi

get_power_AC() {
    power_path=$(upower -d | awk '/power_AC/ {print $NF}' | head -n 1)
    if [ -n "$power_path" ]; then
        online=$(upower -i "$power_path" | awk '/online:/ {print $NF}' | head -n 1)
    else
        online=""
    fi
    if [ -z "$online" ] || [ "$online" = "yes" ]; then
        return 0
    else
        return 1
    fi
}
get_power_AC || exit 0

python3 "$PROJECTS_SCRIPT_DIR/gputest.py" cleanup

drivers_tuple="radv,vk amdvlk,vk"

check_driver() {
    output=$(python3 "$PROJECTS_SCRIPT_DIR/gputest.py" list driver "$driver")
    old_ifs="$IFS"
    newline="
"
    IFS="$newline"
    set -f
    found=0
    for info in $output; do
        clean_info=$(echo "$info" | tr -d '[:space:]')
        item=${clean_info%%:*}
        value=${clean_info#*:}
        if [ "$item" = "Library" ] && [ -e "$value" ]; then
            file_ts=$(stat -c "%Y" "$value" 2>/dev/null)
            if [ -n "$file_ts" ] && [ "$now_timestamps" -lt "$file_ts" ]; then
                found=1
                break
            fi
        fi
    done
    set +f
    IFS="$old_ifs"
    if [ "$found" -eq 1 ]; then
        return 0
    else
        return 1
    fi
}

for elem in $drivers_tuple; do
    driver=${elem%,*}
    suite=${elem#*,}
    check_driver || continue
    tmux send-keys -t runner "python3 $PROJECTS_SCRIPT_DIR/gputest.py run $driver-$suite" ENTER
done
