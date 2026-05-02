# Unified Workflow CLI (`wf`)

This project is a high-performance, unified rewrite of the Python-based workflow script collection using **Zig** and **Meson**.

## Architecture

The CLI is structured around a single binary `wf` with multiple subcommands, each representing a previously independent tool:

- `wf build` (formerly `builder`): Branch-aware build orchestration.
- `wf stack` (formerly `git_stack`): Stacked PRs and remote synchronization.
- `wf gpu` (formerly `gputest`): GPU test automation and management.
- `wf remote` (formerly `setup_remotes`): Git remotes configuration and mirroring.
- `wf crypt` (formerly `transcrypt`): Transparent file encryption for Git.

### Global Options

Global options control the behavior across all subcommands:
- `-v, --verbose`: Enable debug logging.
- `-n, --dry-run`: Preview actions without executing them.
- `-c, --config <path>`: Specify a TOML v1.0 configuration file.

## Building

This project uses Meson as the primary build system, delegating compilation to the Zig compiler.

```bash
# 1. Setup the build directory
meson setup build

# 2. Compile the project
meson compile -C build

# 3. Run the executable
./build/wf --help
```

## Testing

Unit tests are written natively in Zig using `test` blocks. Meson is configured to discover and run them.

```bash
meson test -C build
```

## Configuration

The tool exclusively supports **TOML v1.0** for configuration files, ensuring a strict, predictable, and human-readable format across all subcommands.
