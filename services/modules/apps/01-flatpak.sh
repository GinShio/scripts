#!/bin/sh
#@tags: domain:user, type:nightly, dep:flatpak
set -eu

echo "Updating Flatpak packages..."
flatpak update -y
