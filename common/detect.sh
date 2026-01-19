#!/bin/sh
# System Detection Logic

get_os() {
    uname -s | tr '[:upper:]' '[:lower:]'
}

detect_arch() {
    _arch=$(uname -m | tr '[:upper:]' '[:lower:]')
    case "$_arch" in
        x86_64|amd64) echo "x86_64" ;;
        i*86) echo "x86" ;;
        aarch64|arm64) echo "arm64" ;;
        arm*) echo "arm" ;;
        riscv64) echo "riscv64" ;;
        *) echo "$_arch" ;;
    esac
}

detect_cpu_vendor() {
    _os=$(get_os)
    _cpu_vendor="unknown"

    if [ "$_os" = "linux" ]; then
        if [ -r /proc/cpuinfo ]; then
            if grep -q "GenuineIntel" /proc/cpuinfo; then
                _cpu_vendor="intel"
            elif grep -q "AuthenticAMD" /proc/cpuinfo; then
                _cpu_vendor="amd"
            elif grep -q "SiFive" /proc/cpuinfo; then
                _cpu_vendor="sifive"
            elif grep -q "T-Head" /proc/cpuinfo; then
                _cpu_vendor="thead"
            elif grep -q "StarFive" /proc/cpuinfo; then
                _cpu_vendor="starfive"
            else
                _impl=$(grep -i "^CPU implementer" /proc/cpuinfo | head -n 1 | cut -d: -f2 | tr -d ' \t' | tr '[:upper:]' '[:lower:]')
                case "$_impl" in
                    0x41) _cpu_vendor="arm" ;;
                    0x42) _cpu_vendor="broadcom" ;;
                    0x43) _cpu_vendor="cavium" ;;
                    0x48) _cpu_vendor="hisilicon" ;;
                    0x4e) _cpu_vendor="nvidia" ;;
                    0x50) _cpu_vendor="ampere" ;;
                    0x51) _cpu_vendor="qualcomm" ;;
                    0x53) _cpu_vendor="samsung" ;;
                    0x61) _cpu_vendor="apple" ;;
                    *)
                        if grep -q "BCM" /proc/cpuinfo; then
                            _cpu_vendor="broadcom"
                        fi
                        ;;
                esac
            fi
        fi
    elif [ "$_os" = "darwin" ]; then
        _brand=$(sysctl -n machdep.cpu.brand_string 2>/dev/null | tr '[:upper:]' '[:lower:]')
        if echo "$_brand" | grep -q "intel"; then _cpu_vendor="intel"; fi
        if echo "$_brand" | grep -q "apple"; then _cpu_vendor="apple"; fi
    elif [ "$_os" = "freebsd" ] || [ "$_os" = "openbsd" ]; then
         _brand=$(sysctl -n hw.model 2>/dev/null | tr '[:upper:]' '[:lower:]')
         if echo "$_brand" | grep -q "intel"; then _cpu_vendor="intel"; fi
         if echo "$_brand" | grep -q "amd"; then _cpu_vendor="amd"; fi
    fi
    echo "$_cpu_vendor"
}

detect_gpu_vendor() {
    _vendors=""
    _os=$(get_os)

    if [ "$_os" = "linux" ]; then
        if [ -d "/sys/class/drm" ]; then
            for _card in /sys/class/drm/card*; do
                [ -e "$_card/device/driver" ] || continue
                if command -v readlink >/dev/null 2>&1; then
                    _driver_path=$(readlink -f "$_card/device/driver")
                    _driver=$(basename "$_driver_path")
                else
                    continue
                fi

                case "$_driver" in
                    amdgpu|radeon) _v="amd" ;;
                    i915|xe|iris)  _v="intel" ;;
                    nvidia|nvidia-drm) _v="nvidia" ;;
                    vc4|v3d)       _v="videocore" ;;
                    panfrost|mali) _v="mali" ;;
                    tegra*|nv*)    _v="nvidia" ;;
                    virtio_gpu)    _v="virtio" ;;
                    *)             _v="" ;;
                esac

                if [ -n "$_v" ]; then
                     case "$_vendors" in
                        *"$_v"*) ;;
                        *) _vendors="$_vendors $_v" ;;
                     esac
                fi
            done
        fi

        if [ -z "$_vendors" ] && command -v lspci >/dev/null 2>&1; then
            _pci=$(lspci -mm 2>/dev/null)
            if echo "$_pci" | grep -iE "VGA|3D|Display" | grep -iq "NVIDIA"; then
                 case "$_vendors" in *"nvidia"*) ;; *) _vendors="$_vendors nvidia" ;; esac
            fi
            if echo "$_pci" | grep -iE "VGA|3D|Display" | grep -iqE "AMD|ATI"; then
                 case "$_vendors" in *"amd"*) ;; *) _vendors="$_vendors amd" ;; esac
            fi
            if echo "$_pci" | grep -iE "VGA|3D|Display" | grep -iq "Intel"; then
                 case "$_vendors" in *"intel"*) ;; *) _vendors="$_vendors intel" ;; esac
            fi
        fi

    elif [ "$_os" = "darwin" ]; then
        _profile=$(system_profiler SPDisplaysDataType 2>/dev/null)
        if echo "$_profile" | grep -iq "NVIDIA"; then _vendors="$_vendors nvidia"; fi
        if echo "$_profile" | grep -iq "AMD"; then _vendors="$_vendors amd"; fi
        if echo "$_profile" | grep -iq "Intel"; then _vendors="$_vendors intel"; fi
        if echo "$_profile" | grep -iq "Apple"; then _vendors="$_vendors apple"; fi
    fi
    echo "$_vendors" | awk '{$1=$1};1'
}

