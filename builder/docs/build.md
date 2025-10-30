# Build System
This document describes the technical design and usage of the build system, including its core principles, configuration process, Git integration, supported build systems, and advanced features.
## Core Design Principles

### Design Principles

1. **Unified Configuration**: All project configurations are managed centrally under the `config` directory in the builder's root directory.
2. **Branch Awareness**: Automatically handles Git branch switching and build directory isolation.
3. **Preset-Driven**: Enables flexible build configurations through combinable presets.
4. **Intelligent Defaults**: Provides sensible default behaviors, reducing the need for extensive manual configuration.
5. **Toolchain Compatibility**: Ensures compatibility between the selected toolchain and build system.

---

## Configuration Process

### Loading Workflow

1. Discover project definitions across every configured directory (repository `config/`, entries in `BUILDER_CONFIG_DIR`, and any `-C/--config-dir` values).
2. Identify the target project configuration using the supplied project name, organization hint (`--org`), or fully qualified `org/name` reference.
3. Load project-specific configurations, including Git settings and build directory configurations.
4. Parse preset configurations and resolve inheritance chains.
5. Apply template variables and conditional expressions.
6. Determine the actual main branch to be used for the project.

### Layered Configuration Sources

The loader accepts additional configuration directories in priority order. The default repository `config/` directory
is always included. Directories listed in the `BUILDER_CONFIG_DIR` environment variable are processed next, followed by
any paths supplied via the `-C/--config-dir` CLI option. Later directories override earlier ones when files share a stem,
allowing per-user or per-machine customizations without modifying shared configuration. Parser selection is automatic
based on the file extension (TOML, JSON, or YAML).

---

## Git Integration

### Branch Usage Scenarios

| Project Type       | Configuration                          | Source Code Location | Branch Used  |
|--------------------|----------------------------------------|-----------------------|--------------|
| Standalone Project | `source_dir = "XXX", main_branch = "main"` | `source_dir`         | `main`       |
| Directory Component| `source_dir = "XXX", component_dir = "XXX", main_branch = "main"` | `component_dir` | `main` |
| Submodule Component| `source_dir = "XXX", component_dir = "XXX", main_branch = "main"` | `component_dir` | `main` |
| Submodule Component| `source_dir = "XXX", component_dir = "XXX", main_branch = "main", component_branch = "develop"` | `component_dir` | `develop` |
| Submodule Component| `source_dir = "XXX", component_dir = "XXX", main_branch = "main", component_branch = "develop", --branch=my-build` | `component_dir` | `my-build` in component dir |

### Automatic Branch Management

```shell
# Build for the main branch (default)
builder build myapp --preset development

# Disambiguate similarly named projects
builder build vendor/myapp --preset development
builder build --org vendor myapp --preset development

# Build for a specific branch (with automatic switching)
builder build myapp --preset development --branch feature-x

# Build without switching branches
builder build myapp --preset development --branch feature-x --no-switch-branch
```

#### Key Behaviors:
- Compare the current `commit hash` to determine whether a branch switch is necessary.
- If hashes match, only switch the build directory without performing Git operations.
- If hashes differ, perform a full branch switching workflow.

### Working Tree State Handling

```toml
[git]
auto_stash = true          # Automatically stash uncommitted changes
```

#### Workflow:
1. Check the current working tree state.
2. If there are uncommitted changes:
   - **`auto_stash = true`**: Automatically stash changes and proceed.
   - **`auto_stash = false`**: Report an error and exit.
3. Switch to the target branch (if required).
4. Perform the build.
5. Switch back to the original branch and restore stashed changes (if applicable).

---

## Build Process

### Supported Operation Modes

```shell
# Default intelligent mode
builder build myapp

# Configuration-only mode
builder build myapp --config-only

# Build-only mode
builder build myapp --build-only

# Reconfiguration mode
builder build myapp --reconfig

# Dry-run mode
builder build myapp --dry-run
```

Inspect resolved variables and preset environment mappings for troubleshooting:

```shell
builder build myapp --preset development --dry-run --show-vars
```

Increase logging verbosity for long-running builds:

```shell
builder build myapp --preset development --verbose
```

### Frequently Used Options

- `-p NAME` / `--preset NAME[,NAME]` – apply or stack presets.
- `-b NAME` / `--branch NAME` – build against a specific branch (project and component repos).
- `--org NAME` – select the organization when multiple projects share the same name; alternatively use the fully qualified `org/name` syntax directly.
- `-n` / `--dry-run` – print the Git/build commands without running them.
- `-G NAME` / `--generator NAME` – force a particular generator (Ninja, Visual Studio, etc.).
- `-B TYPE` / `--build-type TYPE` – override the build type (Debug/Release/custom profiles).
- `-t TARGET` / `--target TARGET` – build a single target when the backend supports it.
- `-DNAME=VALUE` / `--definition NAME=VALUE` – inject temporary build definitions (applies during configuration).
- `-T NAME` / `--toolchain NAME` – select a toolchain (clang, gcc, msvc, rustc).

#### Mode Details

1. **`config-only` Mode**:
   - Generates the build directory and configuration files.
   - Does not execute the build process.
   - Outputs success or failure status.

2. **`build-only` Mode**:
   - Verifies that the build directory is already configured.
   - Executes the build process directly.
   - Reports an error if the configuration step has not been performed.

3. **`reconfig` Mode**:
   - Cleans the build directory completely.
   - Re-executes the configuration step.
   - Does not execute the build process.

4. **`dry-run` Mode**:
   - Displays all commands that would be executed.
   - Shows environment variable settings and build directory paths.
   - Does not perform any actual operations.

