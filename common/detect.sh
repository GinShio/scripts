#!/bin/sh
# Hardware Detection Logic (POSIX Compliant)
# Provides: detect_arch, detect_cpu_vendor, detect_gpu_vendor, detect_platform

get_os() {
    uname -s | tr '[:upper:]' '[:lower:]'
}

detect_arch() {
    # Normalize architecture names
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
            # x86 Check
            if grep -q "GenuineIntel" /proc/cpuinfo; then
                _cpu_vendor="intel"
            elif grep -q "AuthenticAMD" /proc/cpuinfo; then
                _cpu_vendor="amd"

            # RISC-V Check
            elif grep -q "SiFive" /proc/cpuinfo; then
                _cpu_vendor="sifive"
            elif grep -q "T-Head" /proc/cpuinfo; then
                _cpu_vendor="thead"
            elif grep -q "StarFive" /proc/cpuinfo; then
                _cpu_vendor="starfive"

            # ARM Check (CPU implementer)
            else
                # Extract implementer code (e.g., 0x41) from first processor
                _impl=$(grep -i "^CPU implementer" /proc/cpuinfo | head -n 1 | cut -d: -f2 | tr -d ' \t' | tr '[:upper:]' '[:lower:]')
                case "$_impl" in
                    0x41) _cpu_vendor="arm" ;;      # ARM Limited
                    0x42) _cpu_vendor="broadcom" ;; # Broadcom
                    0x43) _cpu_vendor="cavium" ;;   # Cavium (Marvell)
                    0x48) _cpu_vendor="hisilicon" ;;# HiSilicon
                    0x4e) _cpu_vendor="nvidia" ;;   # NVIDIA (Tegra/Grace)
                    0x50) _cpu_vendor="ampere" ;;   # Ampere
                    0x51) _cpu_vendor="qualcomm" ;; # Qualcomm
                    0x53) _cpu_vendor="samsung" ;;  # Samsung
                    0x61) _cpu_vendor="apple" ;;    # Apple (M1/M2/etc under Linux)
                    *)
                        # Fallback to model name detection for Raspberry Pi standard kernel
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
        # 1. Sysfs DRM (Preferred)
        if [ -d "/sys/class/drm" ]; then
            for _card in /sys/class/drm/card*; do
                [ -e "$_card/device/driver" ] || continue

                # Assume readlink exists on Linux
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
                     # Append if not present (sub-string check)
                     case "$_vendors" in
                        *"$_v"*) ;;
                        *) _vendors="$_vendors $_v" ;;
                     esac
                fi
            done
        fi

        # 2. LSPCI Fallback
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

    # Return space separated unique list, trimmed
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
