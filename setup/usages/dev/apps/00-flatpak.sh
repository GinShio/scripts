#!/usr/bin/env bash
# Apps: Flatpak

if command -v flatpak &>/dev/null; then
    echo "Installing Flatpaks..."
    # Ensure flathub repo exists (idempotent usually)
    flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo || true
    flatpak install -y flathub com.discordapp.Discord com.visualstudio.code || echo "Warning: Discord install failed"
else
    echo "Warning: Flatpak not found. Skipping."
fi
