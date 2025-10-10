# Build System

This document describes the technical design and usage of the build system, including its core principles, configuration process, Git integration, supported build systems, and advanced features.

---

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

1. Scan all `config/projects/*.toml` files.-C
2. Identify the target project configuration based on the project name.
3. Load project-specific configurations, including Git settings and build directory configurations.
4. Parse preset configurations and resolve inheritance chains.
5. Apply template variables and conditional expressions.
6. Determine the actual main branch to be used for the project.

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
- Users can override the default installation path via command-line options.

---

## Supported Build Systems

### Toolchain Management

- **Default Toolchains**:
  - Unix-like systems: `clang`.
  - Windows systems: `msvc`.

- **Compatibility Check**:
  - The system validates the compatibility between the selected toolchain and build system.
  - Incompatible combinations (e.g., `clang` with `Cargo`) result in an error.

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

---

### Additional Build System Arguments

```shell
# Add custom build system arguments
builder build myapp -X'--jobs=8'
```

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
