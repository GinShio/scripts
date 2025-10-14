# Git System Technical Documentation

This document describes the Git-related functionality in the system, including update processes, Submodule handling, and error management.

---

## Table of Contents

1. [Core Design Principles](#core-design-principles)
2. [Update Command Suite](#update-command-suite)
3. [Update Process Logic](#update-process-logic)
4. [Working Tree State Handling](#working-tree-state-handling)
5. [Submodule Handling](#submodule-handling)
6. [Custom Script Support](#custom-script-support)
7. [Error Scenarios and Solutions](#error-scenarios-and-solutions)

---

## Core Design Principles

### Design Principles

1. **Intelligent Updates**: Automatically detect repository states and execute appropriate update actions.
2. **Branch Awareness**: Correctly handle components with different branch strategies.
3. **Working Tree Safety**: Ensure user changes are not lost during updates.
4. **Zero-Configuration Defaults**: Provide sensible defaults while allowing optional customization.

---

## Update Command Suite

```shell
# Update a specific project and recursively update all Submodules
builder update myapp

# Update all projects
builder update --all

# Specify a branch for the update (for components or main project)
builder update -b feature-x myapp

# Preview Git activity without executing it
builder update -n myapp

Short aliases:

- `-b` selects the branch to update.
- `-s` picks the submodule strategy (`default`, `latest`, `skip`).
- `-n` previews Git work without executing it.
```

---

## Update Process Logic

### Overall Update Process

```fundamental
builder update <project>
├── Detect project type and repository state
├── If repository does not exist → Perform clone process
├── If repository exists → Perform update process
└── Restore working environment
```

---

### Clone Process

If the repository does not exist, the system performs the following steps:

```fundamental
Clone Process:
├── If clone_script is configured
│   └── Execute the custom clone script
└── If no clone_script is configured
    ├── git clone <url> <source_dir>
    ├── Switch to the main branch
    └── Initialize and update all Submodules
```

**Example Command**:
```shell
git clone $PROJECT_URL $PROJECT_SOURCE_DIR --recursive
```

---

### Update Process

If the repository exists, the following update process is executed:

```fundamental
Update Process:
├── Check working tree state
├── If necessary and auto_stash is true → Stash changes; otherwise, report error
├── Fetch remote updates (git fetch --all)
├── Update main branch (git checkout origin/main_branch)
├── Update components based on project type
└── Restore working environment
```

**Example Command**:
```shell
cd $PROJECT_SOURCE_DIR
git fetch --all
git stash push # If auto_stash is enabled
git checkout origin/main
git submodule update --recursive
git checkout $PROJECT_ORIG_BRANCH && git stash pop # If auto_stash is enabled
```

For monorepo projects or Submodule components:
```shell
cd $PROJECT_SOURCE_DIR
git fetch --all
git checkout origin/$PROJECT_MAIN_BRANCH
git submodule update --recursive
cd $PROJECT_COMPONENT_DIR
git fetch --all
git stash push # If auto_stash is enabled
git checkout origin/$PROJECT_COMPONENT_BRANCH
git submodule update --recursive
git checkout $PROJECT_ORIG_BRANCH && git stash pop # If auto_stash is enabled
```

### Branch Management Flags

- `builder build` and `builder list` temporarily switch branches to gather state. Add `--no-switch-branch` to skip
   temporary checkouts while still planning or inspecting repositories. This is helpful when uncommitted work should
   remain on the current branch.
- The `builder update` command always ensures the requested branch is fetched; pair it with `auto_stash = true` for
   safe automation when local changes exist.

---

## Working Tree State Handling

### Auto Stash Mechanism

```toml
[git]
auto_stash = false  # Default: false
```

#### Stash Workflow:
1. Check if the working tree has uncommitted changes.
2. If `auto_stash = true` and there are changes:
   - Execute `git stash push -m "builder auto-stash"`.
   - Record the stash identifier.
3. Perform the update operation.
4. If changes were stashed:
   - Switch back to the original branch.
   - Execute `git stash pop` to restore changes.

#### Error Example (When auto_stash is false):
```
Error: Uncommitted changes prevent the update.
Solutions:
- Commit or stash changes manually.
- Set [git] auto_stash=true in your configuration file to automate stashing.
```

---

## Submodule Handling

### Default Submodule Behavior

- After all Git operations, automatically execute:
  ```shell
  git submodule update --recursive
  ```
- Ensure all Submodules match the versions recorded in the main repository.

---

### Submodule Update Strategies

Users can specify different Submodule update behaviors using the `--submodule` parameter:

```shell
builder update -s <strategy> myapp
```

#### Supported Strategies:
1. **`default`** (Default Behavior):
   - Update Submodules to match the versions recorded in the main repository.
2. **`latest`**:
   - Update all Submodules to their latest versions.
3. **`skip`**:
   - Skip all Submodule updates.

---

## Repository Inspection

Use the `builder list` command to audit repository status without performing updates:

```shell
# Summarize every configured project
builder list

# Focus on a single project
builder list myapp

# Include presets, dependency edges, or remote URLs
builder list --presets --dependencies --url
```

- Builder temporarily checks out the requested branch to gather accurate metadata. Add `--no-switch-branch` to avoid
  temporary branch switches (useful for very large workspaces or when working tree changes must remain untouched).
- Submodule rows appear immediately beneath their parent project, mirroring `git submodule status` output. Pass
  `--dependencies` or `--presets` to repurpose the listing for configuration audits instead of Git inspection.

---

### Component Branch Handling

- `component_branch` is considered the primary branch for each component and must always exist.
- If the branch is not found, the system reports an error:
  ```
  Error: Component branch "develop" does not exist for Submodule "libcore".
  Hint: Ensure the branch exists or update the configuration.
  ```

---

## Custom Script Support

### Supported Scripts

```toml
[git]
clone_script = "{{project.source_dir}}/scripts/bootstrap.sh"
update_script = "{{project.source_dir}}/scripts/bootstrap.sh --update"
```

Both script entries support the full templating language. You can reference values
from `user.*`, `project.*`, `system.*`, `env.*`, or any preset-provided
environment/definition (for example `{{env.SDK_ROOT}}`), and they will be
resolved before execution.

#### Script Execution Workflow:
1. If a custom script is configured, execute it.
2. If the script fails, handle the failure as a Git operation failure (e.g., `git fetch`).
3. Log the failure and exit the process with an appropriate error code.

---

## Error Scenarios and Solutions

### Common Errors and Messages

#### Uncommitted Changes
```
Error: Uncommitted changes prevent the update.
Solutions:
- Commit or stash changes manually.
- Set [git] auto_stash=true in your configuration file to automate stashing.
```

#### Missing Component Branch
```
Error: Component branch "develop" does not exist for Submodule "libcore".
Hint: Ensure the branch exists or update the configuration.
```

#### Script Execution Failure
```
Error: Failed to execute custom update script.
Hint: Check the script's log for details or verify its execution manually.
```

---

## Conclusion

This Git system provides a robust and flexible framework for managing project updates, ensuring working tree safety, and supporting custom workflows. With configurable options like `auto_stash` and `--submodule`, it balances ease of use with advanced customization.
