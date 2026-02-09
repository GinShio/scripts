#!/usr/bin/env bash
# Apps: Flatpak

if command -v flatpak &>/dev/null; then
    echo "Installing Flatpaks..."
    packages=(
        com.discordapp.Discord
        com.visualstudio.code
    )
    # Ensure flathub repo exists (idempotent usually)
    flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo || true
    for pkg in "${packages[@]}"; do
        flatpak install -y flathub "$pkg" || echo "Warning: Failed to install $pkg in flatpak"
    done
else
    echo "Warning: Flatpak not found. Skipping."
fi
