#!/bin/sh
set -eu

# shellcheck disable=SC1091
. "$XDG_CONFIG_HOME/workflow/.env"
# shellcheck disable=SC1091
. "$PROJECTS_SCRIPT_DIR/common/detect.sh"
# shellcheck disable=SC1091
. "$PROJECTS_SCRIPT_DIR/common/proxy.sh"

cleanup() {
    sudo -k
    # shellcheck disable=SC1091
    . "$PROJECTS_SCRIPT_DIR/common/unproxy.sh"
}
trap cleanup EXIT


_os=$(get_os)
if [ "$_os" = "linux" ]; then
    _distro=$(detect_distro)

    case "$_distro" in
        opensuse*|suse)
            sudo -AE -- zypper ref
            # Update specific openSUSE repositories if they exist
            zypper lr | awk 'NR > 4 && $3~/openSUSE:/ {print $3}' | xargs -r -I@ sudo -AE -- zypper up -y --repo @
            sudo -A -- zypper up -y --allow-vendor-change
            sudo -A -- zypper dup -y --allow-vendor-change
            ;;
        
        arch|manjaro|endeavouros)
            if command -v yay >/dev/null 2>&1; then
                # yay handles both repo and AUR updates
                yay -Syu --noconfirm
            elif command -v paru >/dev/null 2>&1; then
                paru -Syu --noconfirm
            fi
            sudo -A -- pacman -Syu --noconfirm
            ;;
            
        debian|ubuntu|pop|kali|linuxmint|raspbian)
            sudo -A -- apt update
            sudo -AE -- apt full-upgrade -y
            ;;
            
        fedora|rhel|centos|almalinux|rocky)
            sudo -AE -- dnf upgrade -y --refresh
            ;;
            
        alpine)
            sudo -A -- apk update
            sudo -A -- apk upgrade
            ;;

        *)
            ;;
    esac

elif [ "$_os" = "darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
        brew update
        brew upgrade
    fi

elif [ "$_os" = "freebsd" ]; then
    sudo -AE -- pkg update
    sudo -AE -- pkg upgrade -y

fi
