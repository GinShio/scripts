#!/bin/sh
#@tags: domain:desktop, type:autostart, net:samba, dep:sudo, dep:mount
set -eu

# shellcheck disable=SC1091
. "${XDG_CONFIG_HOME:-$HOME/.config}/workflow/.env"
trap "sudo -k" EXIT

sudo -A -- mount --all --fstab "$HOME/Public/.config.d/$DOTFILES_CURRENT_PROFILE.imm.fstab"
#sudo -A -- sh -c "nohup mount --all --fstab $HOME/Public/.config.d/$DOTFILES_CURRENT_PROFILE.nohup.fstab &"
