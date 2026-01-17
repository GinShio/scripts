"""Git operations supporting branch switching and updates."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from core.command_runner import CommandError, CommandResult, CommandRunner


@dataclass(slots=True)
class GitWorkState:
    branch: str
    stash_applied: bool
    component_branch: str | None = None
    component_path: Path | None = None
    component_stash_applied: bool = False
    root_switched: bool = False


class GitManager:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def is_repository(
        self,
        repo_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        repo_path = repo_path.resolve()
        if not repo_path.exists():
            return False

        result = self._runner.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_path,
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            return False
        text = result.stdout.strip().lower()
        if not text:
            return True
        return text in {"true", "1", "yes"}

    def is_sparse_checkout(
        self,
        repo_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        repo_path = repo_path.resolve()
        if not repo_path.exists():
            return False

        result = self._runner.run(
            ["git", "config", "--bool", "core.sparseCheckout"],
            cwd=repo_path,
            env=environment,
            check=False,
        )
        if result.returncode != 0:
            return False
        text = result.stdout.strip().lower()
        if not text:
            return False
        return text in {"true", "1", "yes"}

    def get_repository_state(
        self,
        repo_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> tuple[str | None, str | None]:
        repo_path = repo_path.resolve()
        if not self.is_repository(repo_path, environment=environment):
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
        dry_run: bool = False,
    ) -> GitWorkState:
        repo_path = repo_path.resolve()
        if no_switch_branch:
            branch = self._current_branch(repo_path, environment=environment)
            return GitWorkState(branch=branch, stash_applied=False)

        if not dry_run:
            self._ensure_repository(repo_path, environment=environment)
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
        component_repo_detected = False

        if component_dir:
            component_candidate = Path(component_dir)
            if component_candidate.is_absolute():
                component_path = component_candidate.resolve(strict=False)
                try:
                    component_rel_path = component_path.relative_to(repo_path)
                except ValueError:
                    component_rel_path = None
            else:
                component_rel_path = component_candidate
                component_path = (repo_path / component_candidate).resolve(strict=False)

            if component_path and self._is_git_directory(component_path):
                component_repo_detected = True
                if component_rel_path is not None:
                    component_is_submodule = self._is_component_submodule(
                        repo_path,
                        component_rel_path,
                        environment=environment,
                    )
                if component_target_branch:
                    try:
                        component_state_branch = self._current_branch(component_path, environment=environment)
                    except CommandError:
                        component_state_branch = None
            else:
                component_path = None

        should_switch_component = component_repo_detected and bool(component_target_branch)
        should_switch_root = branch_switch_needed and not should_switch_component
        component_stash_applied = False

        component_dirty = False
        if component_path is not None and should_switch_component:
            try:
                component_current_branch = self._current_branch(component_path, environment=environment)
            except CommandError:
                component_current_branch = None
            try:
                component_current_commit = self._current_commit(component_path, environment=environment)
            except CommandError:
                component_current_commit = None
            component_target_commit = None
            if component_target_branch:
                component_target_commit = self._commit_for_branch(component_path, component_target_branch, environment=environment)

            component_switch_needed = True
            if component_current_branch == component_target_branch:
                if component_target_commit is None or component_current_commit == component_target_commit:
                    component_switch_needed = False

            if not component_switch_needed:
                should_switch_component = False
            else:
                try:
                    component_dirty = self._is_dirty(component_path, environment=environment)
                except CommandError:
                    component_dirty = False

        dirty = self._is_dirty(repo_path, environment=environment)
        root_switched = False
        if should_switch_root and dirty:
            if auto_stash:
                self._run_command(
                    ["git", "stash", "push", "-m", "builder auto-stash"],
                    cwd=repo_path,
                    dry_run=dry_run,
                    environment=environment,
                    stream=False,
                )
                stash_applied = True
            else:
                raise RuntimeError("Working tree has uncommitted changes and auto_stash is disabled")

        if should_switch_component and component_dirty:
            if auto_stash:
                self._run_command(
                    ["git", "stash", "push", "-m", "builder auto-stash"],
                    cwd=component_path,
                    dry_run=dry_run,
                    environment=environment,
                    stream=False,
                )
                component_stash_applied = True
            else:
                raise RuntimeError(
                    f"Component working tree at '{component_path}' has uncommitted changes; enable auto_stash to proceed"
                )

        if should_switch_root:
            result = self._run_command(
                ["git", "switch", target_branch],
                cwd=repo_path,
                dry_run=dry_run,
                environment=environment,
                check=False,
                stream=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Unable to switch repository at '{repo_path}' to branch '{target_branch}'. Run 'builder update' first."
                )
            root_switched = True
            if should_switch_component:
                restored_branch = self._switch_component_submodule(
                    component_path=component_path,
                    original_branch=component_state_branch,
                    target_branch=component_target_branch or target_branch,
                    environment=environment,
                    dry_run=dry_run,
                )
                if restored_branch is None:
                    component_path = None
                component_state_branch = restored_branch
            else:
                self._update_submodules(
                    repo_path,
                    dry_run=dry_run,
                    environment=environment,
                    stream=False,
                )
        elif should_switch_component:
            restored_branch = self._switch_component_submodule(
                component_path=component_path,
                original_branch=component_state_branch,
                target_branch=component_target_branch or target_branch,
                environment=environment,
                dry_run=dry_run,
            )
            if restored_branch is None:
                component_path = None
            component_state_branch = restored_branch
        else:
            component_path = None
            component_state_branch = None
        return GitWorkState(
            branch=current_branch,
            stash_applied=stash_applied,
            component_branch=component_state_branch,
            component_path=component_path,
            component_stash_applied=component_stash_applied,
            root_switched=root_switched,
        )

    def restore_checkout(
        self,
        repo_path: Path,
        state: GitWorkState,
        *,
        environment: Mapping[str, str] | None = None,
        dry_run: bool = False,
    ) -> None:
        repo_path = repo_path.resolve()
        current_branch = self._current_branch(repo_path, environment=environment)
        branch_restored = current_branch == state.branch
        restored_now = False
        if not branch_restored:
            self._run_command(
                ["git", "checkout", state.branch],
                cwd=repo_path,
                dry_run=dry_run,
                environment=environment,
                stream=False,
            )
            branch_restored = True
            restored_now = True
        if branch_restored and (state.root_switched or restored_now):
            self._update_submodules(
                repo_path,
                dry_run=dry_run,
                environment=environment,
                stream=False,
            )
        if state.component_path:
            if state.component_branch:
                try:
                    component_current = self._current_branch(state.component_path, environment=environment)
                except CommandError:
                    component_current = None
                if component_current is not None and component_current != state.component_branch:
                    result = self._run_command(
                        ["git", "switch", state.component_branch],
                        cwd=state.component_path,
                        dry_run=dry_run,
                        environment=environment,
                        check=False,
                        stream=False,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(
                            f"Unable to restore component repository at '{state.component_path}' to branch '{state.component_branch}'."
                        )
                self._update_submodules(
                    state.component_path,
                    dry_run=dry_run,
                    environment=environment,
                    stream=False,
                )
            if state.component_stash_applied:
                self._run_command(
                    ["git", "stash", "pop"],
                    cwd=state.component_path,
                    dry_run=dry_run,
                    environment=environment,
                    check=False,
                    stream=False,
                )
        if state.stash_applied:
            self._run_command(
                ["git", "stash", "pop"],
                cwd=repo_path,
                dry_run=dry_run,
                environment=environment,
                check=False,
                stream=False,
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
        component_dir: Path | str | None = None,
    ) -> None:
        repo_path = repo_path.resolve()
        if not repo_path.exists():
            if clone_script:
                self._run_script(clone_script, None, environment=environment, dry_run=dry_run)
            else:
                if not dry_run:
                    repo_path.mkdir(parents=True, exist_ok=True)

                self._run_command(
                    ["git", "init"],
                    cwd=repo_path,
                    environment=environment,
                    dry_run=dry_run,
                )
                self._run_command(
                    ["git", "remote", "add", "origin", url],
                    cwd=repo_path,
                    environment=environment,
                    dry_run=dry_run,
                )
                self._run_command(
                    ["git", "fetch", "origin"],
                    cwd=repo_path,
                    environment=environment,
                    dry_run=dry_run,
                )
                self._run_command(
                    ["git", "checkout", main_branch],
                    cwd=repo_path,
                    environment=environment,
                    dry_run=dry_run,
                )
                self._run_command(
                    ["git", "submodule", "update", "--init", "--recursive"],
                    cwd=repo_path,
                    environment=environment,
                    dry_run=dry_run,
                )
            return

        self._ensure_repository(repo_path, environment=environment)

        component_repo_path: Path | None = None
        component_original_branch: Optional[str] = None
        component_is_submodule = False
        component_stash_applied = False
        component_rel_path: str | None = None
        component_switch_required = False
        if component_dir is not None:
            component_candidate = Path(component_dir)
            candidate_path = (
                component_candidate
                if component_candidate.is_absolute()
                else (repo_path / component_candidate).resolve()
            )
            if self._is_git_directory(candidate_path):
                component_repo_path = candidate_path
                try:
                    component_original_branch = self._current_branch(candidate_path, environment=environment)
                except CommandError:
                    component_original_branch = None
                if not component_candidate.is_absolute():
                    component_rel_path = component_candidate.as_posix()
                    component_is_submodule = self._is_component_submodule(
                        repo_path,
                        component_candidate,
                        environment=environment,
                    )

        # Step 1: stash dirty worktrees as needed
        root_original_branch = self._current_branch(repo_path, environment=environment)
        root_switched = False
        root_stash_applied = self._stash_if_dirty(
            repo_path,
            auto_stash=auto_stash,
            dry_run=dry_run,
            environment=environment,
            error_message="Working tree has uncommitted changes; enable auto_stash to proceed",
        )

        component_target_branch = component_branch or main_branch

        component_current_branch: Optional[str] = None
        if component_repo_path is not None:
            try:
                component_current_branch = self._current_branch(component_repo_path, environment=environment)
            except CommandError:
                component_current_branch = None
            if component_target_branch:
                component_switch_required = component_current_branch != component_target_branch
            component_stash_applied = self._stash_if_dirty(
                component_repo_path,
                auto_stash=auto_stash,
                dry_run=dry_run,
                environment=environment,
                error_message=(
                    f"Component working tree at '{component_repo_path}' has uncommitted changes; enable auto_stash to proceed"
                ),
            )

        # Step 2: update root repository
        if root_original_branch != main_branch:
            self._run_command(
                ["git", "switch", main_branch],
                cwd=repo_path,
                dry_run=dry_run,
                environment=environment,
                check=False,
                stream=False,
            )
            root_switched = True

        if update_script:
            self._run_script(update_script, repo_path, environment=environment, dry_run=dry_run)
        else:
            self._run_command(
                ["git", "fetch", "--all"],
                cwd=repo_path,
                dry_run=dry_run,
                environment=environment,
            )
            merge_target = f"origin/{main_branch}"
            result = self._run_command(
                ["git", "merge", "--ff-only", merge_target],
                cwd=repo_path,
                dry_run=dry_run,
                environment=environment,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Unable to fast-forward main branch '{main_branch}' from {merge_target}")

        self._update_submodules(
            repo_path,
            dry_run=dry_run,
            environment=environment,
        )

        if component_repo_path is not None and component_target_branch:
            try:
                refreshed_branch = self._current_branch(component_repo_path, environment=environment)
            except CommandError:
                refreshed_branch = None
            else:
                component_current_branch = refreshed_branch

        # Step 3: update component repository when available
        if component_repo_path is not None and component_target_branch:
            if component_switch_required or component_current_branch != component_target_branch:
                result = self._run_command(
                    ["git", "switch", component_target_branch],
                    cwd=component_repo_path,
                    dry_run=dry_run,
                    environment=environment,
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"Unable to switch component repository at '{component_repo_path}' to branch '{component_target_branch}'."
                    )
                component_switch_required = True
                component_current_branch = component_target_branch

            self._run_command(
                ["git", "fetch", "--all"],
                cwd=component_repo_path,
                dry_run=dry_run,
                environment=environment,
            )
            merge_target = f"origin/{component_target_branch}"
            result = self._run_command(
                ["git", "merge", "--ff-only", merge_target],
                cwd=component_repo_path,
                dry_run=dry_run,
                environment=environment,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Unable to fast-forward component repository at '{component_repo_path}' using '{merge_target}'."
                )
            self._update_submodules(
                component_repo_path,
                dry_run=dry_run,
                environment=environment,
            )

        # Step 4: restore component branch and stash before restoring root
        if component_repo_path is not None:
            if component_switch_required and component_original_branch and component_original_branch != component_target_branch:
                result = self._run_command(
                    ["git", "switch", component_original_branch],
                    cwd=component_repo_path,
                    dry_run=dry_run,
                    environment=environment,
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"Unable to restore component repository at '{component_repo_path}' to branch '{component_original_branch}'."
                    )
                self._update_submodules(
                    component_repo_path,
                    dry_run=dry_run,
                    environment=environment,
                )

            self._restore_stash(
                component_repo_path,
                stash_applied=component_stash_applied,
                dry_run=dry_run,
                environment=environment,
            )

        # Step 5: restore root branch and stash
        skip_paths: set[str] = set()
        if component_is_submodule and component_rel_path:
            skip_paths.add(component_rel_path)

        can_restore_root = root_switched and bool(root_original_branch) and root_original_branch not in {"", "HEAD"}
        if can_restore_root:
            result = self._run_command(
                ["git", "switch", root_original_branch],
                cwd=repo_path,
                dry_run=dry_run,
                environment=environment,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Unable to restore repository at '{repo_path}' to branch '{root_original_branch}'."
                )
            self._update_submodules(
                repo_path,
                dry_run=dry_run,
                environment=environment,
                skip_paths=skip_paths,
            )
        elif skip_paths:
            # Ensure other submodules still refresh even when we stay on main
            self._update_submodules(
                repo_path,
                dry_run=dry_run,
                environment=environment,
                skip_paths=skip_paths,
            )

        self._restore_stash(
            repo_path,
            stash_applied=root_stash_applied,
            dry_run=dry_run,
            environment=environment,
        )

    def _run_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None,
        dry_run: bool,
        check: bool = True,
        environment: Mapping[str, str] | None = None,
        stream: bool | None = None,
    ) -> CommandResult:
        run_stream = stream if stream is not None else not dry_run
        return self._runner.run(list(command), cwd=cwd, env=environment, check=check, stream=run_stream)

    def _stash_if_dirty(
        self,
        repo_path: Path,
        *,
        auto_stash: bool,
        dry_run: bool,
        environment: Mapping[str, str] | None = None,
        error_message: str,
        stream: bool | None = None,
    ) -> bool:
        dirty = self._is_dirty(repo_path, environment=environment)
        if not dirty:
            return False
        if not auto_stash:
            raise RuntimeError(error_message)
        self._run_command(
            ["git", "stash", "push", "-m", "builder auto-stash"],
            cwd=repo_path,
            dry_run=dry_run,
            environment=environment,
            stream=stream,
        )
        return True

    def _restore_stash(
        self,
        repo_path: Path | None,
        *,
        stash_applied: bool,
        dry_run: bool,
        environment: Mapping[str, str] | None = None,
        stream: bool | None = None,
    ) -> None:
        if not stash_applied or repo_path is None:
            return
        self._run_command(
            ["git", "stash", "pop"],
            cwd=repo_path,
            dry_run=dry_run,
            environment=environment,
            check=False,
            stream=stream,
        )

    def _update_submodules(
        self,
        repo_path: Path,
        *,
        dry_run: bool,
        environment: Mapping[str, str] | None = None,
        stream: bool | None = None,
        skip_paths: set[str] | None = None,
    ) -> CommandResult:
        if not skip_paths:
            return self._run_command(
                ["git", "submodule", "update", "--recursive"],
                cwd=repo_path,
                dry_run=dry_run,
                environment=environment,
                stream=stream,
            )

        include_paths: list[str] = []
        try:
            submodules = self.list_submodules(repo_path, environment=environment)
        except CommandError:
            submodules = []

        for entry in submodules:
            path = entry.get("path")
            if path and path not in skip_paths:
                include_paths.append(path)

        if not include_paths:
            return CommandResult(command=["git", "submodule", "update", "--recursive"], returncode=0, stdout="", stderr="")

        args = ["git", "submodule", "update", "--recursive", "--", *sorted(include_paths)]
        return self._run_command(
            args,
            cwd=repo_path,
            dry_run=dry_run,
            environment=environment,
            stream=stream,
        )

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
        dry_run: bool = False,
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
        branch_switch_needed = True
        if current_branch == target_branch:
            if target_commit is None:
                branch_switch_needed = False
            elif current_commit == target_commit:
                branch_switch_needed = False
        elif current_commit is not None and target_commit is not None and current_commit == target_commit:
            # Branch names differ but commits match; still need to switch to move HEAD onto the target branch.
            branch_switch_needed = True

        if branch_switch_needed:
            result = self._run_command(
                ["git", "switch", target_branch],
                cwd=component_path,
                dry_run=dry_run,
                environment=environment,
                check=False,
                stream=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Unable to switch component repository at '{component_path}' to branch '{target_branch}'. Run 'builder update' first."
                )
            self._update_submodules(
                component_path,
                dry_run=dry_run,
                environment=environment,
                stream=False,
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
        if not self.is_repository(repo_path, environment=environment):
            return []

        submodules: list[dict[str, str]] = []
        sparse_checkout = self.is_sparse_checkout(repo_path, environment=environment)

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

                if sparse_checkout:
                    candidate_path = (repo_path / submodule_path).resolve(strict=False)
                    if not candidate_path.exists():
                        continue

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
        text = result.stdout.strip()
        return text or None

    def _is_dirty(self, repo_path: Path, *, environment: Mapping[str, str] | None = None) -> bool:
        result = self._runner.run(["git", "status", "--porcelain", "--ignore-submodules", "--untracked-files=no"], cwd=repo_path, env=environment)
        return bool(result.stdout.strip())

    def _ensure_repository(
        self,
        repo_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not self.is_repository(repo_path, environment=environment):
            raise RuntimeError(f"Directory '{repo_path}' is not a git repository")

    def _run_script(
        self,
        script: str,
        cwd: Path | None,
        *,
        environment: Mapping[str, str] | None,
        dry_run: bool,
    ) -> None:
        command = ["sh", "-c", script]
        run_stream = not dry_run
        if dry_run:
            self._runner.run(command, cwd=cwd, env=environment, stream=False)
            return
        self._runner.run(command, cwd=cwd, env=environment, stream=run_stream)
