# Configuration System

This document describes the technical details of the configuration system, including the directory structure, file layout, variable resolution, preset inheritance, and validation rules.

---

## Directory Layout

By default the tool reads configuration content from the repository `config/` directory. You can layer additional
locations using the `BUILDER_CONFIG_DIR` environment variable or the `-C/--config-dir` CLI option; later entries win
when files share the same stem. The priority order is:

1. Repository `config/`
2. Each path in `BUILDER_CONFIG_DIR` (supports the platform path separator and relative entries)
3. Paths provided via `-C/--config-dir` (repeat flag or separate paths with the platform path separator)

All directories are merged, so shared configuration files and projects can be overridden in higher-priority layers.

The configuration files are organized in the following structure as an example within each directory:

```text
/config
├── config.toml                    # Global configuration file
├── company-base.toml              # Shared base configuration
└── projects/                      # Project-specific configuration (optional)
   ├── myapp.toml
   ├── libcore.toml
   ├── webserver.toml
   └── vendor/                    # Optional organization nesting
      └── graphics.toml
```

### Key Points

- **Shared base configuration**: Files such as `company-base.toml`, `company-base.json`, or `company-base.yaml` contain reusable settings shared across multiple projects. Higher-priority directories can override these entries with updated values.
- **Project configuration**: Builder auto-detects project configuration files wherever they appear inside `config/` (or any configured directory). You can place `*.toml`/`*.json`/`*.yaml` project files directly in the root or in arbitrarily nested subdirectories. Only one file per stem is allowed (e.g. `myapp.toml` *or* `myapp.yaml`, not both). When a file resides in a subdirectory Builder uses the first directory segment to infer the owning organization (for example `config/vendor/demo.toml` → `org = "vendor"`). If the first segment is `projects`, the next segment (if any) is used instead to ease migrations from the previous layout.
- **Layered overrides**: Project files discovered later in the precedence chain replace earlier definitions for the same project name, enabling local or user-specific overrides without editing the shared repository copy.


## File Naming Conventions

- Use the project name as the file name (e.g. `myapp.toml`, `myapp.json`).
- Keep file names concise and avoid special characters.
- Shared configuration files can use descriptive names such as `company-base.yaml` or `linux-defaults.toml`.
- Builder automatically selects the parser based on the file extension (TOML, JSON, or YAML).

> **Dependency note**: YAML parsing relies on `PyYAML`. The package depends on it by default, but custom environments must ensure it is installed.

---

## Global Configuration

### Example Structure:

```toml
[global]
# Default build type
default_build_type = "Debug"

# Logging level: debug, info, warning, error, none
log_level = "info"

# Log file path
log_file = "{{builder.path}}/logs/log.txt"

# Default operation mode
default_operation = "auto"  # auto, config-only, build-only, reconfig
```

### Key Fields:
- `default_build_type`: Specifies the default build type (e.g., Debug or Release).
- `log_level`: Controls the verbosity of logging.
- `log_file`: Path to the log file, supporting template variables.
- `default_operation`: Defines the default build operation mode.

---

## Project Configuration

Each project configuration file defines the project-specific settings.

### Example Structure:

```toml
[project]
# Project identifier (required)
name = "myapp"

# Owning organization (optional)
# If omitted, Builder infers it from the config path (e.g. projects/<org>/myapp.toml)
# or treats the project as unscoped.
org = "example"

# Project root directory (required)
source_dir = "/home/user/projects/{{project.name}}"

# Build directory (optional, relative to the project root)
# Omit to disable build orchestration for this project
build_dir = "_build/{{user.branch}}_{{user.build_type}}"

# Default toolchain to use when planning builds (required when build_dir/build_system are set)
toolchain = "clang"

# Installation directory (optional, defaults to /usr/local)
install_dir = "_install/{{user.branch}}_{{user.build_type}}"

# Build system type (required if build_dir is set)
build_system = "cmake"  # cmake, meson, cargo, make

# Build generator (optional)
generator = "Ninja"  # e.g., Ninja, Visual Studio 17 2022

# Monorepo component directory (optional)
component_dir = "packages/my-component"

# Monorepo build behavior (optional)
build_at_root = true  # true = place _build inside project root, false = inside component_dir

# Monorepo source layout override (optional)
source_at_root = true  # true = keep {{project.source_dir}} as-is, false = append component_dir for configuration tools

# Extra arguments forwarded to build tooling (optional)
extra_config_args = ["-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]
extra_build_args = ["--target", "install"]

[project.environment]
# Optional project-scoped environment overrides. Values can reference
# other project entries or existing OS variables (e.g. {{env.PATH}}).
TOOLS_ROOT = "{{builder.path}}/env/tools"
BIN_DIR = "{{project.environment.TOOLS_ROOT}}/bin"
CUSTOM_PATH = "{{env.PATH}}:{{project.environment.BIN_DIR}}"

[git]
# Project Git URL (required)
url = "https://example.com/example/app.git"

# Main branch name (required)
main_branch = "main"

# Submodule-specific branch (optional)
component_branch = "develop"

# Automatically stash working tree changes (optional)
auto_stash = true

# Custom update and clone scripts (optional)
update_script = "{{project.source_dir}}/scripts/update.sh"
clone_script = "{{project.source_dir}}/scripts/clone.sh"

[git.environment]
# Environment overrides used for git commands and custom scripts (optional)
SSH_COMMAND = "ssh -i {{project.environment.TOOLS_ROOT}}/keys/deploy_rsa"
```

