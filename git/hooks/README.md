# Ginshio Git Hooks Configuration Guide

This repository contains global Git hooks designed to be POSIX-compliant, modular, and configurable.

## Configuration Hierarchy

You can enable/disable hooks or configure usage through `git config` (local or global) or Environment Variables.

### 1. Global Disable
To disable **ALL** hooks provided by this framework:

*   **Git Config**: `hooks.ginshio.disable` (boolean)
    ```bash
    git config --global hooks.ginshio.disable true
    ```
*   **Env Var**: `GINSHIO_HOOKS_DISABLE_ALL=true`

### 2. Hook Level Disable
To disable a specific hook type (e.g., `pre-commit`):

*   **Git Config**: `hooks.ginshio.<HOOK_NAME>.disable`
    ```bash
    git config --local hooks.ginshio.pre-commit.disable true
    ```
*   **Env Var**: `GINSHIO_HOOKS_<HOOK_NAME>_DISABLE` (e.g., `GINSHIO_HOOKS_PRE_COMMIT_DISABLE=true`)

### 3. Script Level Disable
To disable a specific script within a hook directory (e.g., `code-formatter` in pre-commit):

*   **Git Config**: `hooks.ginshio.<HOOK_NAME>.<SCRIPT_NAME>-disable`
    ```bash
    git config --local hooks.ginshio.pre-commit.code-formatter-disable true
    ```
*   **Env Var**: `GINSHIO_HOOKS_<HOOK_NAME>_<SCRIPT_NAME>_DISABLE`

    Use the clean name without the numeric prefix (e.g., `CODE_FORMATTER` for `85-code-formatter`).
    ```bash
    export GINSHIO_HOOKS_PRE_COMMIT_CODE_FORMATTER_DISABLE=true
    ```

### 4. External Hooks Integration
Integrate third-party or project-specific Git hooks into the execution pipeline seamlessly. It supports both automatic directory scanning (for tools like Husky) and explicit script mapping (for custom legacy scripts).

#### 4.1 Directory Scanning (e.g., Husky, .githooks)
Provide a colon-separated (`:`) list of directories. It supports both single file hooks (`.husky/pre-commit`) and split `.d` directories (`.githooks/pre-commit.d/`). Paths can be absolute, or relative to the repository root.

*   **Git Config**: `hooks.ginshio.external-dirs`
    ```bash
    git config --local hooks.ginshio.external-dirs ".husky:.githooks"
    ```
*   **Env Var**: `GINSHIO_HOOKS_EXTERNAL_DIRS`
    ```bash
    export GINSHIO_HOOKS_EXTERNAL_DIRS=".husky:.githooks:/opt/shared/hooks"
    ```
*   **Disable**: Disable all directory-based external hooks.
    ```bash
    export GINSHIO_HOOKS_PRE_COMMIT_EXTERNAL_DISABLE=true
    ```

#### 4.2 Explicit Script Mapping (e.g., scripts/lint.sh)
If a project uses custom script paths that don't follow the `DIR/HOOK_NAME` structure, you can explicitly map them to run during specific Git hooks using a colon-separated (`:`) list of executable paths.

*   **Git Config**: `hooks.ginshio.<HOOK_NAME>.external-scripts`
    ```bash
    # Run two different scripts during the pre-commit phase
    git config --local hooks.ginshio.pre-commit.external-scripts "scripts/lint.sh:tools/check-format"
    ```
*   **Env Var**: `GINSHIO_HOOKS_<HOOK_NAME>_EXTERNAL_SCRIPTS`
    ```bash
    export GINSHIO_HOOKS_PRE_PUSH_EXTERNAL_SCRIPTS="scripts/ci-dry-run.py"
    ```
*   **Disable**: Explicit scripts participate in the exact same disable hierarchy natively via their script name. (e.g., disabling `scripts/lint.sh` during `pre-commit`):
    ```bash
    export GINSHIO_HOOKS_PRE_COMMIT_LINT_SH_DISABLE=true
    ```

