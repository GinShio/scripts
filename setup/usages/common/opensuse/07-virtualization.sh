#!/bin/sh
#@tags: usage:common, scope:system, os:opensuse

# Virtualization & Containerization & Cross compilation
# -----------------------------------------------------------------------------
sudo -E zypper in -y -t pattern kvm_tools
sudo -E zypper in -y \
    buildah cross-{aarch64,arm,ppc64,ppc64le,riscv64,s390x}-{binutils,gcc14,linux-glibc-devel} crun crun-vm libvirt \
    libvirt-daemon-lxc libvirt-dbus libvirt-doc lxc lxd podman podman-docker qemu{,-extra,-doc,-lang} \
    qemu-{arm,ppc,x86} qemu-linux-user qemu-vhost-user-gpu
# riscv64-suse-linux-gcc -march=rv64gc riscv.c
# clang --target=riscv64-suse-linux --sysroot=/usr/riscv64-suse-linux/sys-root -mcpu=generic-rv64 -march=rv64g riscv.c
# QEMU_LD_PREFIX=/usr/riscv64-suse-linux/sys-root c.out
# QEMU_LD_PREFIX=/usr/riscv64-suse-linux/sys-root QEMU_SET_ENV='LD_LIBRARY_PATH=/usr/riscv64-suse-linux/sys-root/lib64:/usr/lib64/gcc/riscv64-suse-linux/14' cc.out