detect_platform() {
    _platform="generic"
    if [ -f /proc/device-tree/model ]; then
        _platform=$(tr -d '\0' < /proc/device-tree/model)
    elif [ -f /sys/firmware/devicetree/base/model ]; then
        _platform=$(tr -d '\0' < /sys/firmware/devicetree/base/model)
    elif [ -f /sys/class/dmi/id/product_name ]; then
        _platform=$(cat /sys/class/dmi/id/product_name)
    elif [ "$(uname -s)" = "Darwin" ]; then
         _platform=$(sysctl -n hw.model)
    fi
    echo "$_platform"
}

detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$ID"
    else
        echo "unknown"
    fi
}

detect_distro_version() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$VERSION_ID"
    else
        echo "unknown"
    fi
}

detect_kernel() {
    uname -r
}

detect_kernel_major() {
    uname -r | cut -d. -f1
}

detect_kernel_minor() {
    uname -r | cut -d. -f2
}

detect_kernel_patch() {
    # Handles cases like 6.6.10-arch1-1 -> 10
    uname -r | cut -d. -f3 | cut -d- -f1
}

detect_cmdline() {
    if [ -f /proc/cmdline ]; then
        cat /proc/cmdline
    else
        echo ""
    fi
}

has_cmdline() {
    if [ -f /proc/cmdline ]; then
        if grep -q "$1" /proc/cmdline; then
            echo "true"
        else
            echo "false"
        fi
    else
        echo "false"
    fi
}

detect_memory_mb() {
    _os=$(get_os)
    if [ "$_os" = "linux" ]; then
        if [ -f /proc/meminfo ]; then
            awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo
        fi
    elif [ "$_os" = "darwin" ]; then
        _mem=$(sysctl -n hw.memsize 2>/dev/null)
        echo $(( _mem / 1024 / 1024 ))
    elif [ "$_os" = "freebsd" ] || [ "$_os" = "openbsd" ]; then
        _mem=$(sysctl -n hw.physmem 2>/dev/null)
        echo $(( _mem / 1024 / 1024 ))
    else
        echo "0"
    fi
}

detect_hostname() {
    uname -n | tr '[:upper:]' '[:lower:]'
}

detect_desktop() {
    # Check common environment variables
    _de="${XDG_CURRENT_DESKTOP:-${DESKTOP_SESSION}}"
    if [ -z "$_de" ]; then
        # Try to infer from processes if env vars are missing (common in some setups)
        if pgrep -x "kwin_wayland" >/dev/null || pgrep -x "kwin_x11" >/dev/null; then
            echo "kde"
        elif pgrep -x "gnome-shell" >/dev/null; then
             echo "gnome"
        elif pgrep -x "sway" >/dev/null; then
             echo "sway"
        elif pgrep -x "hyprland" >/dev/null; then
             echo "hyprland"
        else
             echo "headless"
        fi
    else
        echo "$_de" | tr '[:upper:]' '[:lower:]'
    fi
}

detect_display_server() {
    _ds="unknown"
    if [ -n "$WAYLAND_DISPLAY" ]; then
        _ds="wayland"
    elif [ -n "$DISPLAY" ]; then
        # Check if it's Xwayland
        if loginctl show-session $(loginctl | grep $(whoami) | awk '{print $1}') -p Type | grep -q "wayland"; then
             _ds="wayland" # Or "xwayland" if we want to be specific, but usually context is "is it wayland or pure X11"
        else
             _ds="x11"
        fi
    fi
     # Fallback for some non-systemd or weird envs
    if [ "$_ds" = "unknown" ]; then
         if pgrep -x "Xorg" >/dev/null || pgrep -x "X" >/dev/null; then
             _ds="x11"
         fi
    fi
    echo "$_ds"
}

# Dispatcher
# Provide execution if script is run directly, allow sourcing as library
if [ "$#" -gt 0 ]; then
    case "$1" in
        arch) detect_arch ;;
        cpu) detect_cpu_vendor ;;
        gpu) detect_gpu_vendor ;;
        mem|memory_mb) detect_memory_mb ;;
        host|hostname) detect_hostname ;;
        de|desktop) detect_desktop ;;
        ds|display_server) detect_display_server ;;
        platform) detect_platform ;;
        distro) detect_distro ;;
        distro_ver) detect_distro_version ;;
        kernel) detect_kernel ;;
        kernel_major) detect_kernel_major ;;
        kernel_minor) detect_kernel_minor ;;
        kernel_patch) detect_kernel_patch ;;
        cmdline) detect_cmdline ;;
        has_cmdline) has_cmdline "$2" ;;
        *) echo "Usage: $0 {arch|cpu|gpu|platform|distro|distro_ver|kernel|cmdline|has_cmdline <arg>}" ;;
    esac
fi
