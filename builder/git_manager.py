"""Git operations supporting branch switching and updates."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .command_runner import CommandRunner, CommandResult, CommandError


@dataclass(slots=True)
class GitWorkState:
    branch: str
    stash_applied: bool


class GitManager:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def prepare_checkout(
        self,
        *,
        repo_path: Path,
        target_branch: str,
        auto_stash: bool,
        no_switch_branch: bool,
    ) -> GitWorkState:
        repo_path = repo_path.resolve()
        if no_switch_branch:
            return GitWorkState(branch=self._current_branch(repo_path), stash_applied=False)

        self._ensure_repository(repo_path)
        current_branch = self._current_branch(repo_path)
        stash_applied = False
        dirty = self._is_dirty(repo_path)
        if dirty:
            if auto_stash:
                self._runner.run(["git", "stash", "push", "-m", "builder auto-stash"], cwd=repo_path)
                stash_applied = True
            else:
                raise RuntimeError("Working tree has uncommitted changes and auto_stash is disabled")

        if current_branch != target_branch:
            self._runner.run(["git", "fetch", "--all"], cwd=repo_path)
            result = self._runner.run(["git", "switch", target_branch], cwd=repo_path, check=False)
            if result.returncode != 0:
                self._runner.run(["git", "switch", "-c", target_branch, f"origin/{target_branch}"], cwd=repo_path)
        return GitWorkState(branch=current_branch, stash_applied=stash_applied)

    def restore_checkout(self, repo_path: Path, state: GitWorkState) -> None:
        repo_path = repo_path.resolve()
        current_branch = self._current_branch(repo_path)
        if current_branch != state.branch:
            self._runner.run(["git", "checkout", state.branch], cwd=repo_path)
        if state.stash_applied:
            self._runner.run(["git", "stash", "pop"], cwd=repo_path, check=False)

    def update_repository(
        self,
        *,
        repo_path: Path,
        url: str,
        main_branch: str,
        component_branch: Optional[str] = None,
        clone_script: Optional[str] = None,
        update_script: Optional[str] = None,
        auto_stash: bool = False,
        dry_run: bool = False,
    ) -> None:
        repo_path = repo_path.resolve()
        if not repo_path.exists():
            if clone_script:
                self._run_script(clone_script, repo_path, dry_run=dry_run)
            else:
                parent = repo_path.parent
                if not dry_run:
                    parent.mkdir(parents=True, exist_ok=True)
                self._run_command(["git", "clone", url, str(repo_path), "--recursive"], cwd=parent, dry_run=dry_run)
            return

        if update_script:
            self._run_script(update_script, repo_path, dry_run=dry_run)
            return

        self._ensure_repository(repo_path)
        dirty = self._is_dirty(repo_path)
        stash_applied = False
        if dirty and auto_stash:
            self._run_command(["git", "stash", "push", "-m", "builder auto-stash"], cwd=repo_path, dry_run=dry_run)
            stash_applied = True
        elif dirty:
            raise RuntimeError("Working tree has uncommitted changes; enable auto_stash to proceed")

        self._run_command(["git", "fetch", "--all"], cwd=repo_path, dry_run=dry_run)
        result = self._run_command(["git", "switch", main_branch], cwd=repo_path, check=False, dry_run=dry_run)
        if result.returncode != 0:
            self._run_command(["git", "switch", "-c", main_branch, f"origin/{main_branch}"], cwd=repo_path, dry_run=dry_run)
        self._run_command(["git", "pull", "--ff-only", "origin", main_branch], cwd=repo_path, dry_run=dry_run)
        self._run_command(["git", "submodule", "update", "--recursive"], cwd=repo_path, dry_run=dry_run)

        if component_branch:
            component_path = repo_path
            self._run_command(["git", "fetch", "--all"], cwd=component_path, dry_run=dry_run)
            result = self._run_command(["git", "switch", component_branch], cwd=component_path, check=False, dry_run=dry_run)
            if result.returncode != 0:
                raise RuntimeError(f"Component branch '{component_branch}' does not exist")
            self._run_command(["git", "pull", "--ff-only", "origin", component_branch], cwd=component_path, dry_run=dry_run)
            self._run_command(["git", "submodule", "update", "--recursive"], cwd=component_path, dry_run=dry_run)

        if stash_applied:
            self._run_command(["git", "switch", main_branch], cwd=repo_path, dry_run=dry_run)
            self._run_command(["git", "stash", "pop"], cwd=repo_path, check=False, dry_run=dry_run)

    def _run_command(
        self,
        command: list[str],
        *,
        cwd: Path,
        dry_run: bool,
        check: bool = True,
    ) -> CommandResult:
        return self._runner.run(command, cwd=cwd, check=check)

    def _current_branch(self, repo_path: Path) -> str:
        result = self._runner.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
        return result.stdout.strip() or "HEAD"

    def _is_dirty(self, repo_path: Path) -> bool:
        result = self._runner.run(["git", "status", "--porcelain"], cwd=repo_path)
        return bool(result.stdout.strip())

    def _ensure_repository(self, repo_path: Path) -> None:
        if not (repo_path / ".git").exists():
            raise RuntimeError(f"Directory '{repo_path}' is not a git repository")

    def _run_script(self, script: str, cwd: Path, *, dry_run: bool) -> None:
        if script.startswith(".") or script.startswith("/"):
            command = ["sh", "-c", script]
        else:
            command = ["sh", "-c", script]
        if dry_run:
            self._runner.run(command, cwd=cwd)
            return
        self._runner.run(command, cwd=cwd)
