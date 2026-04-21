"""Interactive stack slicing via git rebase -i."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from typing import Dict, Generator, List, Tuple

from .git import (
    get_config,
    get_current_branch,
    get_refs_map,
    get_stack_prefix,
    run_git,
    slugify,
)
from .machete import MacheteNode, parse_machete, write_machete


def get_stack_commits(base_branch: str) -> List[Tuple[str, str]]:
    """Get commits from base to HEAD in reverse order (oldest first)."""
    out = run_git(["log", "--reverse", "--pretty=format:%H %s", f"{base_branch}..HEAD"])
    if not out:
        return []
    commits = []
    for line in out.split("\n"):
        parts = line.split(" ", 1)
        if len(parts) == 2:
            commits.append((parts[0], parts[1]))
        elif len(parts) == 1:
            commits.append((parts[0], ""))
    return commits


def _get_editor() -> str:
    """Resolve editor following Git's own priority order."""
    return (
        os.environ.get("GIT_EDITOR")
        or get_config("core.editor")
        or os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
        or "vim"
    )


def _build_todo_content(base_branch: str, commits: List[Tuple[str, str]]) -> str:
    """
    Build rebase-todo content that matches git rebase -i format as closely as
    possible.  Suggested branch assignments appear as commented-out update-ref
    lines so the user only needs to uncomment the ones they want.
    """
    stack_prefix = get_stack_prefix()
    n = len(commits)

    commit_lines: List[str] = []
    for commit_hash, subject in commits:
        suggested = f"{stack_prefix}{slugify(subject)}"
        commit_lines.append(f"pick {commit_hash} {subject}")
        commit_lines.append(f"# update-ref refs/heads/{suggested}")
        commit_lines.append("")

    base_short = run_git(["rev-parse", "--short", base_branch])
    top_short = commits[-1][0][:11]
    cmd_word = "command" if n == 1 else "commands"
    footer = (
        f"# Rebase {base_short}..{top_short} onto {base_short} ({n} {cmd_word})\n"
        "#\n"
        "# Commands:\n"
        "# p, pick <commit> = use commit\n"
        "# r, reword <commit> = use commit, but edit the commit message\n"
        "# e, edit <commit> = use commit, but stop for amending\n"
        "# s, squash <commit> = use commit, but meld into previous commit\n"
        '# f, fixup [-C | -c] <commit> = like "squash" but keep only the previous\n'
        "#                    commit's log message, unless -C is used, in which case\n"
        "#                    keep only this commit's message; -c is same as -C but\n"
        "#                    opens the editor\n"
        "# x, exec <command> = run command (the rest of the line) using shell\n"
        "# b, break = stop here (continue rebase later with 'git rebase --continue')\n"
        "# d, drop <commit> = remove commit\n"
        "# l, label <label> = label current HEAD with a name\n"
        "# t, reset <label> = reset HEAD to a label\n"
        "# m, merge [-C <commit> | -c <commit>] <label> [# <oneline>]\n"
        "#         create a merge commit using the original merge commit's\n"
        "#         message (or the oneline, if no original merge commit was\n"
        "#         specified); use -c <commit> to reword the commit message\n"
        "# u, update-ref <ref> = track a placeholder for the <ref> to be updated\n"
        "#                       to this position in the new commits. The <ref> is\n"
        "#                       updated at the end of the rebase\n"
        "#\n"
        "# These lines can be re-ordered; they are executed from top to bottom.\n"
        "#\n"
        "# If you remove a line here THAT COMMIT WILL BE LOST.\n"
        "#\n"
        "# However, if you remove everything, the rebase will be aborted.\n"
        "#\n"
        "# Uncomment 'update-ref refs/heads/<name>' lines above to assign branches.\n"
        "# update-ref is safe for branches used by other worktrees or the current branch.\n"
    )

    return "\n".join(commit_lines) + "\n" + footer


def _make_sequence_editor_script(
    todo_content: str, editor: str, capture_path: str
) -> str:
    """
    Generate a self-contained Python script for use as GIT_SEQUENCE_EDITOR.

    Git calls it as:  <script> <todo-file-path>

    The script replaces git's default todo with our custom content, then opens
    the real editor.  After the editor closes, the final todo is copied to
    ``capture_path`` so that the caller can parse the active ``update-ref``
    lines without having to rely on ``base..HEAD`` after the rebase (which is
    unreliable when the current branch appears as an intermediate update-ref
    target).
    """
    return (
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, shutil\n"
        "\n"
        "TODO_FILE = sys.argv[1]\n"
        f"EDITOR = {editor!r}\n"
        f"INITIAL_CONTENT = {todo_content!r}\n"
        f"CAPTURE_PATH = {capture_path!r}\n"
        "\n"
        "with open(TODO_FILE, 'w') as f:\n"
        "    f.write(INITIAL_CONTENT)\n"
        "\n"
        "subprocess.check_call(f'{EDITOR} {TODO_FILE}', shell=True)\n"
        "\n"
        "# Save final todo so the caller can read the active update-ref lines.\n"
        "shutil.copy(TODO_FILE, CAPTURE_PATH)\n"
    )


