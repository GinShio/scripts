#!/usr/bin/env bash
# Apps: Pipx

if command -v pipx &>/dev/null; then
    echo "Installing Pipx packages..."
    # Ensure pipx path is fine
    pipx ensurepath || true

    # Core Tools: dotdrop, etc.
    PARAMS=(dotdrop iree-base-compiler[onnx] pyright trash-cli)
    for pkg in "${PARAMS[@]}"; do
        # Extract package name for check (e.g. iree-base-compiler[onnx] -> iree-base-compiler)
        # But pipx list is slow. Let's just try install, pipx skips if installed.
        pipx install "$pkg" || echo "Warning: Failed to install $pkg"
    done
else
    echo "Warning: pipx not found. Skipping."
fi
