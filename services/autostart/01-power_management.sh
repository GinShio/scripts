#!/bin/sh
set -eu

# Check if this is a laptop by detecting battery presence
is_laptop() {
    # Method 1: Check for battery in /sys/class/power_supply/
    for bat in /sys/class/power_supply/BAT* /sys/class/power_supply/battery; do
        if [ -e "$bat/type" ] && grep -qi "battery" "$bat/type" 2>/dev/null; then
            return 0
        fi
    done

    # Method 2: Check DMI chassis type (portable/laptop types: 8, 9, 10, 14)
    if [ -r /sys/class/dmi/id/chassis_type ]; then
        chassis_type=$(cat /sys/class/dmi/id/chassis_type 2>/dev/null)
        case "$chassis_type" in
            8|9|10|14) return 0 ;;
        esac
    fi

    return 1
}

# Only configure power profiles on laptops
if is_laptop; then
    powerprofilesctl configure-action --enable amdgpu_dpm || true
    powerprofilesctl configure-action --enable amdgpu_panel_power || true
fi
