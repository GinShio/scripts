#!/usr/bin/env bash
# System: Hostname

if [[ -n "$SETUP_HOSTNAME" ]] && [[ "$(hostname)" != "$SETUP_HOSTNAME" ]]; then
    echo "Setting hostname to $SETUP_HOSTNAME..."
    sudo -A hostnamectl set-hostname "$SETUP_HOSTNAME"
fi
