#!/bin/sh
#@tags: usage:common, scope:system, os:opensuse, gpu:amd

sudo zypper in -y \
    libvulkan_radeon libvulkan_radeon-32bit xf86-video-amdgpu

sudo zypper ar -fcg obs://science:GPU:ROCm openSUSE:ROCm
sudo zypper ref

sudo -E zypper in -y \
    kernel-firmware-amdgpu libdrm_amdgpu1 libdrm_amdgpu1-32bit libvulkan_radeon libvulkan_radeon-32bit \
    mojo-shader-amdgpu-pro-precompile radeontop