### 5. Logging and Debugging
Control log verbosity. Levels: `0` (OFF), `1` (ERROR), `2` (WARN, Default), `3` (INFO).

*   **Env Var**: `GINSHIO_HOOKS_LOG_LEVEL`
    ```bash
    export GINSHIO_HOOKS_LOG_LEVEL=3
    ```

## Feature Flags

Specific scripts may have their own feature flags.

### Protected Branches Warning (`warn-protected`)
Prompts for confirmation when committing/pushing to protected branches (master, dev, release-*, patch-*).

*   **Pre-Commit**: `hooks.ginshio.pre-commit.warn-protected-enabled` (boolean). Default: false.
    ```bash
    git config --local hooks.ginshio.pre-commit.warn-protected-enabled true
    ```
*   **Pre-Push**: `hooks.ginshio.pre-push.warn-protected-enabled` (boolean). Default: false.
    ```bash
    git config --local hooks.ginshio.pre-push.warn-protected-enabled true
    ```

### Polyglot Code Formatter (`code-formatter`)
Automatically formats staged files for supported languages (C/C++, Rust, Zig). This script consolidates multiple formatters for efficiency.

*   **C/C++ (clang-format)**:
    *   **Enable**: `hooks.ginshio.pre-commit.clang-format-enabled` (boolean). Default: **true**.
        ```bash
        git config --local hooks.ginshio.pre-commit.clang-format-enabled false
        ```
    *   **Style**: `hooks.ginshio.pre-commit.clang-format-style` (string).
        ```bash
        git config --local hooks.ginshio.pre-commit.clang-format-style llvm
        ```

*   **Rust (rustfmt)**:
    *   **Enable**: `hooks.ginshio.pre-commit.rust-fmt-enabled` (boolean). Default: **true**.
        ```bash
        git config --local hooks.ginshio.pre-commit.rust-fmt-enabled false
        ```

*   **Zig (zig fmt)**:
    *   **Enable**: `hooks.ginshio.pre-commit.zig-fmt-enabled` (boolean). Default: **true**.
        ```bash
        git config --local hooks.ginshio.pre-commit.zig-fmt-enabled false
        ```

*   **Python**:
    *   **Enable**: `hooks.ginshio.pre-commit.python-fmt-enabled` (boolean). Default: **true**.
    *   **Tools**: Auto-detects `ruff` (preferred, comprehensive) or falls back to `black` + `isort`.
        ```bash
        git config --local hooks.ginshio.pre-commit.python-fmt-enabled false
        ```

*   **Generic Whitespace**:
    *   **Enable**: `hooks.ginshio.pre-commit.whitespace-enabled` (boolean). Default: **true**.
    *   **Features**: Trims trailing whitespace, ensures newline at EOF.
    *   **Scope**: All text files.
        ```bash
        git config --local hooks.ginshio.pre-commit.whitespace-enabled false
        ```

### Build Directory Cleanup (`cleanup-build-dir`)
*Hook: reference-transaction*
Automatically detects when a local branch is deleted (`git branch -d ...`) and queries the external builder script to find and remove the associated build directory.

*   **Enable**: `hooks.ginshio.reference-transaction.cleanup-build-dir-enabled` (boolean). Default: false.
    ```bash
    git config --local hooks.ginshio.reference-transaction.cleanup-build-dir-enabled true
    ```

### Security Scan (`security-scan`)
*Hook: pre-commit*
Scans staged files for secrets/credentials using available tools.
*   **Tools**: Auto-detects `gitleaks` (preferred) or `git-secrets`.
*   **Behavior**: Blocks commit if secrets are found.
    *   **Gitleaks bypass**: `git commit --no-verify`.

### Encoding Check (`encoding`)
*Hook: pre-commit*
Enforces text encoding and newline style.
*   **Allowed**: `ascii-unix`, `utf8-unix` (LF line endings only).
*   **Behavior**: Blocks files with CRLF or non-UTF8 encodings.

### Git LFS (`git-lfs`)
*Hook: pre-push*
Wraps `git lfs pre-push` to ensure Large File Storage is synchronized.
*   **Requirement**: `git-lfs` command line tool must be available.

