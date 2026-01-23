# Git Stack Tool Design

## Philosophy

The `git-stack` tool is designed to bridge the gap between "stacked diff" development workflows locally and "pull request" workflows remotely. It specifically leverages `git-branchless` for local management and `.git/machete` for dependency definition, focusing its own logic purely on **Remote Synchronization** and **Pull Request Management**.

### The Triad

1.  **Physics: `git-branchless`**
    *   **Role**: Manages the local commit graph.
    *   **Responsibility**: Handles restacking, moving commits, and crucially, *automatically updating branch pointers* when underlying commits are rewritten (amended/rebased).
    *   **Implication**: Our tool assumes that if a branch `feature-a` exists locally, `git-branchless` has kept it pointing to the correct rewritten commit. We treat local git refs as the Source of Truth.

2.  **Topology: `.git/machete`**
    *   **Role**: Defines the logical dependency tree.
    *   **Responsibility**: Records that `feature-b` depends on `feature-a`, and `feature-a` depends on `main`.
    *   **Implication**: We rely on this file format for the "Plan". We do not store commit hashes or state in this file, only relationships.

3.  **Distribution: `git-stack` (This Tool)**
    *   **Role**: Interaction with the Remote Forge (GitHub/GitLab/Codeberg).
    *   **Responsibility**: 
        *   Pushing local branches to remote.
        *   Creating/Updating PRs.
        *   Ensuring PR target branches (bases) match the topology.
        *   Updating PR descriptions (Stack Navigation).

## Core Workflows

### 1. Development (Native)
The user works primarily using `git-branchless` workflows. Commits can be anonymous or named. The user focuses on code and commit structure.

### 2. Definition (Slice)
When ready to share or sync, the user runs `git stack slice`.
*   **Input**: A range of commits (stack).
*   **Interaction**: User assigns branch names to specific commits in the stack.
*   **Output**: 
    1.  Local git branches are created or moved to valid commits.
    2.  `.git/machete` is generated/updated to reflect the linear or tree dependency.
*   **Note**: This is the only time "Commit Hashes" matter explicitly to the tool logic, just to set the initial refs.

### 3. Synchronization (Sync)
The user runs `git stack sync`.
*   **Goal**: Ensure remote branches match local branches.
*   **Process**:
    1.  **Traverse**: Iterate through the branch tree defined in machete file.
    2.  **Push**: For every branch, compare `local_ref` vs `remote_ref`. Force push if different.

### 4. Creation (Create)
The user runs `git stack create`.
*   **Goal**: Ensure PRs/MRs exist for all branches.
*   **Process**:
    1.  Check if PR exists for each branch.
    2.  If not, create it (targeting the correct base from machete).
    3.  If base is incorrect, update it.

### 5. Documentation (Anno)
The user runs `git stack anno`.
*   **Goal**: Update PR descriptions with navigation tables.
*   **Input**: `.git/machete`
*   **Process**:
    1.  Fetch all open PRs for the stack.
    2.  Generate a "Stack Table" (Linear Chain).
    3.  Update each PR's body with the table.


## Architecture Decisions

*   **Statelessness**: The tool attempts to be stateless. It effectively "re-compiles" the state of the world by looking at `.git/machete` and current Git Refs every time it runs.
*   **Platform Agnostic**: Core logic handles the "What", Platform Adapters handle the "How".
    *   Support: GitHub (Priority), GitLab, Codeberg (Gitea/Forgejo).
*   **One-Way Data Flow (Conceptually)**:
    *   Local Physics -> Local Refs -> Remote Refs -> PR State.
    *   We rarely read back complex state from remote to modify local, except perhaps for checking CI status (future).

## Configuration
*   **File**: `.git/machete` (Standard format)
*   **Annotations**: Used for additional metadata if necessary (e.g., `PR #123` or `automerge=yes`), though platform APIs are preferred for retrieving PR numbers.

## Non-Goals
*   Reimplementing `rebase` or `restack` logic (Use `git-branchless`).
*   Complex local branch management UI (Use `git-machete` CLI or `git-branchless` UI if needed, though `slice` provides a basic wizard).
