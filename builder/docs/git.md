# Git System Technical Documentation

This document describes how Builder manages Git repositories for projects and optional components, covering the update workflow, submodule handling, and common error scenarios.

---

## Table of Contents

1. [Core Design Principles](#core-design-principles)
2. [Update Command Suite](#update-command-suite)
3. [Update Process Logic](#update-process-logic)
   1. [Clone Process](#clone-process)
   2. [Existing Repository Update](#existing-repository-update)
4. [Working Tree State Handling](#working-tree-state-handling)
5. [Submodule Handling](#submodule-handling)
6. [Custom Script Support](#custom-script-support)
7. [Error Scenarios and Solutions](#error-scenarios-and-solutions)

---

## Core Design Principles

1. **Intelligent Updates** – Detect repository state and select the appropriate workflow automatically.
2. **Branch Awareness** – Support different strategies for the root project and component repositories (submodule or standalone checkout).
3. **Working Tree Safety** – Protect local changes by stashing when allowed, or halt with a clear error when not.
4. **Unified Code Path** – Script-driven and built-in updates share the same switching, stashing, and restore logic to minimize maintenance effort.

---

## Update Command Suite

```shell
# Update a specific project and recursively update all submodules
builder update myapp

# Update all projects
builder update --all

# Specify a branch for the update (root/component)
builder update -b feature-x myapp

# Preview Git activity without executing it
builder update -n myapp
```

Short aliases:

- `-b` selects the branch to update.
- `-s` selects the submodule strategy (`default`, `latest`, `skip`).
- `-n` performs a dry run, printing the commands instead of executing them.

Use fully qualified names (`vendor/myapp`) or the `--org` flag when multiple organizations share the same project name.

---

## Update Process Logic

### Clone Process

When the repository does not exist, Builder executes the clone process:

```fundamental
Clone Process:
├── If clone_script is configured → run the script
└── Otherwise → git clone <url> <source_dir> --recursive
```

After cloning, the repository is ready on the configured main branch with all submodules initialized.

### Existing Repository Update

When the repository already exists, Builder applies the unified update flow:

```fundamental
Existing Repository Update:
├── Step 1: Stash dirty worktrees (root + optional component) when auto_stash = true
├── Step 2: Switch the root repository to main_branch and update it
│   ├── Script mode → run update_script
│   └── Built-in mode → git fetch --all + git merge --ff-only origin/main_branch
│   └── Update root submodules → git submodule update --recursive
├── Step 3: Update the component repository (standalone or submodule)
│   ├── Switch to component_branch if provided; otherwise main_branch
│   ├── git fetch --all + git merge --ff-only origin/<component_branch>
│   └── git submodule update --recursive
├── Step 4: Restore the component (if switched/stashed)
│   ├── Switch back to the original branch and refresh its submodules
│   └── Pop the component stash when one was created
└── Step 5: Restore the root (if switched/stashed)
    ├── Switch back to the original branch and refresh submodules while skipping the component path when it is a submodule
    └── Pop the root stash when one was created
```

Key details:

- Fast-forward merges (`git merge --ff-only origin/<branch>`) keep history linear and avoid implicit merge commits.
- The same stashing/switching logic applies whether an `update_script` is present, so scripts simply replace the fetch/merge step without diverging behavior.
- When the component lives inside the root repo as a submodule, the final root submodule refresh skips that path; the component remains on the freshly updated commit instead of reverting to the recorded SHA.

---

## Working Tree State Handling

```toml
[git]
auto_stash = false  # default
```

1. Builder checks whether the root and component repositories are dirty.
2. If dirty and `auto_stash = true`, Builder runs `git stash push -m "builder auto-stash"` and remembers to pop it later.
3. If dirty and `auto_stash = false`, Builder aborts the update so users can resolve the state manually.
4. Stashes are restored only after the original branches have been switched back.

---

## Submodule Handling

- Root and component updates both run `git submodule update --recursive` while on their respective update branches.
- During root restoration, Builder issues a selective submodule update that skips the component path when it was treated as a submodule. This preserves the component fast-forward.
- CLI submodule strategies (`default`, `latest`, `skip`) continue to be honored by higher-level commands.

---

## Custom Script Support

Scripts keep the same branch and stash orchestration as the built-in logic:

```toml
[git]
clone_script = "{{project.source_dir}}/scripts/bootstrap.sh"
update_script = "{{project.source_dir}}/scripts/bootstrap.sh --update"
```

- `update_script` runs after the root repository is switched to `main_branch` and before submodules are refreshed.
- Script failures are surfaced like Git command failures, leaving any created stashes in place so users can recover safely.

---

## Error Scenarios and Solutions

| Scenario | Message | Resolution |
| --- | --- | --- |
| Uncommitted changes without auto_stash | "Working tree has uncommitted changes; enable auto_stash to proceed" | Commit/stash manually or enable `auto_stash`. |
| Component branch missing | "Unable to switch component repository … to branch" | Ensure the branch exists or update configuration. |
| Fast-forward denied | "Unable to fast-forward …" | Rebase local work onto the remote branch or resolve conflicts manually. |
| Script failure | "Failed to execute custom update script" | Inspect the script logs, fix the issue, and re-run the update. |

---

## Conclusion

The unified update implementation keeps script-driven and built-in workflows aligned, protects local work through stashing, and ensures component repositories stay current without fighting submodule constraints. Use the documented commands and configuration options above to tailor the process to each project.
