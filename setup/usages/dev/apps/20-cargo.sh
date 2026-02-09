#!/usr/bin/env bash
# Apps: Cargo

if command -v cargo &>/dev/null; then
    echo "Installing Cargo packages..."
    packages=(
        git-branchless
    )
    for pkg in "${packages[@]}"; do
        cargo install "$pkg" --locked || echo "Warning: Failed to install $pkg in cargo"
    done
else
    echo "Warning: cargo not found. Skipping Rust tools."
fi