If you omit `project.org`, Builder infers it from the file path: the first folder beneath each configuration directory becomes the organization (for example `config/vendor/myapp.toml` or `config/projects/vendor/myapp.toml` both produce `org = "vendor"`). Files stored at the configuration root (or directly inside `projects/`) remain unscoped and keep using the bare project name.

The `toolchain` field selects the default toolchain for the project. It must match either a built-in entry (`clang`,
`gcc`, `msvc`, or `rustc`) or a name provided by your configuration's `toolchains` registry. The CLI `--toolchain`
flag can temporarily override this selection when running a build.

Use `extra_config_args` to append arguments only to the configuration command
(for example, extra `-D` definitions for CMake). Use `extra_build_args`
for flags that should only be passed to the build step (such as `--target`).

Project-level environment variables are resolved before presets run and merge into the build environment. They support all template placeholders (including references to other `project.environment` entries) and become available through `{{env.NAME}}` and `{{project.environment.NAME}}`. The optional `[git.environment]` section behaves similarly but applies only to Git operations and custom clone/update scripts.

After presets apply, configuration fields (such as `extra_config_args`, `extra_build_args`, or script paths) can reference preset outputs using `{{preset.environment.NAME}}` and `{{preset.definitions.NAME}}`.

> **Runtime overrides**: The CLI `-DNAME=VALUE` / `--definition NAME=VALUE` flags temporarily extend or replace resolved definitions without editing configuration files.

## Project Dependencies

Projects can express relationships with other configured projects using an
array of tables named `dependencies`:

```toml
[[dependencies]]
name = "libcore"          # Project name declared in another file
org = "example"           # Optional organization hint when the name is reused
presets = ["ci", "asan"]  # Optional presets applied when building the dependency

[[dependencies]]
name = "tools"
```

Dependencies are resolved transitively and executed in topological order before
the requested project. Cycles are rejected during planning. To track a project
without executing build steps, omit its `build_dir`; the dependency will still
be planned so variables resolve, but no configure/build commands will run.

### Organizations and Fully Qualified Project Names

- Every project can declare an organization via `project.org`. When the field is missing Builder infers it from the configuration path (`config/projects/<org>/<project>.toml`) when possible.
- Projects are uniquely addressed by `org/name` once an organization is known. Unscoped projects remain addressable by the bare name.
- CLI commands accept either the bare name or the fully qualified form. Supply `--org` to disambiguate a project when multiple organizations reuse the same name.
- Dependency entries may include `org` to reference a specific organization; otherwise Builder picks the matching project when the name is unique. You can also provide a fully qualified name directly (for example `name = "vendor/libcore"`).

---

## Preset Configuration

Presets define reusable configurations and allow inheritance and conditional logic.

### Example Structure:

```toml
[presets.preset-name]
# Inheriting other presets (optional)
extends = ["base-preset", "{{user.toolchain}}-preset"]

# Conditional application (optional)
condition = "[[ {{system.os}} == 'linux' and {{user.architecture}} == 'x64' ]]"

# Environment variables (optional)
environment = {
    CC = "clang",
    CXX = "clang++",
    CFLAGS = "-O2 -Wall"
}

# Optional scoping metadata (defaults to global visibility)
# org = "example"
# project = "myapp"

# Newly defined environment variables can reference other entries using the
# `{{env.NAME}}` placeholder. Resolution happens after each preset merges, so
# variables created earlier in the chain are available to later ones.

# Build system parameters (optional)
definitions = {
    CMAKE_BUILD_TYPE = "Release",
    BUILD_TESTS = true,
    LINK_JOBS = "[[ {{system.memory.total_gb}} // 2 ]]"
}

# Additional build arguments (optional)
extra_config_args = ["-DENABLE_WARNINGS=ON"]
extra_build_args = ["--warn-uninitialized"]
```