### Issue Tracker Autocomplete (`issue-tracker`)
*Hook: prepare-commit-msg*

**[Disabled by Default / Opt-in]** Automatically extracts an Issue ID from your **current branch name** and prepends it to empty or new commit messages. 
For example, if your branch is named `feature/PROJ-123-login`, the script will automatically extract `PROJ-123` and inject `[PROJ-123] ` at the beginning of your commit message when the editor opens.

*   **Enable (`enabled`)**: To avoid polluting community or personal projects, this hook is strictly opt-in. You must enable it explicitly.
    ```bash
    git config --local hooks.ginshio.prepare-commit-msg.issue-tracker-enabled true
    # Or via Env:
    export GINSHIO_HOOKS_PREPARE_COMMIT_MSG_ISSUE_TRACKER_ENABLED=true
    ```
*   **Regex (`regex`)**: Used to "Search & Extract" the Issue ID from the branch name. 
    *   *Default*: `[A-Z]+-[0-9]+` (Captures "PROJ-1234").
    ```bash
    # Example: If your branches are named "bugfix/1234", capture only the numbers:
    git config --local hooks.ginshio.prepare-commit-msg.issue-tracker-regex "[0-9]+"
    ```
*   **Format (`format`)**: Controls how the extracted ID is formatted and injected into your commit message using `printf` style (where `%s` is replaced by the ID). 
    *   *Default*: `[%s] ` (which results in `[PROJ-1234] `)
    ```bash
    # Example: Format it as "(PROJ-1234): "
    git config --local hooks.ginshio.prepare-commit-msg.issue-tracker-format "(%s): "
    ```
*   **Default Fallback (`default`)**: If the current branch name does *not* match the Regex (e.g., you are on `master` or `dev`), you can configure a fallback Issue ID to be used automatically.
    ```bash
    # Example: fallback to a generic placeholder or specific tracking ticket
    git config --local hooks.ginshio.prepare-commit-msg.issue-tracker-default "GENERAL-001"
    ```

### Polyglot Code Linter (`linter`)
*Hook: pre-commit*
Analyzes code for bugs exactly like `code-formatter` but purely for finding errors before compiling. Validates currently staged logic without enforcing noisy checks. 

*   **Python**: Fast AST/semantic linting using `ruff` (fallback to `flake8`). Checks against the specific staged files.
    *   **Enable**: `hooks.ginshio.pre-commit.python-lint-enabled` (boolean). Default: **true**.

*   **Zig**: Utilizes purely native `zig ast-check` for blazingly fast syntactic verification that requires zero dependency resolution.
    *   **Enable**: `hooks.ginshio.pre-commit.zig-lint-enabled` (boolean). Default: **true**.

*   **Rust**: Automatically triggers a check (`cargo clippy -- -D warnings`). Note: Since Rust is heavily macro & dependency driven, it does require dependencies to be present, but falls back gracefully if rust is disabled or unavailable.
    *   **Enable**: `hooks.ginshio.pre-commit.rust-lint-enabled` (boolean). Default: **true**.

## Directory Structure

*   `hooks/core/`: Core library and runner.
*   `hooks/<HOOK_NAME>.d/`: Directory containing scripts for that hook.
*   `hooks/<HOOK_NAME>`: Symlink to `core/runner`.

### Sanity Checks (`sanity-checks`)
Performs basic health checks on committed files:
1.  **Merge Conflicts**: Blocks files containing `<<<<<<<`, `=======`, `>>>>>>>`.
2.  **Broken Symlinks**: Blocks symbolic links that point to non-existent targets.
3.  **Large Files**: Blocks files larger than the configured limit (default: 25MiB).

**Configuration**:
*   **Max File Size** (in bytes):
    *   **Git Config**: `hooks.ginshio.pre-commit.sanity-checks-max-file-size`
    *   **Env Var**: `GINSHIO_HOOKS_PRE_COMMIT_SANITY_CHECKS_MAX_FILE_SIZE`
    *   *Default*: 26214400 (25MiB)
