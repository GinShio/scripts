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

    # Create a reference file with timestamp to compare against
    # Use mktemp for security (atomic creation, safe permissions)
    ref_file=$(mktemp) || {
        set +f
        IFS="$old_ifs"
        echo "Error: Failed to create temporary file" >&2
        return 1
    }

    # Use native touch command to set file timestamp
    # Convert Unix timestamp to touch format: [[CC]YY]MMDDhhmm[.SS]
    # Try GNU date first, then BSD date
    touch_time=$(date -d "@$now_timestamps" +%Y%m%d%H%M.%S 2>/dev/null || \
                 date -r "$now_timestamps" +%Y%m%d%H%M.%S 2>/dev/null || true)

    if [ -z "$touch_time" ]; then
        # If date conversion fails, restore state and return error
        rm -f "$ref_file"
        set +f
        IFS="$old_ifs"
        echo "Error: Failed to convert timestamp (date command not compatible)" >&2
        return 1
    fi

    # Set the timestamp on the already-created temporary file
    if ! touch -t "$touch_time" "$ref_file" 2>/dev/null; then
        # Restore state and clean up on error
        rm -f "$ref_file"
        set +f
        IFS="$old_ifs"
        echo "Error: Failed to set file timestamp" >&2
        return 1
    fi

    for info in $output; do
        clean_info=$(echo "$info" | tr -d '[:space:]')
        item=${clean_info%%:*}
        value=${clean_info#*:}
        if [ "$item" = "Library" ] && [ -e "$value" ]; then
            # POSIX compatible: use -nt (newer than) test operator
            # This checks if file modification time > now_timestamps
            if [ "$value" -nt "$ref_file" ]; then
                found=1
                break
            fi
        fi
    done

    # Always clean up reference file
    rm -f "$ref_file"
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

    # Run test and signal completion (even on failure)
    # Using a unique channel per iteration to avoid conflicts
    signal_name="gputest-done-$$-$driver-$suite"

    # Use Lock pattern to avoid race condition:
    # 1. Lock the channel (-L)
    # 2. Run task which unlocks (-U) when done
    # 3. Wait for lock (-L) -> blocks until task unlocks
    tmux wait-for -L "$signal_name"

    if tmux send-keys -t runner \
        "python3 $PROJECTS_SCRIPT_DIR/gputest.py run $driver-$suite; tmux wait-for -U $signal_name" ENTER; then

        # Wait for the test to complete (blocks until the pane unlocks)
        tmux wait-for -L "$signal_name"
        # Cleanup: Release the lock we just re-acquired
        tmux wait-for -U "$signal_name"
    else
        # If sending failed, release our lock
        tmux wait-for -U "$signal_name"
        check_driver_ret=1 # Mark failure if needed
    fi
done