> **Tip:** Environment placeholders now include variables introduced by presets.
> Use `{{env.NAME}}` (or `{{preset.environment.NAME}}`) to reference values defined
> earlier in the inheritance chain or the same preset, enabling layered configuration
> without repeating paths. Circular references (for example `FOO -> BAR -> FOO`) are
> detected automatically and reported with the full dependency chain.

### Inheritance Rules:
- Use the `extends` field to inherit other presets.
- Inheritance is static, and the order determines override priority (later overrides earlier).
- Cyclic inheritance is not allowed and will be validated.
- Scoped presets (those declaring `org` and/or `project`) are only considered when they match the active project. Resolution prefers project-scoped presets, then organization-scoped, then global entries.

### Resolution Order

Builder constructs the final build environment and definition map by layering sources in a strict order (later entries override earlier data while retaining access to previously resolved variables):

1. Initialize empty `environment` / `definitions` containers.
2. Apply the active toolchain's base values (global environment + definitions + build-system overrides).
3. Derive build-system defaults from the toolchain (compiler/linker commands, launchers, color diagnostics, default `CMAKE_BUILD_TYPE`, `CARGO_TARGET_DIR`, etc.).
4. Resolve and merge `[project.environment]` so project variables can reference toolchain-provided values.
5. Apply default presets supplied by Builder (for example `configs.debug` / `configs.release`, when present).
6. Apply presets supplied on the CLI (`--preset`) in the order they are declared; each preset can override earlier sources.
7. Finally apply CLI `-DNAME=VALUE` / `--definition NAME=VALUE` overrides.

This layering ensures presets and ad-hoc overrides see the complete context produced by the toolchain while still retaining the ability to supersede any earlier value.

---

## Variable Resolution

### Supported Variables

1. **User Variables**:
   - `{{user.branch}}`: Current Git branch.
   - `{{user.build_type}}`: Build type (Debug/Release).
   - `{{user.generator}}`: Build generator name.
   - `{{user.toolchain}}`: Selected toolchain identifier.
   - `{{user.linker}}`: Preferred linker executable (if available).
   - `{{user.cc}}`: Default C compiler for the active toolchain.
   - `{{user.cxx}}`: Default C++ compiler for the active toolchain.
2. **Project Variables**:
   - `{{project.name}}`: Project name.
   - `{{project.source_dir}}`: Project source directory.
   - `{{project.org}}`: Owning organization (if defined).
3. **System Variables**:
   - `{{system.os}}`: Operating system name.
   - `{{system.architecture}}`: System architecture.
4. **Environment Variables**:
   - `{{env.PATH}}`: Environment variable values.

### Resolution Rules:
- Variables are resolved at runtime.
- Variables must maintain consistent types across overrides. **Type mismatches will cause an error.**
- Lazy evaluation ensures variables are calculated only when needed.

---

## Validation Rules

### Required Fields:
- `project.name`: Must be defined and unique within its organization (or globally when no organization is set).
- `project.org`: Optional; when present the combination `project.org` + `project.name` must be unique across the configuration set.
- `project.source_dir`: Must exist and be accessible.
- `git.url`: Remote repository URL must be defined.
- `git.main_branch`: Main branch must be specified.
- `project.build_system`: Required when `project.build_dir` is provided. Supported values: `cmake`, `meson`, `bazel`, `cargo`, `make`.
- `project.build_dir`: Required for `cmake`, `meson`, `cargo`, and `make`. Optional otherwise—omit it to mark a project as "validate only".
- `project.toolchain`: Required whenever a project defines a build system; optional when no build system is configured.

### Preset Validation:
- Preset names must be unique.
- Inheritance chains must not contain cycles.
- Conditional expressions must be syntactically correct.
- Template variables must resolve correctly.

### Build System Compatibility:
- Parameters must conform to the build system's requirements.
- Environment variables must be valid and compatible.

---

## Monorepo Support

### Component Detection
1. **Standalone Project**: No `component_dir` specified.
2. **Directory Component**: `component_dir` points to a local directory.
3. **Submodule Component**: `component_dir` points to a Git submodule.

### Build Strategy
- Controlled via `build_at_root`:
  - `true`: Build at the root-level `_build` directory.
  - `false`: Build at the component-level directory.
- `source_at_root` (optional) determines where configuration tools such as CMake point their `-S` directory:
   - `true` (default for `build_at_root = true`): use `{{project.source_dir}}`.
   - `false`: use `{{project.source_dir}}/{{project.component_dir}}`.
