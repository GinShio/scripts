#!/usr/bin

sudo zypper ar -fcg obs://science:GPU:ROCm openSUSE:ROCm
sudo zypper ref

# Mesa
sudo zypper in -y \
    libvulkan_radeon{,-32bit} xf86-video-amdgpu

# ROCm / AMD Support
sudo -E zypper in -y \
    amdsmi hipcc 'libhip*' 'librocalution*' 'librocblas*' 'librocfft*' 'librocm-core*' rocminfo rocm-clang \
    rocm-clang-devel rocm-clang-libs rocm-clang-runtime-devel rocm-cmake rocm-compilersupport-macros rocm-device-libs \
    rocm-hip{,-devel} rocm-libc++-devel rocm-lld rocm-llvm rocm-llvm-devel rocm-llvm-libs rocm-llvm-static rocm-smi \
    roctracer
