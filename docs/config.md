# Configuration System

This document describes the technical details of the configuration system, including the directory structure, file layout, variable resolution, preset inheritance, and validation rules.

---

## Directory Layout

The configuration files are organized in the following structure as example:

```text
/config
├── config.toml                    # Global configuration file
├── company-base.toml              # Shared base configuration
└── projects/                      # Project-specific configuration
    ├── myapp.toml
    ├── libcore.toml
    └── webserver.toml
```

### Key Points:
- **Global Configuration**: `config.*` (TOML/JSON/YAML) serves as the central configuration file for global settings.
- **Shared Base Configuration**: Files such as `company-base.toml`, `company-base.json`, or `company-base.yaml` contain reusable configurations shared across multiple projects.
- **Project Configuration**: Each project has its own configuration file under `projects/`, named after the project. Only one file per stem is allowed (e.g., don't mix `myapp.toml` and `myapp.yaml`).

---

## File Naming Conventions

Supported file extensions are `.toml`, `.json`, `.yaml`, and `.yml`. Choose one format per configuration entry.

1. **Project Configuration**:
   - Use the project name as the file name (e.g., `myapp.toml`, `myapp.json`).
   - File names must be concise and avoid special characters.
2. **Shared Configuration**:
   - Use descriptive names for shared configurations (e.g., `company-base.yaml`).

> **Dependency note**: YAML files require the `PyYAML` package. It is included in the default project dependencies, but custom environments must ensure it is installed.

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

# Project root directory (required)
source_dir = "/home/user/projects/{{project.name}}"

# Build directory (required, relative to the project root)
build_dir = "_build/{{user.branch}}_{{user.build_type}}"

# Installation directory (optional, defaults to /usr/local)
install_dir = "_install/{{user.branch}}_{{user.build_type}}"

# Build system type (required)
build_system = "cmake"  # cmake, meson, cargo, make

# Build generator (optional)
generator = "Ninja"  # e.g., Ninja, Visual Studio 17 2022

# Monorepo component directory (optional)
component_dir = "packages/my-component"

# Monorepo build behavior (optional)
build_at_root = true  # true = build at root, false = build at component level

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
```

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

# Build system parameters (optional)
definitions = {
    CMAKE_BUILD_TYPE = "Release",
    BUILD_TESTS = true,
    LINK_JOBS = "[[ {{system.memory.total_gb}} // 2 ]]"
}

# Additional build arguments (optional)
extra_args = ["--warn-uninitialized"]
```

### Inheritance Rules:
- Use the `extends` field to inherit other presets.
- Inheritance is static, and the order determines override priority (later overrides earlier).
- Cyclic inheritance is not allowed and will be validated.

---

## Variable Resolution

### Supported Variables

1. **User Variables**:
   - `{{user.branch}}`: Current Git branch.
   - `{{user.build_type}}`: Build type (Debug/Release).
   - `{{user.generator}}`: Build generator name.
2. **Project Variables**:
   - `{{project.name}}`: Project name.
   - `{{project.source_dir}}`: Project source directory.
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
- `project.name`: Must be defined and unique.
- `project.source_dir`: Must exist and be accessible.
- `project.build_dir`: Must be defined.
- `project.build_system`: Must specify a supported build system.
- `git.url`: Remote repository URL must be defined.
- `git.main_branch`: Main branch must be specified.

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

### Project Configuration (`projects/myapp.toml`):

```toml
[project]
name = "myapp"
source_dir = "/home/user/projects/myapp"
build_dir = "_build/main_Debug"
install_dir = "_install/main_Debug"
build_system = "cmake"
generator = "Ninja"

[git]
url = "https://github.com/example/myapp.git"
main_branch = "main"
auto_stash = true
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

## Future Considerations

1. **Dynamic Expression Security**:
   - Currently, the system assumes users provide valid expressions. Future versions may include sandboxing for safer evaluation.
2. **Dependency Management in Monorepo**:
   - Dependency relationships between components are not yet handled. Extensions may include DAG-based dependency resolution.

---

## Conclusion

This configuration system provides a robust and flexible way to manage project settings. It supports global settings, project-specific configurations, reusable presets, and advanced templating with variable resolution.
