# Unified Workflow CLI (`wf`)

A high-performance, unified rewrite of the Python-based workflow script
collection in **Rust**, built with **Meson** + **Cargo**.

## Subcommands

| Command | Former Python tool | Status |
|---|---|---|
| `wf build` | `builder` | Planned |
| `wf stack` | `git_stack` | Planned |
| `wf gpu` | `gputest` | Planned |
| `wf remote` | `setup_remotes` | Planned |
| `wf crypt` | `transcrypt` | Core complete; CLI wired |

## Architecture

```
wf/
├── Cargo.toml           # Rust dependency manifest
├── meson.build          # Meta build system (invokes cargo)
├── docs/
│   ├── core.md          # Core library reference
│   └── crypt.md         # wf crypt design & usage
└── src/
    ├── main.rs          # Binary entry point (clap CLI)
    ├── cli.rs           # GlobalOptions, Resolver
    ├── core/            # Shared library modules
    │   ├── log.rs       # Logging & dry-run flags
    │   ├── process.rs   # Subprocess builder
    │   ├── git.rs       # Pure-CLI Git API
    │   ├── config.rs    # TOML config loader
    │   └── crypto.rs    # AEAD encrypt/decrypt (SIV)
    └── cmd/             # Subcommand implementations
        ├── builder.rs
        ├── stack.rs
        ├── gpu.rs
        ├── remote.rs
        └── crypt.rs
```

## Global flags

| Flag | Description |
|---|---|
| `-v, --verbose` | Enable debug logging |
| `-n, --dry-run` | Print what would be executed; no side effects |
| `-c, --config <PATH>` | Explicit TOML v1.0 configuration file |

## Building

### Prerequisites

- [Rust](https://rustup.rs/) (stable, 1.75+)
- [Meson](https://mesonbuild.com/) (1.0+)
- [Ninja](https://ninja-build.org/)

### Quick start

```bash
# 1. Set up the build directory
meson setup build

# 2. Compile
meson compile -C build

# 3. Run
./build/wf --help
```

The Meson build delegates all Rust compilation to Cargo; the first build
fetches crate dependencies from crates.io and may take a minute.

### Development build (debug symbols, no optimisations)

```bash
meson setup build-debug --buildtype=debug
meson compile -C build-debug
```

### Direct Cargo usage (faster iteration)

```bash
cargo build --release
./target/release/wf --help
```

## Testing

Unit tests live alongside their modules in `#[cfg(test)]` blocks.

```bash
# Via Meson (recommended for CI)
meson test -C build

# Via Cargo (faster during development)
cargo test
```

## Configuration

`wf` uses **TOML v1.0** exclusively.  The configuration file is resolved in
this order:

1. `-c <PATH>` CLI flag
2. `WF_CONFIG` environment variable
3. `wf.toml` in the current directory
4. `.wf.toml` in the current directory

When the resolved path is a **directory**, every `*.toml` file inside is
merged in alphabetical order — later files override earlier ones.

## Core library documentation

See [`docs/core.md`](docs/core.md) for a detailed reference on every module
in `src/core/`.

## `wf crypt` — transparent file encryption

See [`docs/crypt.md`](docs/crypt.md) for the full design, configuration
resolution chain, and Git filter integration guide.

### Quick example

```bash
# Encrypt a file (outputs base64 to stdout)
echo "TOP SECRET" | wf crypt clean secrets/key.pem

# Decrypt
cat secrets/key.pem.enc | wf crypt smudge secrets/key.pem
```

### Git filter integration

```ini
# .git/config
[filter "transcrypt"]
    clean   = wf crypt clean %f
    smudge  = wf crypt smudge %f
    required = true
[diff "transcrypt"]
    textconv = wf crypt textconv
```

```gitattributes
# .gitattributes
secrets/**  filter=transcrypt diff=transcrypt merge=transcrypt
```
