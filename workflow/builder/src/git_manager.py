"""Git operations supporting branch switching and updates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from core.command_runner import CommandError, CommandResult, CommandRunner
from core.git_api import GitRepository


@dataclass(slots=True)
class GitWorkState:
    branch: str | None
    stash_applied: bool
    component_branch: str | None = None
    component_path: Path | None = None
    component_stash_applied: bool = False
    root_switched: bool = False


class GitManager:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def _get_repo(self, path: Path) -> GitRepository:
        return GitRepository(path, runner=self._runner)

    def is_repository(
        self,
        repo_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        return self._get_repo(repo_path).is_valid

    def is_sparse_checkout(
        self,
        repo_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        return self._get_repo(repo_path).is_sparse_checkout()

    def get_repository_state(
        self,
        repo_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> tuple[str | None, str | None]:
        repo = self._get_repo(repo_path)
        if not repo.is_valid:
            return (None, None)

        branch = repo.get_current_branch()
        try:
            commit = repo.get_head_commit()
        except Exception:
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
        repo = self._get_repo(repo_path)

        if no_switch_branch:
            branch = repo.get_current_branch()
            return GitWorkState(branch=branch, stash_applied=False)

        if not dry_run and not repo.is_valid:
            raise RuntimeError(f'Directory "{repo_path}" is not a git repository')

        # Read state
        current_branch = repo.get_current_branch()
        stash_applied = False
        try:
            current_commit = repo.get_head_commit()
        except Exception:
            current_commit = None

        target_commit = repo.resolve_rev(target_branch)

        # Logic from original: switch if branch or commit differs
        branch_switch_needed = (
            current_branch != target_branch and current_commit != target_commit
        )

        component_state_branch: str | None = None
        component_path: Path | None = None
        component_target_branch = component_branch or target_branch
        component_rel_path: Path | None = None
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
                    if self._is_component_submodule(
                        repo_path, component_rel_path, environment=environment
                    ):
                        pass

                if component_target_branch:
                    comp_repo = self._get_repo(component_path)
                    component_state_branch = comp_repo.get_current_branch()
            else:
                component_path = None

        should_switch_component = component_repo_detected and bool(
            component_target_branch
        )
        should_switch_root = branch_switch_needed and not should_switch_component
        component_stash_applied = False

        component_dirty = False
        if component_path is not None and should_switch_component:
            comp_repo = self._get_repo(component_path)
            component_current_branch = comp_repo.get_current_branch()
            try:
                component_current_commit = comp_repo.get_head_commit()
            except Exception:
                component_current_commit = None

            component_tgt_commit = comp_repo.resolve_rev(component_target_branch)

            component_switch_needed = True
            if component_current_branch == component_target_branch:
                if (
                    component_tgt_commit is None
                    or component_current_commit == component_tgt_commit
                ):
                    component_switch_needed = False

            if not component_switch_needed:
                should_switch_component = False
            else:
                component_dirty = comp_repo.is_dirty()

        # Handle Root Stash
        if should_switch_root:
            dirty = repo.is_dirty()
            if dirty:
                if auto_stash:
                    repo.stash(message="builder auto-stash")
                    stash_applied = True
                else:
                    raise RuntimeError(
                        "Working tree has uncommitted changes and auto_stash is disabled"
                    )

        # Handle Component Stash
        if should_switch_component and component_dirty and component_path:
            comp_repo = self._get_repo(component_path)
            if auto_stash:
                comp_repo.stash(message="builder auto-stash")
                component_stash_applied = True
            else:
                raise RuntimeError(
                    f'Component working tree at "{component_path}" has uncommitted changes; enable auto_stash to proceed'
                )

        root_switched = False
        if should_switch_root:
            # Switch Root
            try:
                repo.checkout(target_branch)
            except Exception:
                raise RuntimeError(
                    f'Unable to switch repository at "{repo_path}" to branch "{target_branch}". Run "builder update" first.'
                )
            root_switched = True

            if should_switch_component and component_path:
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
                    repo_path, dry_run=dry_run, environment=environment
                )

        elif should_switch_component and component_path:
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
        repo = self._get_repo(repo_path)

        current_branch = repo.get_current_branch()
        branch_restored = current_branch == state.branch
        restored_now = False

        if not branch_restored and state.branch:
            repo.checkout(state.branch)
            branch_restored = True
            restored_now = True

        if branch_restored and (state.root_switched or restored_now):
            self._update_submodules(repo_path, dry_run=dry_run, environment=environment)

        if state.component_path:
            comp_repo = self._get_repo(state.component_path)
            if state.component_branch:
                component_current = comp_repo.get_current_branch()
                if (
                    component_current is not None
                    and component_current != state.component_branch
                ):
                    try:
                        comp_repo.checkout(state.component_branch)
                    except Exception:
                        raise RuntimeError(
                            f'Unable to restore component repository at "{state.component_path}" to branch "{state.component_branch}".'
                        )

                self._update_submodules(
                    state.component_path, dry_run=dry_run, environment=environment
                )

            if state.component_stash_applied:
                comp_repo.stash_pop()

        if state.stash_applied:
            repo.stash_pop()

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
        repo = self._get_repo(repo_path)

        if not repo_path.exists():
            if clone_script:
                self._run_script(
                    clone_script, None, environment=environment, dry_run=dry_run
                )
            else:
                if not dry_run:
                    repo_path.mkdir(parents=True, exist_ok=True)

                repo.run_git_cmd(["init"], env=environment)
                repo.run_git_cmd(["remote", "add", "origin", url], env=environment)
                repo.run_git_cmd(["fetch", "origin"], env=environment)
                repo.run_git_cmd(["checkout", main_branch], env=environment)
                self._update_submodules(
                    repo_path, dry_run=dry_run, environment=environment, init=True
                )
            return

        if not dry_run and not repo.is_valid:
            raise RuntimeError(f'Directory "{repo_path}" is not a git repository')

        component_repo_path: Path | None = None
        component_original_branch: Optional[str] = None
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
                comp_repo = self._get_repo(candidate_path)
                component_original_branch = comp_repo.get_current_branch()

        # Step 1: stash dirty worktrees
        root_original_branch = repo.get_current_branch()
        root_switched = False

        root_stash_applied = False
        if repo.is_dirty():
            if not auto_stash:
                raise RuntimeError(
                    "Working tree has uncommitted changes; enable auto_stash to proceed"
                )
            repo.stash(message="builder auto-stash")
            root_stash_applied = True

        component_target_branch = component_branch or main_branch
        component_current_branch: Optional[str] = None
        component_stash_applied = False

        if component_repo_path is not None:
            comp_repo = self._get_repo(component_repo_path)
            component_current_branch = comp_repo.get_current_branch()

            if component_target_branch:
                component_switch_required = (
                    component_current_branch != component_target_branch
                )

            if comp_repo.is_dirty():
                if not auto_stash:
                    raise RuntimeError(
                        f'Component working tree at "{component_repo_path}" has uncommitted changes; enable auto_stash to proceed'
                    )
                comp_repo.stash(message="builder auto-stash")
                component_stash_applied = True

        # Step 2: update root
        if root_original_branch != main_branch:
            repo.checkout(main_branch)
            root_switched = True

        if update_script:
            self._run_script(
                update_script, repo_path, environment=environment, dry_run=dry_run
            )
        else:
            repo.fetch(all_remotes=True)
            try:
                repo.merge(f"origin/{main_branch}", fast_forward_only=True)
            except Exception:
                raise RuntimeError(
                    f'Unable to fast-forward main branch "{main_branch}"'
                )

        self._update_submodules(repo_path, dry_run=dry_run, environment=environment)

        if component_repo_path and component_target_branch:
            comp_repo = self._get_repo(component_repo_path)
            component_current_branch = comp_repo.get_current_branch()

        # Step 3: update component
        if component_repo_path and component_target_branch:
            comp_repo = self._get_repo(component_repo_path)
            if (
                component_switch_required
                or component_current_branch != component_target_branch
            ):
                try:
                    comp_repo.checkout(component_target_branch)
                except Exception:
                    raise RuntimeError(
                        f"Unable to switch component repository to {component_target_branch}"
                    )
                component_current_branch = component_target_branch
                component_switch_required = True

            comp_repo.fetch(all_remotes=True)
            try:
                comp_repo.merge(
                    f"origin/{component_target_branch}", fast_forward_only=True
                )
            except Exception:
                raise RuntimeError(f"Unable to fast-forward component repository")

            self._update_submodules(
                component_repo_path, dry_run=dry_run, environment=environment
            )

        # Step 4: restore component
        if component_repo_path:
            comp_repo = self._get_repo(component_repo_path)
            if (
                component_switch_required
                and component_original_branch
                and component_original_branch != component_target_branch
            ):
                comp_repo.checkout(component_original_branch)
                self._update_submodules(
                    component_repo_path, dry_run=dry_run, environment=environment
                )

            if component_stash_applied:
                comp_repo.stash_pop()

        # Step 5: restore root
        if (
            root_switched
            and root_original_branch
            and root_original_branch not in ("HEAD", None)
        ):
            repo.checkout(root_original_branch)
            extra_skip = set()
            if component_repo_path:
                # try to find relative path if it is submodule
                try:
                    rel = component_repo_path.relative_to(repo_path)
                    if self._is_component_submodule(
                        repo_path, rel, environment=environment
                    ):
                        extra_skip.add(str(rel))
                except ValueError:
                    pass

            self._update_submodules(
                repo_path,
                dry_run=dry_run,
                environment=environment,
                skip_paths=extra_skip,
            )

        if root_stash_applied:
            repo.stash_pop()

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

    def _update_submodules(
        self,
        repo_path: Path,
        *,
        dry_run: bool,
        environment: Mapping[str, str] | None = None,
        init: bool = False,
        skip_paths: set[str] | None = None,
    ) -> None:
        repo = self._get_repo(repo_path)
        args = ["submodule", "update", "--recursive"]
        if init:
            args.append("--init")

        if skip_paths:
            submodules = repo.get_submodules()
            to_update = [s.path for s in submodules if s.path not in skip_paths]
            if not to_update:
                return
            args.append("--")
            args.extend(to_update)

        repo.run_git_cmd(args, env=environment)

    def _is_git_directory(self, path: Path) -> bool:
        return (path / ".git").exists()

    def _is_component_submodule(
        self,
        repo_path: Path,
        component_dir: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> bool:
        repo = self._get_repo(repo_path)
        relative_str = component_dir.as_posix()
        escaped = relative_str.replace('"', r"\\\"")
        candidate_keys = [
            f'submodule."{escaped}".path',
            f"submodule.{relative_str}.path",
        ]

        for key in candidate_keys:
            res = repo.run_git_cmd(
                ["config", "--file", ".gitmodules", key], check=False, env=environment
            )
            if res.returncode == 0 and res.stdout.strip():
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

        repo = self._get_repo(component_path)
        current_branch = repo.get_current_branch()
        try:
            commits_match = False
            current_head = repo.get_head_commit()
            target_head = repo.resolve_rev(target_branch)
            if current_head and target_head and current_head == target_head:
                commits_match = True
        except Exception:
            commits_match = False

        branch_switch_needed = True
        if current_branch == target_branch:
            if not repo.resolve_rev(target_branch):
                branch_switch_needed = False
            elif commits_match:
                branch_switch_needed = False
        elif commits_match:
            branch_switch_needed = False  # Original was False if commits match?
            # logic: if current_branch != target_branch
            # if commits match (HEAD is same), do we switch?
            # Original: if current_commit == target_commit -> branch_switch_needed = True (to update branch ref?)
            # Wait, original logic:
            # if current_branch != target_branch:
            #    if commits_match: branch_switch_needed = True

            branch_switch_needed = True

        if branch_switch_needed:
            repo.checkout(target_branch)
            self._update_submodules(
                component_path, dry_run=dry_run, environment=environment
            )
            if original_branch and original_branch != target_branch:
                return original_branch
        return None

    def list_submodules(
        self, repo_path: Path, *, environment: Mapping[str, str] | None = None
    ) -> list[dict[str, str]]:
        repo = self._get_repo(repo_path)
        if not repo.is_valid:
            return []

        sparse = repo.is_sparse_checkout()
        ret = []
        for s in repo.get_submodules():
            if sparse:
                sub_path = repo_path.resolve() / s.path
                if not sub_path.exists():
                    continue
            ret.append({"path": s.path, "hash": s.current_commit, "url": s.url})
        return ret
