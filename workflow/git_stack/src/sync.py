"""Sync module for pushing branches and managing PRs."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Set, Tuple

from .git import (
    get_config,
    get_current_branch,
    get_refs_map,
    resolve_base_branch,
    run_git,
)
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
    title_source: str = "last",
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
        print("Error: No .git/machete definition found. Ensure your stack is defined.")
        sys.exit(1)

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
            # Helper to derive title/body based on title_source
            def derive_title_body(
                branch: str, parent: str
            ) -> Tuple[Optional[str], Optional[str]]:
                # 'first' -> oldest commit subject on branch vs parent
                # 'last' -> newest commit subject on branch vs parent
                if title_source in ("first", "last"):
                    order = "--reverse" if title_source == "first" else ""
                    # Use pretty format to include clear per-commit markers
                    fmt = "==GITSTACK_COMMIT==%n%s%n==GITSTACK_BODY==%n%b"
                    args = [
                        "log",
                        order,
                        f"--pretty=format:{fmt}",
                        f"{parent}..{branch}",
                    ]
                    out = run_git(args, check=False)
                    # If parent..branch produced nothing (e.g. parent missing or
                    # range empty), try to fall back to the branch's HEAD commit
                    # to extract a sensible title/body.
                    if not out:
                        out = run_git(
                            [
                                "show",
                                "-s",
                                f"--pretty=format:{fmt}",
                                branch,
                            ],
                            check=False,
                        )
                        if not out:
                            return (branch, "")

                    # Split per-commit blocks by commit marker
                    raw_chunks = out.split("==GITSTACK_COMMIT==\n")
                    chunks = [c for c in raw_chunks if c.strip()]
                    entries = []
                    for chunk in chunks:
                        lines = chunk.splitlines()
                        if not lines:
                            continue
                        # Prefer explicit body marker. If present, everything after
                        # the marker is the body. The subject is the first
                        # non-empty line before the marker. If marker absent,
                        # fall back to first line = subject, rest = body.
                        body_marker = "==GITSTACK_BODY=="
                        if body_marker in lines:
                            marker_idx = lines.index(body_marker)
                            # subject is first non-empty line before marker
                            subject_candidates = [
                                l for l in lines[:marker_idx] if l.strip()
                            ]
                            subject = (
                                subject_candidates[0] if subject_candidates else ""
                            )
                            body_lines = lines[marker_idx + 1 :]
                        else:
                            subject = lines[0]
                            body_lines = lines[1:]

                        # Strip leading/trailing empty lines from body
                        while body_lines and not body_lines[0].strip():
                            body_lines.pop(0)
                        while body_lines and not body_lines[-1].strip():
                            body_lines.pop()

                        body = "\n".join(body_lines).strip()
                        entries.append((subject, body))

                    if not entries:
                        return (branch, "")

                    chosen_subject, chosen_body = (
                        entries[0] if title_source == "first" else entries[-1]
                    )
                    return (chosen_subject, chosen_body)

                # custom: launch editor with possible commit template prefilled
                if title_source == "custom":
                    # Try commit template from git config
                    template_path = run_git(
                        ["config", "--get", "commit.template"], check=False
                    )
                    initial = ""
                    if template_path:
                        try:
                            with open(template_path, "r") as tf:
                                initial = tf.read()
                        except Exception:
                            initial = ""

                    editor = (
                        os.environ.get("GIT_EDITOR")
                        or os.environ.get("EDITOR")
                        or get_config("core.editor", "vim")
                    )
                    import subprocess
                    import tempfile

                    with tempfile.NamedTemporaryFile(
                        suffix=".prmsg", mode="w+", delete=False
                    ) as tf:
                        tf.write(initial)
                        temp_path = tf.name

                    try:
                        subprocess.check_call(f"{editor} {temp_path}", shell=True)
                        with open(temp_path, "r") as f:
                            content = f.read()
                    finally:
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass

                    # Parse content: first non-comment non-empty line is title
                    lines = [l.rstrip() for l in content.splitlines()]
                    filtered = [l for l in lines if l and not l.strip().startswith("#")]
                    if not filtered:
                        return (None, None)
                    title = filtered[0]
                    body = "\n".join(filtered[1:]) if len(filtered) > 1 else None
                    return (title, body)

                return (None, None)

            # If custom interactive mode is requested, run sequentially so editor isn't raced
            if title_source == "custom":
                for branch, parent_name in pr_tasks:
                    node = nodes.get(branch)
                    parent_node = nodes.get(parent_name)

                    real_is_stack_base = False
                    if parent_name == stack_base:
                        real_is_stack_base = True
                    elif parent_node and parent_node.parent is None:
                        real_is_stack_base = True

                    is_draft = not real_is_stack_base

                    title, body = derive_title_body(branch, parent_name)
                    try:
                        if platform:
                            kwargs = {"draft": is_draft}
                            if title is not None:
                                kwargs["title"] = title
                            if body is not None:
                                kwargs["body"] = body
                            platform.sync_mr(branch, parent_name, **kwargs)
                    except Exception as e:
                        print(f"PR sync failed for {branch}: {e}", file=sys.stderr)
            else:
                # Decide which PR tasks are updates (can be parallel) vs creations
                # (must be sequential to avoid race/order issues when opening MRs).
                update_tasks: List[Tuple[str, str]] = []
                create_tasks: List[Tuple[str, str]] = []

                for branch, parent_name in pr_tasks:
                    try:
                        open_mr = (
                            platform.get_mr(branch, state="open") if platform else None
                        )
                    except Exception:
                        open_mr = None

                    if open_mr:
                        update_tasks.append((branch, parent_name))
                        continue

                    try:
                        any_mr = (
                            platform.get_mr(branch, state="all") if platform else None
                        )
                    except Exception:
                        any_mr = None

                    if any_mr:
                        # Existing (merged/closed) PR found; platform.sync_mr will
                        # decide to skip or update base. Treat as update (no create).
                        update_tasks.append((branch, parent_name))
                    else:
                        create_tasks.append((branch, parent_name))

                # Parallelize update tasks
                if update_tasks:
                    print(f"Syncing {len(update_tasks)} PRs in parallel...")

                    def do_pr_sync(payload):
                        branch, parent_name = payload
                        node = nodes.get(branch)
                        parent_node = nodes.get(parent_name)

                        real_is_stack_base = False
                        if parent_name == stack_base:
                            real_is_stack_base = True
                        elif parent_node and parent_node.parent is None:
                            real_is_stack_base = True

                        is_draft = not real_is_stack_base

                        title, body = derive_title_body(branch, parent_name)

                        try:
                            if platform:
                                kwargs = {"draft": is_draft}
                                if title is not None:
                                    kwargs["title"] = title
                                if body is not None:
                                    kwargs["body"] = body
                                platform.sync_mr(branch, parent_name, **kwargs)
                        except Exception as e:
                            print(f"PR sync failed for {branch}: {e}", file=sys.stderr)

                    with ThreadPoolExecutor(max_workers=5) as executor:
                        futures = {
                            executor.submit(do_pr_sync, task): task
                            for task in update_tasks
                        }
                        for future in as_completed(futures):
                            pass

                # Perform create tasks sequentially in stack order
                if create_tasks:
                    print(f"Creating {len(create_tasks)} PRs sequentially...")
                    for branch, parent_name in create_tasks:
                        node = nodes.get(branch)
                        parent_node = nodes.get(parent_name)

                        real_is_stack_base = False
                        if parent_name == stack_base:
                            real_is_stack_base = True
                        elif parent_node and parent_node.parent is None:
                            real_is_stack_base = True

                        is_draft = not real_is_stack_base

                        title, body = derive_title_body(branch, parent_name)
                        try:
                            if platform:
                                kwargs = {"draft": is_draft}
                                if title is not None:
                                    kwargs["title"] = title
                                if body is not None:
                                    kwargs["body"] = body
                                platform.sync_mr(branch, parent_name, **kwargs)
                        except Exception as e:
                            print(f"PR sync failed for {branch}: {e}", file=sys.stderr)


def push_stack(dry_run: bool = False) -> None:
    """Deprecated: Use sync_stack(push=True, pr=False)."""
    sync_stack(push=True, pr=False, dry_run=dry_run)


def create_stack_prs(dry_run: bool = False) -> None:
    """Deprecated: Use sync_stack(push=False, pr=True)."""
    sync_stack(push=False, pr=True, dry_run=dry_run)
