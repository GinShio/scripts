"""Interactive stack slicing module."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

from .git import get_config, get_refs_map, get_stack_prefix, run_git, slugify
from .machete import MacheteNode, parse_machete, write_machete


def get_stack_commits(base_branch: str) -> List[Tuple[str, str]]:
    """Get commits from base to HEAD in reverse order (oldest first)."""
    out = run_git(["log", "--reverse", "--pretty=format:%H %s", f"{base_branch}..HEAD"])
    if not out:
        return []
    lines = out.split("\n")
    commits = []
    for line in lines:
        parts = line.split(" ", 1)
        if len(parts) == 2:
            commits.append((parts[0], parts[1]))
        elif len(parts) == 1:
            commits.append((parts[0], ""))
    return commits


def launch_interactive_editor(
    base_branch: str, commits: List[Tuple[str, str]]
) -> Dict[str, str]:
    """
    Launch editor. Returns mapping of commit_hash -> branch_name.
    """
    stack_prefix = get_stack_prefix()

    instructions = f"""
# Interactive Stack Slice
#
# Define branches for your stack ({base_branch}..HEAD).
# The stack is displayed from oldest (top) to newest (bottom).
#
# Syntax:
#   # <commit-hash> <subject>
#   branch <name>
#
# Commands available:
#   branch, b <name>  : Create/Update a branch pointing to the commit above
#
# Uncomment suggestion lines to quickly create branches.
"""
    initial_content = instructions + "\n"
    for cm, subject in commits:
        initial_content += f"# {cm} {subject}\n"
        suggested_name = f"{stack_prefix}{slugify(subject)}"
        initial_content += f"# branch {suggested_name}\n"
        initial_content += "\n"

    # Editor resolution
    editor = os.environ.get("GIT_EDITOR") or os.environ.get("EDITOR")
    if not editor:
        editor = get_config("core.editor", "vim")

    with tempfile.NamedTemporaryFile(
        suffix=".stack-slice", mode="w+", delete=False
    ) as tf:
        tf.write(initial_content)
        temp_path = tf.name

    try:
        # Use simple shell execution for editor string (handles args like 'code --wait')
        subprocess.check_call(f"{editor} {temp_path}", shell=True)
        with open(temp_path, "r") as f:
            lines = f.readlines()
    finally:
        os.remove(temp_path)

    # Parse result
    commit_branch_map = {}
    current_commit = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Try to identify commit context line: "# <hash> <subject>"
        # or just "# <hash> ..."
        # Git short hash is usually >= 7 chars.
        # Strict regex for our format: # [0-9a-f]{7,} .*

        match_commit = re.match(r"^#\s+([0-9a-f]{7,40})\b", line)
        if match_commit:
            # Verify it's one of our stack commits to avoid parsing random comments
            found_hash = match_commit.group(1)
            full_commit = next(
                (c for c, _ in commits if c.startswith(found_hash)), None
            )
            if full_commit:
                current_commit = full_commit
            continue

        if line.startswith("#"):
            continue

        parts = line.split()
        if not parts:
            continue

        cmd = parts[0]
        if cmd in ("branch", "b") and len(parts) > 1:
            if current_commit:
                branch_name = parts[1]
                commit_branch_map[current_commit] = branch_name
            else:
                print(
                    f"Warning: 'branch' command found before any commit context: {line}"
                )

    return commit_branch_map


def apply_slice(base_branch: str, commit_branch_map: Dict[str, str]) -> None:
    """
    Creates/Moves branches and updates machete file logic.
    """
    if not commit_branch_map:
        print("No branches defined. Aborting.")
        return

    # 1. Create/Move Git Branches
    for commit, branch_name in commit_branch_map.items():
        print(f"Pointing {branch_name} to {commit[:7]}...")
        run_git(["branch", "-f", branch_name, commit])

    # 2. Update Machete (Smart Merge)
    # Goal: Replace the subtree starting at 'base_branch' (or append it).
    # But since we are defining a linear stack on top of base_branch, we want
    # to find base_branch in the existing tree, remove its EXISTING children
    # (that are part of the stack? or all?), and insert our new linear chain.

    existing_nodes = parse_machete()
    all_roots = []

    # ---------------------------------------------------------
    # CLEANUP LOGIC: Identify and remove orphaned branches
    # ---------------------------------------------------------
    # We define the "scope" of the stack operation as the commits
    # currently reachable from base_branch..HEAD (before we moved anything?
    # No, we already moved branches in step 1. But the commits objects exist.)

    # Actually, we already updated branches in Step 1.
    # So 'commits' list below will reflect the NEW state if we query now?
    # get_stack_commits uses `base_branch..HEAD`.
    # HEAD is likely pointing to the top of the stack.
    # If we created new branches, they point to these commits.
    # If we abandoned old branches, they point to the SAME commits (or similar).

    # To find orphans, we need to know which branches *used* to point to these commits
    # AND were managed (in machete).
    # Since we moved valid branches in Step 1, the valid ones are fine.
    # The abandoned ones still point to their old commits (same hashes).

    commits = get_stack_commits(base_branch)
    scope_hashes = {c[0] for c in commits}

    refs = get_refs_map()
    candidate_branches = {b for b, sha in refs.items() if sha in scope_hashes}

    # Only consider branches that were already known in Machete (managed branches)
    # This prevents deleting random local branches user might have that happen to be here.
    managed_candidates = candidate_branches.intersection(existing_nodes.keys())

    new_active_branches = set(commit_branch_map.values())

    orphans = managed_candidates - new_active_branches

    # Exclude base_branch just in case
    if base_branch in orphans:
        orphans.remove(base_branch)

    if orphans:
        print(f"Cleaning up orphaned branches: {', '.join(orphans)}")
        for orphan in orphans:
            run_git(["branch", "-D", orphan], check=False)
            # Remove from existing_nodes so we don't write it back
            if orphan in existing_nodes:
                del existing_nodes[orphan]
    # ---------------------------------------------------------

    # We need to reconstruct the list of roots to leverage write_machete logic
    # or manipulate the dict. Manipulating dict is easier if we have links.

    # Helper: Convert dict back to list of roots for safety?
    # get_roots does that.

    # Find base_branch node
    base_node = existing_nodes.get(base_branch)

    if not base_node:
        # Base branch not in file. Create new root.
        base_node = MacheteNode(base_branch)
        existing_nodes[base_branch] = base_node
        # We need to know if this new node is a root. Yes.

    # Identify the new branches we created (ordered)
    # We must order them by commit age (oldest first) to form the chain
    commits = get_stack_commits(base_branch)
    new_chain_names = []
    for c, s in commits:
        if c in commit_branch_map:
            new_chain_names.append(commit_branch_map[c])

    if not new_chain_names:
        return

    # Remove existing children of base_node that conflict?
    # Actually, we want to replace the path.
    # If base_branch had children: A, B.
    # And we are now saying base_branch -> New1 -> New2.
    # Should we keep A and B as siblings of New1?
    # Usually in a stack workflow, we are updating "The Stack".
    # Assuming 'base_branch' (e.g. main) might have multiple stacks.
    # But if we are "Slicing THIS stack", we might be replacing the old definition of THIS stack.

    # Heuristic:
    # 1. Build the new chain as MacheteNodes.
    # 2. Add the first new node as a child of base_branch.
    # 3. Warning: If base_branch already has this child, we reuse it?
    #    If base_branch has OTHER children, we leave them alone (siblings).

    # Let's handle logical updates.
    # Base -> [OtherStack, OldBranch1 -> OldBranch2]
    # We want Base -> [OtherStack, NewBranch1 -> NewBranch2]
    # If NewBranch1 name == OldBranch1 name, we effectively update the subtree.

    # Deduplicate chain names to prevent self-looping (A -> A)
    # We only care about transitions.
    # [A, A, B, B, C] -> [A, B, C]
    unique_chain_names = []
    if new_chain_names:
        unique_chain_names.append(new_chain_names[0])
        for name in new_chain_names[1:]:
            if name != unique_chain_names[-1]:
                unique_chain_names.append(name)

    new_chain_names = unique_chain_names

    current_parent = base_node

    for branch_name in new_chain_names:
        # Check if node exists anywhere in the tree already
        existing_node = existing_nodes.get(branch_name)

        if existing_node:
            node = existing_node
            # Check if we need to reparent
            if node.parent != current_parent:
                # Detach from old parent
                if node.parent:
                    # Remove from old parent's children list
                    if node in node.parent.children:
                        node.parent.children.remove(node)

                # Attach to new parent (if not already there - though we just checked !=)
                node.parent = current_parent
                current_parent.children.append(node)

            # If parent is same, verify order? Machete list is ordered.
            # But simplistic append is fine for now.
            # If strict ordering is needed, we might need to remove and re-append to end,
            # but usually slice defines the sequence.

        else:
            # Create new
            node = MacheteNode(branch_name)
            node.parent = current_parent
            current_parent.children.append(node)
            existing_nodes[branch_name] = node  # Update lookup

        current_parent = node

    # Write back
    write_machete(existing_nodes)
    print(f"Updated .git/machete.")