---

### Target-Specific Builds

```shell
# Build a specific target
builder build myapp --target part-m
```

#### Behavior:
- Allows selective builds for specific components or targets within a project.
- Supported by all build systems (e.g., CMake, Meson).

---

### Installation Support

```shell
# Automatically install after build
builder build myapp --install
```

#### Behavior:
- Installs the project to the directory specified in `project.install_dir`.
- Users can override the default installation path with `--install-dir`.

---

## Supported Build Systems

### Toolchain Management

- **Explicit selection**:
   - Every project must set `project.toolchain` (or `project.default_toolchain` via shared config) when a build system is
      configured.
   - The CLI `--toolchain NAME` flag can override the project default at runtime.

- **Built-in registry**:
   - Builder ships definitions for `clang`, `gcc`, `msvc`, and `rustc`. Custom entries defined in
      `toolchains.{toml,json,yaml}` extend or override these built-ins.

- **Launcher support**:
   - Toolchain definitions can specify `launcher = "ccache"` (or similar) globally or per build system. Launchers wrap
      compiler invocations and populate `CMAKE_*_COMPILER_LAUNCHER` when applicable.

- **Compatibility check**:
   - During planning the engine verifies that the chosen toolchain is allowed for the active build system. For example,
      attempting to drive Cargo with `clang` fails with a clear error message.

- **Configurable metadata**:
   - Toolchain entries can pin compiler executables, preferred linkers, and build-system definitions such as
      `CMAKE_*`. Builder applies these values ahead of preset/project configuration so downstream overrides remain
      predictable.

**Example Error**:
```
[Error] Toolchain "clang" is not compatible with build system "Cargo".
Hint: Use a compatible toolchain such as "rustc".
```

---

### CMake Integration

```toml
[project]
build_system = "cmake"

[presets.cmake-example]
generator = "Ninja"
definitions = {
    CMAKE_BUILD_TYPE = "Release",
    BUILD_SHARED_LIBS = "OFF"
}
```

**Generated Commands**:
```shell
# Configuration command
cmake -G "Ninja" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
      -B "_build/main_release" -S "/path/to/source"

# Build command
cmake --build "_build/main_release"
```

---

### Meson Integration

```toml
[project]
build_system = "meson"

[presets.meson-example]
definitions = {
    buildtype = "release",
    default_library = "static"
}
```

**Generated Commands**:
```shell
# Configuration command
meson setup --buildtype=release --default-library=static \
            "_build/main_release" "/path/to/source"

# Build command
meson compile -C "_build/main_release"
```

---

### Bazel Integration

```toml
[project]
build_system = "bazel"

[presets.bazel-example]
definitions = {
    TARGET = "//myapp:app",
    BUILD_OPTS = "--copt=-O2"
}

environment = {
    BAZEL_CACHE = "/home/user/.bazel_cache"
}
```

**Generated Commands**:
```shell
# Build command
bazel build //myapp:app --copt=-O2
```

---

### Cargo Integration

```toml
[project]
build_system = "cargo"
build_dir = "_build/main"

[presets.cargo-release]
extends = ["configs.release"]
extra_build_args = ["--workspace"]
```

**Generated Commands**:
```shell
# Optional prefetch (config-only / reconfig)
cargo fetch --locked

# Build command (Debug by default, add --release automatically when build_type=Release)
cargo build --target-dir _build/main
```

- `--target` is intentionally unsupported for Cargo projects; forward additional flags via `--extra-build-args`.
- Install mode is not currently available for Cargo builds.

---

## Error Handling and Debugging

### Compilation Errors

- On build failure, print the relevant error output.
- Provide a clear log location for further investigation.

**Example**:
```
$ builder build myapp --preset development
src/main.cpp:45:15: error: expected ';' after expression
    std::cout << "Hello world"
              ^
              ;

Build log: _build/main_dev/build.log
Suggestion: Use --verbose to view detailed output.
```

---

### Debugging Support

```shell
# Enable verbose output
builder build myapp --preset development --verbose
```

- Displays detailed command execution logs.
- Shows all resolved template variables and environment settings.
- Combine with `--show-vars` to emit the entire context before execution.

---

## Advanced Features

### Preset Combination

```shell
# Combine multiple presets
builder build myapp --preset feature-x,feature-y
```

#### Behavior:
- Presets are resolved in the order they are declared.
- Later presets override earlier ones.
- If no preset for the current build type is specified explicitly, the builder automatically applies `configs.<build_type>` for single-config generators. Multi-config generators automatically apply both `configs.debug` and `configs.release` (when present).

---

### Additional Build System Arguments

```shell
# Forward extra arguments to configuration/build steps
builder build myapp -Xconfig,-DENABLE_FEATURE -Xbuild,--jobs=8

# Provide multiple arguments in one go
builder build myapp --extra-config-args -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
                              --extra-build-args --target install --extra-build-args --parallel
```

- `-Xscope,arg` forwards a single argument. Use `scope=config` or `scope=build`
   to target only configuration or build steps. Omit the scope to send the
   argument to both.
- `--extra-config-args` and `--extra-build-args` accept one or more arguments
   per flag for bulk additions. They complement the configuration values of
   `extra_config_args` and `extra_build_args` in project or preset files.

---

## Validation and Verification

### Configuration Validation

```shell
# Validate configuration files
builder validate
```

### Display Variable Resolution

```shell
# Show resolved variables
builder build myapp --preset development --show-vars
```

---

## Conclusion

This build system provides a flexible and robust framework for managing complex build workflows. It supports multiple build systems, advanced debugging, and highly customizable presets, making it suitable for diverse development environments.