- `build_at_root` controls where the build directory is created:
   - `true`: `{{project.build_dir}}` resolves relative to `{{project.source_dir}}`.
   - `false`: the build directory resolves relative to `{{project.source_dir}}/{{project.component_dir}}` regardless of `source_at_root`.
- Projects can define their own presets or inherit global ones.

---

## Example Configuration

### Global Configuration (`config.toml`):

```toml
[global]
default_build_type = "Release"
log_level = "debug"
log_file = "{{builder.path}}/logs/build.log"
default_operation = "auto"
```

### Project Configuration (`projects/example/myapp.toml`):

```toml
[project]
name = "myapp"
org = "example"
source_dir = "/home/user/projects/myapp"
build_dir = "_build/main_Debug"
install_dir = "_install/main_Debug"
build_system = "cmake"
generator = "Ninja"

[project.environment]
TOOLS_ROOT = "{{builder.path}}/env/tools"
BIN_DIR = "{{project.environment.TOOLS_ROOT}}/bin"
CUSTOM_PATH = "{{env.PATH}}:{{project.environment.BIN_DIR}}"

[git]
url = "https://github.com/example/myapp.git"
main_branch = "main"
auto_stash = true

[git.environment]
SSH_COMMAND = "ssh -i {{project.environment.TOOLS_ROOT}}/keys/deploy_rsa"
```

### Preset Configuration:

```toml
[presets.default]
extends = ["base"]
environment = {
    CC = "gcc",
    CXX = "g++"
}
definitions = {
    CMAKE_BUILD_TYPE = "Debug",
    ENABLE_TESTS = true
}
```

---

## Toolchain Configuration

Builder loads shared toolchain metadata from a top-level file named `toolchains.{toml,json,yaml}` inside each configuration directory. Entries define compiler, linker, launcher, and build-system-specific settings that apply whenever the matching toolchain is active (selected via project configuration or the `--toolchain` flag). Definitions from higher-priority configuration directories override previously loaded values.

### Example (`config/toolchains.toml`)

```toml
[toolchains.clang]
description = "LLVM Clang"
supports = ["cmake", "meson", "bazel", "make"]
linker = "mold"
launcher = "ccache"

[toolchains.clang.environment]
CC = "clang-17"
CXX = "clang++-17"
AR = "llvm-ar-17"
RANLIB = "llvm-ranlib-17"

[toolchains.clang.build_systems.cmake.definitions]
CMAKE_AR = "llvm-ar-17"
CMAKE_RANLIB = "llvm-ranlib-17"

[toolchains.gcc.environment]
CC = "gcc"
CXX = "g++"

[toolchains.rustc]
supports = ["cargo"]

[toolchains.rustc.environment]
RUSTC = "rustc"
CARGO = "cargo"
```

### Schema Overview

- `description` *(optional)* – free-form text for humans.
- `supports` *(optional)* – list of build systems the toolchain may drive. If omitted, the built-in compatibility matrix is used.
- `cc`, `cxx`, `rustc`, `linker` *(optional)* – explicit compiler/linker commands. When omitted, Builder falls back to `environment.CC`, `environment.CXX`, or built-in defaults.
- `launcher` *(optional)* – an executable such as `ccache` that should wrap compiler commands. When provided, Builder prefixes `CC`/`CXX` values and sets `CMAKE_*_COMPILER_LAUNCHER` where applicable.
- `environment` *(optional)* – key/value environment variables applied to every build using this toolchain. Values resolve templates like any other configuration field.
- `definitions` *(optional)* – build definitions injected ahead of preset/project definitions (for example, global `CMAKE_*` values).
- `build_systems.<name>.launcher` *(optional)* – override the launcher for a specific build system (lowercase name such as `cmake`).
- `build_systems.<name>.environment` *(optional)* – per-build-system environment overrides (`name` is normalized to lowercase, e.g. `cmake`).
- `build_systems.<name>.definitions` *(optional)* – per-build-system definitions.
- `metadata` *(optional)* – arbitrary mapping preserved for tooling; not interpreted by Builder.

Entries merge deeply when the same toolchain name appears in multiple directories: later definitions extend or override earlier environment values, definitions, and per-build-system overrides. Creating a brand-new toolchain requires specifying at least one supported build system (`supports`), otherwise the default compatibility matrix must already include the toolchain name.

## Conclusion

This configuration system provides a robust and flexible way to manage project settings. It supports global settings, project-specific configurations, reusable presets, advanced templating with variable resolution, and now centralized toolchain metadata.
