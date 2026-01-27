"""Sync module for pushing branches and managing PRs."""

from __future__ import annotations

import os
import sys
import uuid
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


class StackSyncer:
    def __init__(
        self,
        push: bool = True,
        pr: bool = False,
        dry_run: bool = False,
        limit_to_branch: Optional[str] = None,
        title_source: str = "last",
    ):
        self.push = push
        self.pr = pr
        self.dry_run = dry_run
        self.limit_to_branch = limit_to_branch
        self.title_source = title_source

        self.nodes = {}
        self.refs = {}
        self.platform: Optional[PlatformInterface] = None
        self.stack_base = ""
        self.failed_pushes: Set[str] = set()
        self.push_tasks: List[str] = []
        self.pr_tasks: List[Tuple[str, str]] = []

    def sync(self) -> None:
        self._init_state()
        self._collect_tasks()
        self._execute_push_tasks()
        self._execute_pr_tasks()

    def _init_state(self) -> None:
        self.nodes = parse_machete()
        if not self.nodes:
            print(
                "Error: No .git/machete definition found. Ensure your stack is defined."
            )
            sys.exit(1)

        self.refs = get_refs_map()
        self.stack_base = resolve_base_branch()

        if self.pr:
            self.platform = get_platform()
            if not self.platform:
                print("Warning: Could not detect git platform. PR creation skipped.")
                self.pr = False

    def _collect_tasks(self) -> None:
        roots = get_roots(self.nodes)
        targets = list(self.nodes.values())

        if self.limit_to_branch:
            if self.limit_to_branch not in self.nodes:
                print(f"Branch '{self.limit_to_branch}' not found in stack definition.")
                sys.exit(1)

            from .machete import get_linear_stack

            linear_stack = get_linear_stack(self.limit_to_branch, self.nodes)
            if not linear_stack:
                print("Empty stack found.")
                return

            targets = linear_stack
            roots = [linear_stack[0]]
            print(f"Limiting sync to linear stack: {[n.name for n in targets]}")

        target_names = {t.name for t in targets}

        def _visit(node: MacheteNode, parent: Optional[MacheteNode]):
            branch = node.name
            if self.limit_to_branch and branch not in target_names:
                return

            is_root_special = branch == self.stack_base

            if branch in self.refs:
                if self.push and not is_root_special:
                    self.push_tasks.append(branch)
                if self.pr and not is_root_special:
                    if parent and self.platform:
                        self.pr_tasks.append((branch, parent.name))

            for child in node.children:
                _visit(child, node)

        for root in roots:
            _visit(root, None)

    def _execute_push_tasks(self) -> None:
        if not self.push_tasks:
            return

        if self.dry_run:
            print(f"Would push branches: {', '.join(self.push_tasks)}")
            return

        print(f"Pushing {len(self.push_tasks)} branches in parallel...")
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(push_branch, branch): branch
                for branch in self.push_tasks
            }
            for future in as_completed(futures):
                branch = futures[future]
                try:
                    success = future.result()
                    if not success:
                        self.failed_pushes.add(branch)
                except Exception as exc:
                    print(f"Push task for {branch} generated an exception: {exc}")
                    self.failed_pushes.add(branch)

    def _execute_pr_tasks(self) -> None:
        if not self.pr_tasks:
            return

        if self.dry_run:
            print(f"Would sync PRs for: {', '.join([b for b, p in self.pr_tasks])}")
            return

        if self.title_source == "custom":
            self._execute_pr_tasks_custom()
        else:
            self._execute_pr_tasks_auto()

    def _execute_pr_tasks_custom(self) -> None:
        # Sequential execution for interactive mode
        for branch, parent_name in self.pr_tasks:
            if branch in self.failed_pushes:
                print(f"Skipping PR sync for {branch} due to failed push.")
                continue
            self._sync_single_pr(branch, parent_name)

    def _execute_pr_tasks_auto(self) -> None:
        update_tasks = []
        create_tasks = []

        # Classify tasks
        for branch, parent_name in self.pr_tasks:
            if branch in self.failed_pushes:
                print(f"Skipping PR sync for {branch} due to failed push.")
                continue

            # Check existing PR state to decide update vs create
            # This check can be slow, maybe parallelize?
            # For now, let's keep it simple or parallelize the check if needed.
            # To be safe and fast, let's just assume we can check in the worker
            # BUT creation must be sequential to avoid race conditions on some platforms?
            # Actually, checking first is better.

            # Optimization: We can just try to get open PR
            try:
                open_mr = (
                    self.platform.get_mr(branch, state="open", base=parent_name)
                    if self.platform
                    else None
                )
            except Exception:
                open_mr = None

            if open_mr:
                update_tasks.append((branch, parent_name))
            else:
                # Check closed/merged to avoid dups
                try:
                    any_mr = (
                        self.platform.get_mr(branch, state="all", base=parent_name)
                        if self.platform
                        else None
                    )
                except Exception:
                    any_mr = None

                if any_mr:
                    update_tasks.append((branch, parent_name))
                else:
                    create_tasks.append((branch, parent_name))

        # Parallel Updates
        if update_tasks:
            print(f"Syncing {len(update_tasks)} PRs in parallel...")
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(self._sync_single_pr, b, p): (b, p)
                    for b, p in update_tasks
                }
                for future in as_completed(futures):
                    pass

        # Sequential Creates
        if create_tasks:
            print(f"Creating {len(create_tasks)} PRs sequentially...")
            for branch, parent_name in create_tasks:
                self._sync_single_pr(branch, parent_name)

    def _sync_single_pr(self, branch: str, parent_name: str) -> None:
        node = self.nodes.get(branch)
        parent_node = self.nodes.get(parent_name)

        real_is_stack_base = False
        if parent_name == self.stack_base:
            real_is_stack_base = True
        elif parent_node and parent_node.parent is None:
            real_is_stack_base = True

        is_draft = not real_is_stack_base

        title, body = self._derive_title_body(branch, parent_name)

        try:
            if self.platform:
                kwargs = {"draft": is_draft}
                if title is not None:
                    kwargs["title"] = title
                if body is not None:
                    kwargs["body"] = body

                local_sha = self.refs.get(branch)
                self.platform.sync_mr(
                    branch, parent_name, local_sha=local_sha, **kwargs
                )
        except Exception as e:
            print(f"PR sync failed for {branch}: {e}", file=sys.stderr)

    def _derive_title_body(
        self, branch: str, parent: str
    ) -> Tuple[Optional[str], Optional[str]]:
        if self.title_source == "custom":
            return self._derive_custom(branch, parent)
        return self._derive_from_git(branch, parent)

    def _derive_from_git(
        self, branch: str, parent: str
    ) -> Tuple[Optional[str], Optional[str]]:
        order = "--reverse" if self.title_source == "first" else ""

        marker_commit = f"GITSTACK_COMMIT_{uuid.uuid4().hex}"
        marker_body = f"GITSTACK_BODY_{uuid.uuid4().hex}"

        fmt = f"{marker_commit}%n%s%n{marker_body}%n%b"
        args = ["log", order, f"--pretty=format:{fmt}", f"{parent}..{branch}"]
        out = run_git(args, check=False)

        if not out:
            # Fallback to HEAD
            out = run_git(["show", "-s", f"--pretty=format:{fmt}", branch], check=False)
            if not out:
                return (branch, "")

        raw_chunks = out.split(f"{marker_commit}\n")
        chunks = [c for c in raw_chunks if c.strip()]
        entries = []
        for chunk in chunks:
            lines = chunk.splitlines()
            if not lines:
                continue

            if marker_body in lines:
                marker_idx = lines.index(marker_body)
                subject_candidates = [l for l in lines[:marker_idx] if l.strip()]
                subject = subject_candidates[0] if subject_candidates else ""
                body_lines = lines[marker_idx + 1 :]
            else:
                subject = lines[0]
                body_lines = lines[1:]

            while body_lines and not body_lines[0].strip():
                body_lines.pop(0)
            while body_lines and not body_lines[-1].strip():
                body_lines.pop()

            body = "\n".join(body_lines).strip()
            entries.append((subject, body))

        if not entries:
            return (branch, "")

        return entries[0] if self.title_source == "first" else entries[-1]

    def _derive_custom(
        self, branch: str, parent: str
    ) -> Tuple[Optional[str], Optional[str]]:
        # Try commit template
        template_path = run_git(["config", "--get", "commit.template"], check=False)
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

        lines = [l.rstrip() for l in content.splitlines()]
        filtered = [l for l in lines if l and not l.strip().startswith("#")]
        if not filtered:
            return (None, None)
        title = filtered[0]
        body = "\n".join(filtered[1:]) if len(filtered) > 1 else None
        return (title, body)


def push_branch(branch: str, check: bool = False) -> bool:
    """
    Force push branch to origin.
    Returns True if pushed, False if already up to date (or failed if check=False).
    """
    print(f"Pushing {branch}...")
    try:
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
    """
    syncer = StackSyncer(push, pr, dry_run, limit_to_branch, title_source)
    syncer.sync()


def push_stack(dry_run: bool = False) -> None:
    """Deprecated: Use sync_stack(push=True, pr=False)."""
    sync_stack(push=True, pr=False, dry_run=dry_run)


def create_stack_prs(dry_run: bool = False) -> None:
    """Deprecated: Use sync_stack(push=False, pr=True)."""
    sync_stack(push=False, pr=True, dry_run=dry_run)
