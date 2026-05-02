project('cyber-wf', 'c',
    version: '0.1.0',
    default_options: ['warning_level=3', 'buildtype=release']
)

zig = find_program('zig', required: true)
cc = find_program('cc', 'gcc', 'clang', required: true)

# ---------------------------------------------------------------------------
# Zig Sources
# ---------------------------------------------------------------------------
# Explicitly listing sources is the idiomatic Meson way. 
# It allows Meson to accurately track dependencies for incremental rebuilds.
zig_sources = files(
    'src/main.zig',
    'src/cli.zig',
    'src/core/log.zig',
    'src/core/process.zig',
    'src/core/config.zig',
    'src/cmd/builder.zig',
    'src/cmd/crypt.zig',
    'src/cmd/gpu.zig',
    'src/cmd/remote.zig',
    'src/cmd/stack.zig',
)

# ---------------------------------------------------------------------------
# Buildtype Mapping
# ---------------------------------------------------------------------------
# Map Meson buildtype to Zig optimization flag
buildtype = get_option('buildtype')
if buildtype == 'release'
    zig_opt = '-OReleaseFast'
elif buildtype == 'debugoptimized'
    zig_opt = '-OReleaseSafe'
elif buildtype == 'minsize'
    zig_opt = '-OReleaseSmall'
else
    zig_opt = '-ODebug'
endif

# ---------------------------------------------------------------------------
# Step 1: Compile Zig to Static Library
# ---------------------------------------------------------------------------
# -fcompiler-rt bundles Zig's compiler-rt (provides __zig_probe_stack etc.)
# so the final link with the system C driver works in all build modes.
wf_lib = custom_target('wf-lib',
    input: zig_sources,
    output: 'libwf.a',
    command: [
        zig, 'build-lib',
        '-static',
        '-fcompiler-rt',
        zig_opt,
        '@INPUT0@',
        '--name', 'wf',
        '--cache-dir', meson.current_build_dir() / 'zig-cache',
        '--global-cache-dir', meson.current_build_dir() / 'zig-global-cache',
        '-lc',
    ],
    build_by_default: false,
    console: true,
)

# ---------------------------------------------------------------------------
# Step 2: Link with System Compiler
# ---------------------------------------------------------------------------
# Link with the system C compiler so host libc is found correctly.
# We use --whole-archive to ensure the linker doesn't strip the main entry point.
wf_exe = custom_target('wf',
    input: [wf_lib],
    output: 'wf',
    command: [
        cc,
        '-o', '@OUTPUT@',
        '-Wl,--whole-archive', '@INPUT@', '-Wl,--no-whole-archive',
        '-lm', '-lpthread',
    ],
    install: true,
    install_dir: get_option('bindir'),
    build_by_default: true,
    console: true,
)

# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------
test('zig-unit-tests', zig, 
    args: [
        'test', files('src/main.zig'),
        '--cache-dir', meson.current_build_dir() / 'zig-cache',
        '--global-cache-dir', meson.current_build_dir() / 'zig-global-cache',
    ],
    workdir: meson.current_source_dir()
)
