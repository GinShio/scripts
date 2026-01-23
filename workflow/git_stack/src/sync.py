"""Sync module for pushing branches and managing PRs."""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Set, Tuple

from .git import get_current_branch, get_refs_map, resolve_base_branch, run_git
from .machete import MacheteNode, get_roots, parse_machete
from .platform import PlatformInterface, get_platform


def push_branch(branch: str, check: bool = False) -> bool:
    """
    Force push branch to origin.
    Returns True if pushed, False if already up to date (or failed if check=False).
    """
    # Check if we need to push?
    # git push -f origin branch
    # To optimize, we could check remote hash first, but force pushing is safe
    # for stacks (lease is better).
    print(f"Pushing {branch}...")
    try:
        # --force-with-lease is safer, but if user rebased local, lease might fail if
        # remote has moved strictly forward (unlikely in single-user stack).
        # We use simple force for now as per requirement.
        run_git(["push", "origin", branch, "--force-with-lease"], check=True)
        return True
    except Exception as e:
        print(f"Failed to push {branch}: {e}", file=sys.stderr)
        if check:
            raise
        return False


def sync_stack(
    push: bool = True,
    pr: bool = False,
    dry_run: bool = False,
    limit_to_branch: Optional[str] = None,
) -> None:
    """
    Sync code by pushing local branches to remote, and optionally manage PRs.
    Traverses the Machete tree mostly once (conceptually).

    Args:
        push: Whether to push branches.
        pr: Whether to sync PRs.
        dry_run: No-op mode.
        limit_to_branch: If provided, only sync the stack containing this branch.
    """
    nodes = parse_machete()
    if not nodes:
        print("No .git/machete definition found.")
        return

    # Filter roots if limit_to_branch is set
    roots = get_roots(nodes)
    targets = list(nodes.values())

    if limit_to_branch:
        if limit_to_branch not in nodes:
            print(f"Branch '{limit_to_branch}' not found in stack definition.")
            sys.exit(1)

        from .machete import get_linear_stack

        linear_stack = get_linear_stack(limit_to_branch, nodes)

        if not linear_stack:
            print("Empty stack found.")
            return

        targets = linear_stack

        # Re-determine roots just for the traversal entry point
        # The "roots" of our operation is just the top of the chain
        roots = [linear_stack[0]]
        print(f"Limiting sync to linear stack: {[n.name for n in targets]}")

    refs = get_refs_map()

    # Resolve base to identify special roots
    stack_base = resolve_base_branch()

    platform = None
    if pr:
        platform = get_platform()
        if not platform:
            print("Warning: Could not detect git platform. PR creation skipped.")
            pr = False

    # Set of target names for O(1) lookup
    target_names = {t.name for t in targets}

    # Collecting tasks
    push_tasks: List[str] = []
    pr_tasks: List[Tuple[str, str]] = []

    def collect_tasks(node: MacheteNode, parent: Optional[MacheteNode]):
        branch = node.name

        # If limiting, skip if not in our target set
        if limit_to_branch and branch not in target_names:
            return

        is_root_special = branch == stack_base

        if branch not in refs:
            # Local branch doesn't exist? Then we can't push it.
            pass
        else:
            # 1. Push
            if push and not is_root_special:
                push_tasks.append(branch)

            # 2. PR
            if pr and not is_root_special:
                if parent and platform:
                    pr_tasks.append((branch, parent.name))

        for child in node.children:
            collect_tasks(child, node)

    for root in roots:
        collect_tasks(root, None)

    # Execute Push Tasks
    if push_tasks:
        if dry_run:
            print(f"Would push branches: {', '.join(push_tasks)}")
        else:
            print(f"Pushing {len(push_tasks)} branches in parallel...")
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(push_branch, branch): branch
                    for branch in push_tasks
                }
                for future in as_completed(futures):
                    branch = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        print(f"Push task for {branch} generated an exception: {exc}")

    # Execute PR Tasks
    if pr_tasks:
        if dry_run:
            print(f"Would sync PRs for: {', '.join([b for b, p in pr_tasks])}")
        else:
            print(f"Syncing {len(pr_tasks)} PRs in parallel...")

            def do_pr_sync(payload):
                branch, parent_name = payload
                is_stack_base = parent_name == stack_base
                # Fallback check if parent is root but not explicit base (upstream)
                # But since we passed string names, we rely on the logic used during collection or here
                # In collect_tasks we passed `parent.name`.
                # Let's verify `is_stack_base` logic.
                # In previous code:
                # if parent and parent.name == stack_base: is_stack_base = True
                # elif parent and parent.parent is None: is_stack_base = True

                # We need to access node info or re-check.
                # Simplest is to assume parent_name == stack_base is the main trigger for Open PR.
                # If we want to support "upstream" roots that aren't strict stack_base string,
                # we might miss them here.
                # Re-lookup node for safer check?
                node = nodes.get(branch)
                parent_node = nodes.get(parent_name)

                real_is_stack_base = False
                if parent_name == stack_base:
                    real_is_stack_base = True
                elif parent_node and parent_node.parent is None:
                    real_is_stack_base = True

                is_draft = not real_is_stack_base

                try:
                    # Platform interface must be thread-safe (requests is thread-safe)
                    if platform:
                        platform.sync_mr(branch, parent_name, draft=is_draft)
                except Exception as e:
                    print(f"PR sync failed for {branch}: {e}", file=sys.stderr)

            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(do_pr_sync, task): task for task in pr_tasks}
                for future in as_completed(futures):
                    pass  # Exceptions printed in thread function


def push_stack(dry_run: bool = False) -> None:
    """Deprecated: Use sync_stack(push=True, pr=False)."""
    sync_stack(push=True, pr=False, dry_run=dry_run)


def create_stack_prs(dry_run: bool = False) -> None:
    """Deprecated: Use sync_stack(push=False, pr=True)."""
    sync_stack(push=False, pr=True, dry_run=dry_run)
