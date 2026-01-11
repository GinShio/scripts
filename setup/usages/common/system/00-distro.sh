#!/usr/bin/env bash
# System: Distro Specific Setup

. "$SCRIPT_DIR/common/detect.sh"

GPU_VENDOR=$(detect_gpu_vendor)

distro_shell_prefix=""
case "$DISTRO_ID" in
    debian)
        distro_shell_prefix="debian"
        ;;
    opensuse*)
        distro_shell_prefix="opensuse"
        ;;
    *)
        echo "Unknown or unsupported Distro: $DISTRO_ID ($DISTRO_NAME)"
        exit 1
        ;;
esac
sudo -AE bash "$SCRIPT_DIR/setup/install/${distro_shell_prefix}-${SETUP_USAGE}.sh"

IFS=' ' read -r -a GPU_VENDORS <<< "$GPU_VENDOR"
for vendor in "${GPU_VENDORS[@]}"; do
    echo ":: Configuring drivers for: $vendor"
    case "$vendor" in
        amd)
            sudo -AE bash "$SCRIPT_DIR/setup/install/${distro_shell_prefix}-amdgpu.sh"
            ;;
        *)
            echo "Info: No specific driver setup for GPU vendor: $vendor"
            ;;
    esac
done
