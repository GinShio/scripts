#!/usr/bin/env bash
# System: Swap File

if grep -q "/swapfile" /etc/fstab; then
    echo "Swapfile already configured in /etc/fstab."
else
    echo "Setting up swapfile ($SETUP_SWAPSIZE GiB)..."
    # Using 4MiB block size
    sudo -A dd if=/dev/zero of=/swapfile bs=4MiB count=$(( 256 * SETUP_SWAPSIZE )) status=progress
    sudo -A chmod 0600 /swapfile
    sudo -A mkswap /swapfile
    sudo -A swapon /swapfile
    echo "/swapfile                                  none       swap  defaults,pri=10  0  0" | sudo -A tee -a /etc/fstab
fi
