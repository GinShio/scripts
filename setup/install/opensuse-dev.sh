# update source
# -----------------------------------------------------------------------------
### zypper
sudo zypper rr --all
# NJU Mirrors
sudo zypper ar -fcg https://mirrors.nju.edu.cn/opensuse/tumbleweed/repo/oss NJU:oss
sudo zypper ar -fcg https://mirrors.nju.edu.cn/opensuse/tumbleweed/repo/non-oss NJU:non-oss
sudo zypper ar -fcg https://mirror.nju.edu.cn/packman/suse/openSUSE_Tumbleweed NJU:packman
# Extras
sudo zypper ar -fcg obs://KDE:Extra openSUSE:kDE:Extra
sudo zypper ar -fcg obs://Virtualization openSUSE:Virtualization
sudo zypper ar -fcg https://download.opensuse.org/repositories/devel:/tools:/compiler/openSUSE_Factory openSUSE:compiler
# sudo zypper ar -fcg https://download.opensuse.org/repositories/server:/messaging/openSUSE_Factory openSUSE:messaging
# sudo zypper ar -fcg https://download.opensuse.org/repositories/utilities/openSUSE_Factory openSUSE:Utilities

sudo -E zypper ref
sudo -E zypper dup -y --allow-vendor-change

# Cleanups
sudo zypper remove -u valkey mariadb mariadb-client akonadi gitk || true
sudo -E zypper al cmake-gui git-gui akonadi gitk

# Common environment
# -----------------------------------------------------------------------------
# Core Utilities & Shell
sudo -E zypper in -y \
    bat dash fd figlet fish fzf git git-delta git-doc git-lfs moreutils neowofetch osdlyrics ripgrep ShellCheck tmux \
    tree-sitter zstd

# Archives & Compression
sudo -E zypper in -y \
    7zip lz4 unzip zip

# Network & Web
sudo -E zypper in -y \
    MozillaFirefox MozillaThunderbird cifs-utils curl privoxy proxychains-ng qbittorrent sshpass wget

# Development Tools & Editors
sudo -E zypper in -y -t pattern devel_basis
sudo -E zypper in -y \
    bison chrpath dwarves flex graphviz hugo inkscape libxslt-tools pandoc-cli patchelf re2c sqlite3 xmlto

# Multimedia
sudo -E zypper in -y \
    ImageMagick mpv obs-studio

# System Utils
sudo -E zypper in -y \
    cpuinfo{,-devel}

# C++ environment
# -----------------------------------------------------------------------------
# Core Toolchain
sudo -E zypper in -y -t pattern devel_C_C++
sudo -E zypper in -y \
    ccache cmake conan doxygen gcovr gdb imake include-what-you-use kf6-extra-cmake-modules lcov meson mold ninja \
    sccache tree-sitter-c{,pp}

# Compilers (GCC & Clang)
sudo -E zypper in -y \
    clang{,-doc,-extract,-tools,-devel} gcc{,-32bit} gcc-c++{,-32bit} gcc-info \
    lld lldb llvm{,-doc,-opt-viewer,-devel}

# Libraries - Core
sudo -E zypper in -y \
    cli11-devel eigen3-{devel,doc} 'libboost_*-devel' libc++{,abi}-devel libcaca-devel libelf-devel{,-32bit} \
    libexpat-devel{,-32bit} libopenssl-devel{,-32bit} libpciaccess-devel libstdc++-devel{,-32bit} libunwind-devel \
    libxml2-devel{,-32bit} libzstd-devel{,-32bit}

# Libraries - Other
sudo -E zypper in -y \
    nanomsg-devel ncurses-devel{,-32bit} poco-devel readline-devel{,-32bit} spdlog-devel stb-devel tinyobjloader-devel \
    z3-devel zlib-ng-compat-devel

# Python3 environment
# -----------------------------------------------------------------------------
# Interpreter & Core
sudo -E zypper in -y \
    python3 python3-devel python3-doc python3-pipx python3-virtualenv tree-sitter-python

# AI / ML / Math
sudo -E zypper in -y \
    libonnx onnx-devel python3-numpy{,-devel} python3-onnx python3-sympy

# Build & Packaging
sudo -E zypper in -y \
    python3-distutils-extra python3-packaging python3-setuptools python3-u-msgpack-python

# Utilities & Libraries
sudo -E zypper in -y \
    python3-Jinja2 python3-Mako python3-PyYAML python3-Sphinx python3-astunparse python3-cryptography{,-vectors} \
    python3-filelock python3-fsspec python3-lit python3-lxml python3-lz4 python3-psutil python3-pybind11{,-devel} \
    python3-pyelftools python3-pygit2 python3-pylint python3-pytest python3-requests python3-ruamel.yaml \
    python3-typing_extensions

