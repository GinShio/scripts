"""Git operations supporting branch switching and updates."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional

from .command_runner import CommandRunner, CommandResult, CommandError


@dataclass(slots=True)
class GitWorkState:
    branch: str
    stash_applied: bool
    component_branch: str | None = None
    component_path: Path | None = None


class GitManager:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def get_repository_state(
        self,
        repo_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> tuple[str | None, str | None]:
        repo_path = repo_path.resolve()
        if not repo_path.exists() or not (repo_path / ".git").exists():
            return (None, None)

        branch: str | None = None
        commit: str | None = None

        try:
            branch = self._current_branch(repo_path, environment=environment)
        except CommandError:
            branch = None

        try:
            commit = self._current_commit(repo_path, environment=environment)
        except CommandError:
            commit = None

        return (branch, commit)

    def prepare_checkout(
        self,
        *,
        repo_path: Path,
        target_branch: str,
        auto_stash: bool,
        no_switch_branch: bool,
        environment: Mapping[str, str] | None = None,
        component_dir: Path | str | None = None,
        component_branch: str | None = None,
    ) -> GitWorkState:
        repo_path = repo_path.resolve()
        if no_switch_branch:
            branch = self._current_branch(repo_path, environment=environment)
            return GitWorkState(branch=branch, stash_applied=False)

        self._ensure_repository(repo_path)
        current_branch = self._current_branch(repo_path, environment=environment)
        stash_applied = False
        current_commit = self._current_commit(repo_path, environment=environment)
        target_commit = self._commit_for_branch(repo_path, target_branch, environment=environment)
        branch_switch_needed = current_branch != target_branch and current_commit != target_commit

        component_state_branch: str | None = None
        component_path: Path | None = None
        component_target_branch = component_branch or target_branch
        component_rel_path: Path | None = None
        component_is_submodule = False

        if component_dir:
            component_rel_path = Path(component_dir)
            component_path = component_rel_path if component_rel_path.is_absolute() else (repo_path / component_rel_path).resolve()
            component_is_submodule = self._is_component_submodule(
                repo_path,
                component_rel_path,
                environment=environment,
            )
            if component_is_submodule and component_target_branch:
                if self._is_git_directory(component_path):
                    try:
                        component_state_branch = self._current_branch(component_path, environment=environment)
                    except CommandError:
                        component_state_branch = None
                else:
                    component_path = None
                    component_state_branch = None
            else:
                component_path = None

        dirty = self._is_dirty(repo_path, environment=environment)
        if dirty and branch_switch_needed:
            if auto_stash:
                self._runner.run(
                    ["git", "stash", "push", "-m", "builder auto-stash"],
                    cwd=repo_path,
                    env=environment,
                )
                stash_applied = True
            else:
                raise RuntimeError("Working tree has uncommitted changes and auto_stash is disabled")

        if branch_switch_needed:
            result = self._runner.run(
                ["git", "switch", target_branch],
                cwd=repo_path,
                env=environment,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Unable to switch repository at '{repo_path}' to branch '{target_branch}'. Run 'builder update' first."
                )
            if component_is_submodule and component_path is not None and component_target_branch:
                restored_branch = self._switch_component_submodule(
                    component_path=component_path,
                    original_branch=component_state_branch,
                    target_branch=component_target_branch,
                    environment=environment,
                )
                if restored_branch is None:
                    component_path = None
                component_state_branch = restored_branch
            else:
                self._runner.run(
                    ["git", "submodule", "update", "--recursive"],
                    cwd=repo_path,
                    env=environment,
                )
        else:
            component_path = None
            component_state_branch = None
        return GitWorkState(
            branch=current_branch,
            stash_applied=stash_applied,
            component_branch=component_state_branch,
            component_path=component_path,
        )

    def restore_checkout(
        self,
        repo_path: Path,
        state: GitWorkState,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        repo_path = repo_path.resolve()
        current_branch = self._current_branch(repo_path, environment=environment)
        branch_restored = current_branch == state.branch
        if not branch_restored:
            self._runner.run(["git", "checkout", state.branch], cwd=repo_path, env=environment)
            branch_restored = True
        if branch_restored:
            self._runner.run(["git", "submodule", "update", "--recursive"], cwd=repo_path, env=environment)
        if state.stash_applied:
            self._runner.run(["git", "stash", "pop"], cwd=repo_path, env=environment, check=False)
        if state.component_path and state.component_branch:
            try:
                component_current = self._current_branch(state.component_path, environment=environment)
            except CommandError:
                return
            component_restored = component_current == state.component_branch
            if not component_restored:
                result = self._runner.run(
                    ["git", "switch", state.component_branch],
                    cwd=state.component_path,
                    env=environment,
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"Unable to restore component repository at '{state.component_path}' to branch '{state.component_branch}'."
                    )
                component_restored = True
            if component_restored:
                self._runner.run(
                    ["git", "submodule", "update", "--recursive"],
                    cwd=state.component_path,
                    env=environment,
                )

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
        environment: Mapping[str, str] | None = None,
        dry_run: bool = False,
    ) -> None:
        repo_path = repo_path.resolve()
        if not repo_path.exists():
            if clone_script:
                self._run_script(clone_script, None, environment=environment, dry_run=dry_run)
            else:
                self._run_command(
                    ["git", "clone", url, str(repo_path), "--recursive"],
                    cwd=None,
                    environment=environment,
                    dry_run=dry_run,
                )
            return

        if update_script:
            self._run_script(update_script, repo_path, environment=environment, dry_run=dry_run)
            return

        self._ensure_repository(repo_path)
        restore_branch: Optional[str] = None
        if not dry_run:
            current_branch = self._current_branch(repo_path, environment=environment)
            if current_branch and current_branch not in {"", "HEAD", main_branch}:
                restore_branch = current_branch

        dirty = self._is_dirty(repo_path, environment=environment)
        stash_applied = False
        if dirty and auto_stash:
            self._run_command(
                ["git", "stash", "push", "-m", "builder auto-stash"],
                cwd=repo_path,
                environment=environment,
                dry_run=dry_run,
            )
            stash_applied = True
        elif dirty:
            raise RuntimeError("Working tree has uncommitted changes; enable auto_stash to proceed")

        self._run_command(["git", "fetch", "--all"], cwd=repo_path, environment=environment, dry_run=dry_run)
        result = self._run_command(
            ["git", "switch", main_branch],
            cwd=repo_path,
            environment=environment,
            check=False,
            dry_run=dry_run,
        )
        if result.returncode != 0:
            self._run_command(
                ["git", "switch", "-c", main_branch, f"origin/{main_branch}"],
                cwd=repo_path,
                environment=environment,
                dry_run=dry_run,
            )
        self._run_command(
            ["git", "pull", "--ff-only", "origin", main_branch],
            cwd=repo_path,
            environment=environment,
            dry_run=dry_run,
        )
        self._run_command(
            ["git", "submodule", "update", "--recursive"],
            cwd=repo_path,
            environment=environment,
            dry_run=dry_run,
        )

        if component_branch:
            component_path = repo_path
            self._run_command(
                ["git", "fetch", "--all"],
                cwd=component_path,
                environment=environment,
                dry_run=dry_run,
            )
            result = self._run_command(
                ["git", "switch", component_branch],
                cwd=component_path,
                environment=environment,
                check=False,
                dry_run=dry_run,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Component branch '{component_branch}' does not exist")
            self._run_command(
                ["git", "pull", "--ff-only", "origin", component_branch],
                cwd=component_path,
                environment=environment,
                dry_run=dry_run,
            )
            self._run_command(
                ["git", "submodule", "update", "--recursive"],
                cwd=component_path,
                environment=environment,
                dry_run=dry_run,
            )

        if stash_applied:
            self._run_command(
                ["git", "switch", main_branch],
                cwd=repo_path,
                environment=environment,
                dry_run=dry_run,
            )
            self._run_command(
                ["git", "stash", "pop"],
                cwd=repo_path,
                environment=environment,
                check=False,
                dry_run=dry_run,
            )

        if restore_branch:
            self._run_command(
                ["git", "switch", restore_branch],
                cwd=repo_path,
                environment=environment,
                dry_run=dry_run,
            )

    def _run_command(
        self,
        command: list[str],
        *,
        cwd: Path | None,
        dry_run: bool,
        check: bool = True,
        environment: Mapping[str, str] | None = None,
        stream: bool | None = None,
    ) -> CommandResult:
        run_stream = stream if stream is not None else not dry_run
        return self._runner.run(command, cwd=cwd, env=environment, check=check, stream=run_stream)

    def _is_git_directory(self, path: Path) -> bool:
        git_dir = path / ".git"
        return git_dir.exists()

    def _is_component_submodule(
        self,
        repo_path: Path,
        component_dir: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        # Normalize path to the git-config representation
        relative_str = component_dir.as_posix()
        escaped = relative_str.replace("\"", r"\\\"")
        candidate_keys = [
            f'submodule."{escaped}".path',
            f"submodule.{relative_str}.path",
        ]
        for key in candidate_keys:
            result = self._runner.run(
                ["git", "config", "--file", ".gitmodules", key],
                cwd=repo_path,
                env=environment,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
        return False

    def _switch_component_submodule(
        self,
        *,
        component_path: Path,
        original_branch: str | None,
        target_branch: str,
        environment: Mapping[str, str] | None = None,
    ) -> str | None:
        if not self._is_git_directory(component_path):
            return None

        try:
            current_branch = self._current_branch(component_path, environment=environment)
        except CommandError:
            current_branch = None

        try:
            current_commit = self._current_commit(component_path, environment=environment)
        except CommandError:
            current_commit = None

        target_commit = self._commit_for_branch(component_path, target_branch, environment=environment)
        branch_switch_needed = current_branch != target_branch and (current_commit != target_commit)

        if branch_switch_needed:
            result = self._runner.run(
                ["git", "switch", target_branch],
                cwd=component_path,
                env=environment,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Unable to switch component repository at '{component_path}' to branch '{target_branch}'. Run 'builder update' first."
                )
            self._runner.run(
                ["git", "submodule", "update", "--recursive"],
                cwd=component_path,
                env=environment,
            )
            if original_branch and original_branch != target_branch:
                return original_branch
        return None

    def _current_branch(self, repo_path: Path, *, environment: Mapping[str, str] | None = None) -> str:
        result = self._runner.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            env=environment,
        )
        return result.stdout.strip() or "HEAD"

    def _current_commit(self, repo_path: Path, *, environment: Mapping[str, str] | None = None) -> str:
        result = self._runner.run(["git", "rev-parse", "HEAD"], cwd=repo_path, env=environment)
        return result.stdout.strip()

    def list_submodules(
        self,
        repo_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> list[dict[str, str]]:
        """List all submodules with their paths, URLs, and commit hashes."""
        repo_path = repo_path.resolve()
        if not repo_path.exists() or not (repo_path / ".git").exists():
            return []

        submodules: list[dict[str, str]] = []

        try:
            # Get submodule status which includes commit hash and path
            status_result = self._runner.run(
                ["git", "submodule", "status", "--recursive"],
                cwd=repo_path,
                env=environment,
                check=False,
            )

            if status_result.returncode != 0:
                return []

            # Parse submodule status output
            # Format: [+|-| ]<hash> <path> [(<branch>)]
            for line in status_result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue

                # Remove status prefix (+ for ahead, - for behind, space for clean)
                if line.startswith(('+', '-', ' ')):
                    line = line[1:]

                parts = line.split(None, 2)
                if len(parts) < 2:
                    continue

                commit_hash = parts[0]
                submodule_path = parts[1]

                # Get submodule URL from configuration
                url = ""
                try:
                    escaped_path = submodule_path.replace("\"", r"\\\"")
                    candidate_keys = [
                        f'submodule."{escaped_path}".url',
                        f"submodule.{submodule_path}.url",
                    ]

                    for key in candidate_keys:
                        url_result = self._runner.run(
                            ["git", "config", "--file", ".gitmodules", key],
                            cwd=repo_path,
                            env=environment,
                            check=False,
                        )
                        if url_result.returncode == 0:
                            url = url_result.stdout.strip()
                            if url:
                                break

                    if not url:
                        for key in candidate_keys:
                            url_result = self._runner.run(
                                ["git", "config", "--get", key],
                                cwd=repo_path,
                                env=environment,
                                check=False,
                            )
                            if url_result.returncode == 0:
                                url = url_result.stdout.strip()
                                if url:
                                    break
                except CommandError:
                    pass

                submodules.append({
                    "path": submodule_path,
                    "hash": commit_hash,
                    "url": url,
                })

        except CommandError:
            pass

        return submodules

    def _commit_for_branch(
        self,
        repo_path: Path,
        branch: str,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> str | None:
        result = self._runner.run(["git", "rev-parse", branch], cwd=repo_path, check=False, env=environment)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _is_dirty(self, repo_path: Path, *, environment: Mapping[str, str] | None = None) -> bool:
        result = self._runner.run(["git", "status", "--porcelain"], cwd=repo_path, env=environment)
        return bool(result.stdout.strip())

    def _ensure_repository(self, repo_path: Path) -> None:
        if not (repo_path / ".git").exists():
            raise RuntimeError(f"Directory '{repo_path}' is not a git repository")

    def _run_script(
        self,
        script: str,
        cwd: Path | None,
        *,
        environment: Mapping[str, str] | None,
        dry_run: bool,
    ) -> None:
        if script.startswith(".") or script.startswith("/"):
            command = ["sh", "-c", script]
        else:
            command = ["sh", "-c", script]
        run_stream = not dry_run
        if dry_run:
            self._runner.run(command, cwd=cwd, env=environment, stream=False)
            return
        self._runner.run(command, cwd=cwd, env=environment, stream=run_stream)
