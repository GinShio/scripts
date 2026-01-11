#!/usr/bin/env bash
# Apps: Cargo

if command -v cargo &>/dev/null; then
    echo "Installing Cargo packages..."
    PARAMS=(git-branchless)
    for pkg in "${PARAMS[@]}"; do
        cargo install "$pkg" --locked || echo "Warning: Failed to install $pkg"
    done
else
    echo "Warning: cargo not found. Skipping Rust tools."
fi
