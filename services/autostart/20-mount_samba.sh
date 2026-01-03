#!/usr/bin/env bash

source $XDG_CONFIG_HOME/workflow/.env
trap "sudo -k" EXIT

sudo -A -- mount --all --fstab $HOME/Public/.config.d/$DOTFILES_CURRENT_PROFILE.imm.fstab
# sudo -A -- bash -c "nohup mount --all --fstab $HOME/Public/.config.d/$DOTFILES_CURRENT_PROFILE.nohup.fstab &"
