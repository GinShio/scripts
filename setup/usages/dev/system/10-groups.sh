#!/usr/bin/env bash
# System: User Groups

echo "Adding user to groups..."
sudo -AE usermod -aG kvm,libvirt,render,video "$(whoami)" || echo "Warning: Failed to add user to some groups."
