#!/usr/bin/env bash
# update source
# -----------------------------------------------------------------------------
sudo -E apt install apt-transport-https ca-certificates
. /etc/os-release
cat <<-EOF |tee /etc/apt/sources.list
deb https://mirrors.shanghaitech.edu.cn/debian/ ${VERSION_CODENAME} main contrib non-free non-free-firmware
# deb-src https://mirrors.shanghaitech.edu.cn/debian/ ${VERSION_CODENAME} main contrib non-free non-free-firmware

deb https://mirrors.shanghaitech.edu.cn/debian/ ${VERSION_CODENAME}-updates main contrib non-free non-free-firmware
# deb-src https://mirrors.shanghaitech.edu.cn/debian/ ${VERSION_CODENAME}-updates main contrib non-free non-free-firmware

deb https://mirrors.shanghaitech.edu.cn/debian/ ${VERSION_CODENAME}-backports main contrib non-free non-free-firmware
# deb-src https://mirrors.shanghaitech.edu.cn/debian/ ${VERSION_CODENAME}-backports main contrib non-free non-free-firmware

deb https://mirrors.shanghaitech.edu.cn/debian-security ${VERSION_CODENAME}-security main contrib non-free non-free-firmware
# deb-src https://mirrors.shanghaitech.edu.cn/debian-security ${VERSION_CODENAME}-security main contrib non-free non-free-firmware
EOF

sudo dpkg --add-architecture i386 && sudo -E apt update && sudo -E apt dist-upgrade -y

# Cleanups
sudo apt purge akonadi-server mariadb-common mariadb-server mariadb-client redis || true
sudo apt-mark hold akonadiconsole

# Common environment
# -----------------------------------------------------------------------------
# Core Utilities & Shell
sudo -E apt install -y \
    aspell bat dash fd-find figlet fish flatpak fzf git{,-doc,-lfs} moreutils neofetch ripgrep tmux zstd

# Archives & Compression
sudo -E apt install -y \
    7zip iputils-ping lz4 unzip zip

# Network & Web
sudo -E apt install -y \
    cifs-utils curl firefox-esr privoxy proxychains4 qbittorrent sshpass telegram-desktop thunderbird wget

# Development Tools & Editors
sudo -E apt install -y \
    bison dwarves emacs flex graphviz imagemagick-6-common inkscape re2c software-properties-common sqlite3 xmlto \
    xsltproc

# Multimedia & Gaming
sudo -E apt install -y \
    mpv obs-studio osdlyrics steam-{installer,libs}

# kDE environment
# -----------------------------------------------------------------------------
sudo -E apt install -y \
    fcitx5{,-rime} filelight freerdp3-wayland kdeconnect krdc krfb partitionmanager

# C++ environment
# -----------------------------------------------------------------------------
# Core Toolchain
sudo -E apt install -y \
    binutils build-essential ccache cmake gcovr gdb lcov meson mold ninja-build

# Compilers (GCC & Clang)
sudo -E apt install -y \
    clang{,d,-format,-tidy,-tools} gcc g++ g++-multilib libclang-dev lld lldb llvm{,-dev}

# Libraries - Core
sudo -E apt install -y \
    libboost1.81-all-dev libc++{,abi}-dev libcaca-dev{,:i386} libcli11-dev libelf-dev{,:i386} libexpat1-dev{,:i386} \
    libssl-dev{,:i386} libunwind-14-dev{,:i386} libxml2-dev{,:i386} libzip-dev{,:i386} libzstd-dev{,:i386}

# Libraries - Other
sudo -E apt install -y \
    doxygen extra-cmake-modules libnanomsg-dev libncurses5-dev libpciaccess-dev{,:i386} libpoco-dev \
    libreadline-dev{,:i386} libspdlog-dev{,:i386} libstb-dev libtinyobjloader-dev libudev-dev{,:i386}

# Programmings environment (Other)
# -----------------------------------------------------------------------------
# Rust
sudo -E apt install -y \
    cargo librust-bindgen-dev rust-all

# Java
sudo -E apt install -y \
    openjdk-17-{jdk,jre}

# Node.js
sudo -E apt install -y \
    node-builtins node-util nodejs yarnpkg

# Functional (Erlang/Elixir/Haskell)
sudo -E apt install -y \
    elixir erlang ghc ghc-doc ghc-prof

