# GPU Test Tool Refactoring Plan

## 1. Overview

The current `gputest` implementation has become over-engineered and detached from the original operational requirements. The goal of this refactoring is to create a Python-based tool that strictly replicates the functionality of the original shell scripts while introducing configuration flexibility. The new design will prioritize simplicity, transparency, and ease of maintenance over abstract architectural purity.

## 2. Analysis of Original Scripts

### 2.1. Core Test Runner (`gpu_test.sh`)
**Functionality:**
- Accepts a list of test configurations in the format `vendor,glapi,testkits`.
- **Environment Setup:**
  - Configures environment variables (ICD paths, debug flags) based on `vendor` (mesa, swrast, llpc) and `glapi` (rusticl, zink).
  - Calculates `AVAILABLE_CPUS_CNT`.
- **Execution:**
  - Supports three test kits: `deqp`, `piglit`, `vkd3d`.
  - **dEQP:** Runs `deqp-runner`. Handles logic for Vulkan vs GL/GLES (different executables, case lists, and arguments).
  - **Piglit:** Runs `piglit-runner` with the `quick` profile.
  - **VKD3D:** Runs `test-runner.sh`.
- **Artifacts:**
  - Captures git SHA1s of relevant projects.
  - Generates `flakes.txt` and `testlist.txt`.
  - Compresses results into `.tar.zst` archives.
  - Syncs archives to a persistent result directory.

### 2.2. Baseline Restorer (`80-copy_gpu_tests.sh`)
**Functionality:**
- Runs on autostart.
- Scans the persistent result directory for archives from the last 10 days.
- Filters archives matching the current machine's `GPU_DEVICE_ID`.
- Extracts these archives into a runtime baseline directory (`/run/user/1000/runner/baseline`) to serve as reference for regression testing.

### 2.3. Cleanup Service (`90-cleanup-test-results.sh`)
**Functionality:**
- Deletes persistent archives older than 360 days.
- Deletes extracted baseline results older than 16 days to free up runtime space.

### 2.4. Toolbox Installer (`__ginshio_copy-graphisc-testcases.fish`)
**Functionality:**
- "Installs" test binaries and assets from source build directories to the runner directory.
- Handles specific exclusion lists (e.g., `vk-exclude.txt` for dEQP).
- Patches RPATHs for Piglit binaries.

## 3. Critique of Current Python Implementation

The current `gputest` implementation suffers from:
- **Over-abstraction:** Classes like `Planner`, `Executor`, `BundleContext`, and `ConfigOrchestrator` add unnecessary layers that obscure the simple "setup env -> run command" logic.
- **Inflexibility:** Adding a new driver combination or test suite requires code changes or complex config overrides rather than simple config entries.
- **Disconnect:** The logic for things like "calculating available CPUs" or "getting git SHA1s" is scattered or over-formalized compared to the direct shell approach.

## 4. Proposed Architecture

The new design will be a "Script Runner" architecture. It will read a TOML configuration and execute procedural logic that mirrors the shell scripts.

### 4.1. Configuration (`config.toml`)
The configuration adopts a layered approach: **Layout** (structure) -> **Driver** (implementation) -> **Backend** (compatibility layer) -> **Suite** (workload). A **Test** defines a specific combination of these.

```toml
[global]
result_dir = "~/Public/result"
runner_root = "/run/user/1000/runner"

# Layouts: Directory structures (reusable)
[layouts.mesa]
lib_dirs = ["lib", "lib64"]
icd_pattern = "share/vulkan/icd.d/*_icd.x86_64.json"
env = { LD_LIBRARY_PATH = "{root}/lib64:{root}/lib" }

# Drivers: Specific driver instances using a layout
[drivers.radv]
layout = "mesa"
root = "/usr"
env = { VK_ICD_FILENAMES = "{root}/share/vulkan/icd.d/radeon_icd.x86_64.json" }

# Backends: Compatibility layers (Optional)
[backends.zink]
env = { MESA_LOADER_DRIVER_OVERRIDE = "zink" }

# Suites: Test definitions
[suites.deqp-vk]
runner = "deqp-runner"
exe = "deqp-vk"
args = ["--deqp-log-images=disable"]

# Tests: The executable units
[tests.radv-zink-gl]
driver = "radv"
backend = "zink"
suite = "deqp-gl"

# Hooks: Integration with external scripts
[hooks]
# Define reusable commands. Variables like {path}, {name}, {root} are substituted at runtime.
get_git_info = "python3 ~/scripts/get_git_info.py {name} >> git-sha1.txt"
patchelf_piglit = "find {dest} -type f -executable -exec patchelf --set-rpath '$ORIGIN/../lib' {} +"
gen_vk_default = "fd --regex '.*\\.txt' -- {dest}/mustpass/vk-default | sed -e 's~^{dest}/mustpass/~~' > {dest}/mustpass/vk-default.txt"

# Toolbox: Configuration for copying test suites
[toolbox]
deqp_src = "{project_root}/khronos/deqp"
deqp_dest = "deqp"
# Reference hooks by name to run after installation
deqp_post_install = ["gen_vk_default"]

piglit_src = "{project_root}/fd.o/piglit"
piglit_dest = "piglit"
piglit_post_install = ["patchelf_piglit"]

# Tests: The executable units
[tests.radv-zink-gl]
driver = "radv"
backend = "zink"
suite = "deqp-gl"
# Reference hooks to run before/after the test
pre_run = ["get_git_info"]
```

### 4.2. Modules

1.  **`main.py`**: Entry point. Parses args.
2.  **`resolver.py`**:
    *   Resolves contexts.
    *   **Built-in Logic**: Calculates `AVAILABLE_CPUS` and injects it into the template context.
3.  **`executor.py`**:
    *   `run_test(test_context)`:
        *   **Device ID Detection**: Automatically detects `GPU_DEVICE_ID` for the current driver context (used for archive naming).
        *   **Hook Execution**: Runs configured `pre_run` hooks (e.g., `get_git_info`).
        *   **Execution**: Runs the suite.
        *   **Post-Processing**:
            *   Automatically generates `flakes.txt` (extracts flakes from runner results).
            *   Automatically generates `testlist.txt` (list of executed cases).
        *   **Hook Execution**: Runs configured `post_run` hooks.
        *   **Packing**: Creates the archive named `{suite}_{device_id}_{date}.tar.zst`.
4.  **`toolbox.py`**:
    *   `install_tools()`: Copies files.
    *   **Hook Execution**: Runs `post_install` hooks defined in config (e.g., for `patchelf`).
5.  **`maintenance.py`**:
    *   `restore()`:
        *   **Device Filtering**: Automatically detects current `GPU_DEVICE_ID` and only restores matching archives.

## 5. Refactoring Steps

1.  **Define Config Schema**: Create a `gputest/config/refactored.toml` that implements the `layout`/`backend`/`suite` model.
2.  **Implement Resolver**: Write the logic to merge Backend + Layout into a usable execution context.
3.  **Implement Toolbox**: Rewrite the fish script logic in Python.
4.  **Implement Maintenance**: Port cleanup/restore scripts.
5.  **Implement Runner**: Write the executor that uses the Resolver.


## 6. Key Differences from Old Python Tool

- **No "Planning" Phase**: The tool will not try to "plan" a run graph. It will just iterate over the requested items and run them.
- **Direct Shell Command Generation**: Instead of abstracting command runners, we will construct shell commands strings or lists directly from config, making it easier to debug (verbose mode = print the command).
- **Explicit Environment**: Environment variables will be merged explicitly (Base + Driver + Suite) without complex inheritance objects.