@contextmanager
def _temp_path(suffix: str) -> Generator[str, None, None]:
    """Context manager that creates a temp file and deletes it on exit."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _parse_todo_branches(todo_path: str) -> List[str]:
    """
    Parse active (non-commented) ``update-ref refs/heads/<name>`` lines from a
    saved rebase-todo file and return branch names in the order they appear.

    This is the authoritative source for which branches the user assigned and
    in what commit order — independent of where HEAD or the current branch
    ends up after the rebase.
    """
    branches: List[str] = []
    prefix = "update-ref refs/heads/"
    try:
        with open(todo_path, "r") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(prefix):
                    name = stripped[len(prefix) :].strip()
                    if name:
                        branches.append(name)
    except OSError:
        pass
    return branches


def _collect_stack_branches(base_branch: str) -> List[List[str]]:
    """
    After a successful rebase, scan base_branch..HEAD in oldest-first order
    and collect local branches pointing to each commit.

    Returns a list of groups, one group per commit that has at least one branch.
    Within each group, non-current branches come first so they serve as the
    primary chain nodes; the current branch (if present at the same commit)
    comes last and becomes a sibling in the machete tree.

    .. note::
        This function is unreliable when the currently checked-out branch is
        assigned as an intermediate update-ref target, because git may leave
        HEAD at that branch's position, causing commits after it to fall
        outside the ``base..HEAD`` scan range.  Prefer ``_parse_todo_branches``
        when the saved TODO is available.
    """
    commits = get_stack_commits(base_branch)
    if not commits:
        return []

    current_branch = get_current_branch()
    refs: Dict[str, str] = get_refs_map()

    # Inverted map: full_hash -> [branch, ...]  (base_branch excluded)
    hash_to_branches: Dict[str, List[str]] = {}
    for branch, sha in refs.items():
        if branch == base_branch:
            continue
        hash_to_branches.setdefault(sha, []).append(branch)

    groups: List[List[str]] = []
    seen: set = set()
    for commit_hash, _ in commits:
        raw = [b for b in hash_to_branches.get(commit_hash, []) if b not in seen]
        if not raw:
            continue
        for b in raw:
            seen.add(b)
        # Non-current branches first so the first entry drives the chain;
        # current branch last so it becomes a sibling rather than a parent.
        raw.sort(key=lambda b: (b == current_branch, b))
        groups.append(raw)

    return groups


def _update_machete(base_branch: str, branch_groups: List[List[str]]) -> None:
    """
    Update .git/machete to reflect a stack rooted at base_branch.

    branch_groups is a list of groups (one per commit position).  Within each
    group every branch becomes a child of the same parent (i.e. siblings), so
    that branches on the same commit are at the same indentation level.
    The first branch in each group becomes the parent for the next group,
    preserving the linear chain of the primary stack branches.
    """
    if not branch_groups:
        return

    existing_nodes = parse_machete()

    if base_branch not in existing_nodes:
        base_node = MacheteNode(base_branch)
        existing_nodes[base_branch] = base_node
    base_node = existing_nodes[base_branch]

    current_parent = base_node
    seen: set = set()

    for group in branch_groups:
        # Drop duplicates while preserving order.
        group = [b for b in group if b not in seen]
        seen.update(group)
        if not group:
            continue

        chain_head: MacheteNode | None = None  # first node in group → next parent

        for branch_name in group:
            existing_node = existing_nodes.get(branch_name)
            if existing_node:
                node = existing_node
                if node.parent != current_parent:
                    # Detach from old parent.
                    if node.parent and node in node.parent.children:
                        node.parent.children.remove(node)
                    # Attach to new parent.
                    node.parent = current_parent
                    current_parent.children.append(node)
            else:
                node = MacheteNode(branch_name)
                node.parent = current_parent
                current_parent.children.append(node)
                existing_nodes[branch_name] = node

            if chain_head is None:
                chain_head = node

        # The first (primary) branch at this commit is the parent for the
        # next commit's branches.
        current_parent = chain_head  # type: ignore[assignment]

    write_machete(existing_nodes)


def do_slice(base_branch: str) -> None:
    """
    Slice the current stack into branches using git rebase -i.

    Workflow:
      1. Scan base_branch..HEAD to build the initial rebase-todo.
      2. Open the editor (via GIT_SEQUENCE_EDITOR) for the user to edit the
         todo and uncomment desired ``update-ref refs/heads/<name>`` lines.
      3. Let git execute the rebase; branch pointers are set by update-ref at
         the end of the rebase (safe for worktrees and the current branch).
      4. Scan the rebased commits to discover which branches are present, then
         update .git/machete accordingly.
    """
    commits = get_stack_commits(base_branch)
    if not commits:
        print(f"No commits found between {base_branch} and HEAD.")
        return

    editor = _get_editor()
    todo_content = _build_todo_content(base_branch, commits)

    # capture_path outlives script_path, so it is the outer context manager.
    with _temp_path(".slice-capture.todo") as capture_path:
        script_content = _make_sequence_editor_script(
            todo_content, editor, capture_path
        )

        with _temp_path(".slice-editor.py") as script_path:
            with open(script_path, "w") as f:
                f.write(script_content)
            os.chmod(script_path, 0o755)

            env = os.environ.copy()
            env["GIT_SEQUENCE_EDITOR"] = script_path
            result = subprocess.run(["git", "rebase", "-i", base_branch], env=env)
        # script_path deleted here

        if result.returncode != 0:
            print("Rebase failed or was aborted.")
            print("Run `git rebase --abort` if the rebase is still in progress.")
            sys.exit(result.returncode)

        # Use the saved TODO as the authoritative source of branch assignments.
        # This avoids the ``base..HEAD`` scan which breaks when the current branch
        # is assigned to an intermediate commit (git ignores update-ref for the
        # checked-out branch, leaving HEAD at the wrong position).
        todo_branches = _parse_todo_branches(capture_path)
    # capture_path deleted here

    if not todo_branches:
        print("No update-ref lines found in todo — nothing to update in .git/machete.")
        return

    # Each branch from the todo is its own commit-position group (linear chain).
    branch_groups = [[b] for b in todo_branches]
    _update_machete(base_branch, branch_groups)
    all_branches = todo_branches
    print(f"Branches: {', '.join(all_branches)}")
    print("Updated .git/machete.")