# Other (Lua, Zig)
sudo -E apt install -y \
    liblua5.4-dev lua5.4 zig

# TeX environment
# -----------------------------------------------------------------------------
sudo -E apt install -y texlive-full

# Emacs
# -----------------------------------------------------------------------------
sudo -E apt install -y emacs-gtk emacs-nox libtool-bin libvterm-dev

# Python3 environment
# -----------------------------------------------------------------------------
# Core & Build
sudo -E apt install -y \
    pipx pylint python3 python3-dev python3-distutils-extra python3-doc python3-packaging python3-setuptools \
    python3-virtualenv

# AI / ML / Math
sudo -E apt install -y \
    libonnx-dev python3-numpy python3-numpy-dev python3-onnx python3-sympy

# Libraries & Util
sudo -E apt install -y \
    pybind11-dev python3-astunparse python3-cryptography python3-filelock python3-fsspec python3-jinja2 \
    python3-lit python3-lxml python3-lz4 python3-mako python3-psutil python3-pybind11 python3-pyelftools \
    python3-pytest python3-requests python3-ruamel.yaml python3-sphinx python3-typing-extensions python3-u-msgpack \
    python3-yaml

# Graphics
# -----------------------------------------------------------------------------
# X11 / Xorg development
sudo -E apt install -y \
    libx11-dev{,:i386} libx11-xcb-dev{,:i386} libxcb-dri2-0-dev{,:i386} libxcb-dri3-dev{,:i386} \
    libxcb-glx0-dev{,:i386} libxcb-present-dev{,:i386} libxcb-shm0-dev{,:i386} libxcomposite-dev{,:i386} \
    libxcursor-dev{,:i386} libxdamage-dev{,:i386} libxext-dev{,:i386} libxfixes-dev{,:i386} libxi-dev{,:i386} \
    libxinerama-dev{,:i386} libxkbcommon-dev{,:i386} libxrandr-dev{,:i386} libxrender-dev{,:i386} \
    libxshmfence-dev{,:i386} libxxf86vm-dev{,:i386} x11proto-dev x11proto-gl-dev xorg-dev xserver-xorg-dev \
    xutils-dev

# Wayland development
sudo -E apt install -y \
    libglfw3-{dev,wayland} libwayland-dev{,:i386} libwayland-egl-backend-dev wayland-protocols waylandpp-dev

# Mesa / OpenGL / OpenCL / SPIR-V
sudo -E apt install -y \
    freeglut3-dev{,:i386} glslang-{dev,tools} libcairo2-dev{,:i386} libdmx-dev libdrm-dev{,:i386} \
    libegl1-mesa-dev{,:i386} libfontenc-dev{,:i386} libgl1-mesa-dev{,:i386} libglm-dev libglvnd-dev{,:i386} \
    libllvmspirvlib-$(llvm-config --version |awk -F. '{print $1}')-dev libsdl2-dev{,:i386} libslang2-dev{,:i386} \
    libva-dev{,:i386} libvdpau-dev{,:i386} libvulkan-dev libwaffle-dev{,:i386} mesa-common-dev{,:i386} mesa-utils \
    piglit slsh spirv-{cross,tools} vulkan-tools vulkan-validationlayers-dev

# Virtualization & Containerization & Cross compilation
# -----------------------------------------------------------------------------
sudo -E apt install -y \
    buildah cross-gcc-dev crossbuild-essential-{arm64,armhf,ppc64el,s390x} lxc lxd podman podman-docker

# QEMU / KVM / Libvirt
sudo -E apt install -y \
    libvirt-clients-qemu libvirt-daemon-driver-lxc libvirt-daemon-driver-qemu libvirt-daemon-system \
    libvirt-{clients,daemon,dbus,doc} qemu-efi{,-aarch64,-arm} qemu-system-{,arm,common,data,ppc,x86} \
    qemu-{kvm,system,user,utils} virt-manager

# Font
# -----------------------------------------------------------------------------
sudo -E apt install -y fonts-wqy-{microhei,zenhei}
mkdir -p "$HOME/.local/share/fonts/Symbols-Nerd"
cd "$(mktemp -d)"
curl -o NerdSymbol.tar.xz -sSL https://github.com/ryanoasis/nerd-fonts/releases/latest/NerdFontsSymbolsOnly.tar.xz
tar -xzf NerdSymbol.tar.xz -C "$HOME/.local/share/fonts/Symbols-Nerd"