# Programmings environment (Other)
# -----------------------------------------------------------------------------
# Rust
sudo -E zypper in -y \
    cargo rust rust-bindgen tree-sitter-rust

# Zig
sudo -E zypper in -y \
    tree-sitter-zig zig zig-libs zls

# Java
sudo -E zypper in -y \
    java-{17,21}-openjdk{,-devel}

# Node.js
sudo -E zypper in -y \
    nodejs-common yarn

# Functional (Erlang/Elixir/Haskell)
sudo -E zypper in -y \
    elixir elixir-doc elixir-hex erlang erlang-doc ghc{,-doc,-manual,-prof} tree-sitter-haskell

# Others
sudo -E zypper in -y \
    lua lua-devel

# GPGPU
# -----------------------------------------------------------------------------
# Vulkan development pattern
sudo -E zypper in -y -t pattern devel_vulkan

# X11 / Xorg development
sudo -E zypper in -y \
    libX11-devel{,-32bit} libXau-devel{,-32bit} libXaw-devel{,-32bit} libXcomposite-devel{,-32bit} \
    libXcursor-devel{,-32bit} libXdamage-devel{,-32bit} libXdmcp-devel{,-32bit} libXext-devel{,-32bit} \
    libXfixes-devel{,-32bit} libXfont2-devel{,-32bit} libXft-devel{,-32bit} libXi-devel{,-32bit} \
    libXinerama-devel{,-32bit} libXmu-devel{,-32bit} libXpm-devel{,-32bit} libXrandr-devel{,-32bit} \
    libXrender-devel{,-32bit} libXres-devel{,-32bit} libXss-devel{,-32bit} libXt-devel{,-32bit} \
    libXtst-devel{,-32bit} libXv-devel{,-32bit} libXvMC-devel{,-32bit} libXxf86dga-devel libXxf86vm-devel{,-32bit} \
    libxkbcommon-devel{,-32bit} libxkbfile-devel{,-32bit} xcb-proto-devel 'xcb-util*-devel' \
    'xcb-util*-devel-32bit' xorg-x11-server-sdk xorgproto-devel

# Wayland development
sudo -E zypper in -y \
    libglfw3-wayland wayland-devel{,-32bit} wayland-protocols-devel waylandpp-devel

# Mesa / OpenGL / OpenCL / SPIR-V
sudo -E zypper in -y \
    clinfo glm-devel glslang-devel libclc libdmx-devel libdrm-devel{,-32bit} libvulkan_lvp \
    Mesa-demo-egl{,-32bit} Mesa-demo-es{,-32bit} Mesa-demo-x{,-32bit} Mesa-dri{,-32bit,-devel} \
    Mesa-libEGL-devel{,-32bit} Mesa-libGL-devel{,-32bit} Mesa-libRusticlOpenCL Mesa-vulkan-device-select{,-32bit} \
    Mesa-vulkan-overlay{,-32bit} ocl-icd-devel opencl-headers piglit shaderc spirv-{cross,tools} \
    spirv-tools-devel{,-32bit} tree-sitter-{glsl,hlsl} vulkan-{tools,devel}{,-32bit} \
    vulkan-{utility-libraries,volk}-devel

# Graphics Utils
sudo -E zypper in -y \
    SDL2-devel{,-32bit} cairo-devel{,-32bit} freeglut-devel{,-32bit} libICE-devel{,-32bit} \
    libLLVMSPIRVLib-devel libSM-devel{,-32bit} libfontenc-devel{,-32bit} libglfw3-wayland \
    libglvnd-devel{,-32bit} libva-devel{,-32bit} libvdpau-devel{,-32bit} libxcb-devel{,-32bit} \
    libxcb-dri2-0{,-32bit} libxcb-dri3-0{,-32bit} libxshmfence-devel waffle-devel wine-devel

# kDE environment
# -----------------------------------------------------------------------------
# sudo -E zypper in -y libplasma6-devel
sudo -E zypper in -y \
    fcitx5{,-rime} filelight{,-lang} freerdp-wayland kdeconnect-kde{,-lang} krdc{,-lang} krfb{,-lang} \
    kvantum-manager{,-lang} pam_kwallet6 partitionmanager{,-lang}
# sudo -E zypper in -t pattern devel_qt6 devel_kde_frameworks6

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

# TeX environment
# -----------------------------------------------------------------------------
sudo -E zypper in -y 'texlive-*'

# Emacs
# -----------------------------------------------------------------------------
sudo -E zypper in -y emacs emacs-nox emacs-x11 libtool libvterm-{tools,devel}

# Font
# -----------------------------------------------------------------------------
sudo -E zypper in -y \
    adobe-source{serif4,sans3,codepro}-fonts adobe-sourcehan{serif,sans}-{cn,hk,jp,kr,tw}-fonts fontawesome-fonts \
    symbols-only-nerd-fonts wqy-{bitmap,microhei,zenhei}-fonts
