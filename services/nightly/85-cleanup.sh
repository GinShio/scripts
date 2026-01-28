#!/bin/sh
# System Cleanup & Hygiene
# POSIX-compliant script for nightly maintenance

set -u

# shellcheck disable=SC1091
. "$XDG_CONFIG_HOME/workflow/.env"
# shellcheck disable=SC1091
. "$PROJECTS_SCRIPT_DIR/common/detect.sh"

# 1. Container Engines (Docker/Podman)
# -----------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
        docker system prune -f --volumes >/dev/null 2>&1
    elif sudo -n true 2>/dev/null; then
        sudo -A -- docker system prune -f --volumes >/dev/null 2>&1
    fi
fi

if command -v podman >/dev/null 2>&1; then
    if podman info >/dev/null 2>&1; then
        podman system prune -f --volumes >/dev/null 2>&1
    fi
fi

# 2. System Logs & Temporary Files
# -----------------------------------------------------------------------------
_os=$(get_os)

# Systemd Journal (Linux)
if command -v journalctl >/dev/null 2>&1; then
    sudo -A -- journalctl --vacuum-time=2weeks >/dev/null 2>&1
fi

# Traditional /var/log (Linux & FreeBSD)
# Cleans old rotated/compressed logs older than 30 days.
# Safe extensions only to avoid deleting active logs.
if [ -d "/var/log" ]; then
    sudo -A -- find /var/log -type f \( \
        -name "*.gz" -o \
        -name "*.xz" -o \
        -name "*.bz2" -o \
        -name "*.Z" -o \
        -name "*.zip" -o \
        -name "*.old" \
    \) -mtime +30 -delete 2>/dev/null || true
fi

# /tmp Cleaning
# Safety: We ONLY delete regular files (-type f) accessed > 10 days ago.
# We NEVER touch sockets (-type s), pipes, or directories.
# We NEVER touch $XDG_RUNTIME_DIR (/run/user/UID).
if command -v systemd-tmpfiles >/dev/null 2>&1; then
    # Systemd handles this best via tmpfiles.d
    sudo -A -- systemd-tmpfiles --clean >/dev/null 2>&1
else
    # Fallback for non-systemd Linux & FreeBSD
    find /tmp -depth -type f -atime +10 -delete 2>/dev/null || true
    
    # Also clean /var/tmp which is often persistent
    find /var/tmp -depth -type f -atime +30 -delete 2>/dev/null || true
fi

# User Logs
# ~/.xsession-errors can grow indefinitely on some X11 setups
if [ -f "$HOME/.xsession-errors" ]; then
    # Truncate if larger than 50MB
    _size=$(du -m "$HOME/.xsession-errors" | cut -f1)
    if [ "$_size" -gt 50 ]; then
        : > "$HOME/.xsession-errors"
    fi
fi

# macOS Logs
if [ "$_os" = "darwin" ]; then
    sudo -A -- rm -rf /private/var/log/asl/*.asl 2>/dev/null || true
    find "$HOME/Library/Logs" -type f -mtime +14 -delete 2>/dev/null || true
fi

# 3. User Cache & Trash (XDG Compliant)
# -----------------------------------------------------------------------------
_cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}"
_data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"

# Generic Cache (60 days retention)
if [ -d "$_cache_dir" ]; then
    find "$_cache_dir" -depth -type f -atime +60 -delete 2>/dev/null || true
fi

# Thumbnails
if [ -d "$_cache_dir/thumbnails" ]; then
    find "$_cache_dir/thumbnails" -type f -atime +14 -delete 2>/dev/null || true
fi

# Trash Bin (30 days retention)
if [ -d "$_data_dir/Trash/files" ]; then
    find "$_data_dir/Trash/files" -depth -type f -atime +30 -delete 2>/dev/null || true
    find "$_data_dir/Trash/info" -depth -type f -atime +30 -delete 2>/dev/null || true
fi

# Language Specific Caches
if command -v go >/dev/null 2>&1; then
    go clean -modcache >/dev/null 2>&1 || true
    go clean -cache >/dev/null 2>&1 || true
fi
if command -v pip >/dev/null 2>&1; then
    pip cache purge >/dev/null 2>&1 || true
fi
if command -v npm >/dev/null 2>&1; then
    npm cache clean --force >/dev/null 2>&1 || true
fi
if command -v yarn >/dev/null 2>&1; then
    yarn cache clean >/dev/null 2>&1 || true
fi
if command -v cargo >/dev/null 2>&1; then
    : # Skip cargo to avoid expensive registry re-downloads
fi

# 4. Package Manager (Cache & Orphans)
# -----------------------------------------------------------------------------
if [ "$_os" = "linux" ]; then
    _distro=$(detect_distro)
    case "$_distro" in
        opensuse*|suse)
            sudo -A -- zypper clean --all >/dev/null 2>&1
            ;;

        arch|manjaro|endeavouros)
            # 1. Cache
            if command -v paccache >/dev/null 2>&1; then
                sudo -A -- paccache -rk2 >/dev/null 2>&1
                sudo -A -- paccache -ruk0 >/dev/null 2>&1
            else
                sudo -A -- pacman -Sc --noconfirm >/dev/null 2>&1
            fi

            # 2. Orphans (Recursive)
            _orphans=$(pacman -Qtdq 2>/dev/null || true)
            if [ -n "$_orphans" ]; then
                # shellcheck disable=SC2086
                sudo -A -- pacman -Rns --noconfirm $_orphans >/dev/null 2>&1 || true
            fi

            # 3. AUR Cache
            if command -v yay >/dev/null 2>&1; then
                yay -Sc --noconfirm >/dev/null 2>&1 || true
                yay -Yc --noconfirm >/dev/null 2>&1 || true
            elif command -v paru >/dev/null 2>&1; then
                paru -Sc --noconfirm >/dev/null 2>&1 || true
                paru -c --noconfirm >/dev/null 2>&1 || true
            fi
            ;;

        debian|ubuntu|pop|kali|linuxmint|raspbian)
            sudo -A -- apt-get autoclean >/dev/null 2>&1
            sudo -A -- apt-get autoremove -y >/dev/null 2>&1
            ;;

        fedora|rhel|centos|almalinux|rocky)
            sudo -A -- dnf clean all >/dev/null 2>&1
            sudo -A -- dnf autoremove -y >/dev/null 2>&1
            ;;

        alpine)
            sudo -A -- apk cache clean >/dev/null 2>&1
            ;;
    esac

elif [ "$_os" = "darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
        brew cleanup -s >/dev/null 2>&1
    fi

elif [ "$_os" = "freebsd" ]; then
    sudo -A -- pkg clean -y >/dev/null 2>&1
    sudo -A -- pkg autoremove -y >/dev/null 2>&1
fi

exit 0
