#!/bin/sh
set -eu

powerprofilesctl configure-action --enable amdgpu_dpm
powerprofilesctl configure-action --enable amdgpu_panel_power
