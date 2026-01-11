#!/usr/bin/env bash
# Services: User Services

echo "Enabling User Services..."
USER_SERVICES=(nightly-script.timer emacs.service develop-autostart.service podman.service)

for svc in "${USER_SERVICES[@]}"; do
    # Check user unit existence roughly or just try enable
    systemctl --user enable --now "$svc" || echo "Note: User service $svc could not be enabled (missing?)"
done
