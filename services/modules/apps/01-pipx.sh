#!/bin/sh
#@tags: domain:user, type:nightly, dep:pipx
set -eu

echo "Updating Pipx packages..."
pipx upgrade-all
