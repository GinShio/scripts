"""High-level Git API wrapper using pygit2."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Union

import pygit2


@dataclass
class SubmoduleInfo:
    """Information about a submodule."""
    path: str
    url: str
    current_commit: str
    branch: Optional[str] = None


class GitRepository:
    """
    High-level API for git operations using pygit2.
    Replaces usages of CLI git commands in workflow scripts.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self._repo: Optional[pygit2.Repository] = None

    def open(self) -> None:
        """Opens the repository. Raises exception if not found."""
        try:
            self._repo = pygit2.Repository(str(self.path))
        except pygit2.GitError as e:
            raise RuntimeError(f"Failed to open repository at {self.path}: {e}")

    @property
    def repo(self) -> pygit2.Repository:
        """Access the underlying pygit2 Repository object."""
        if self._repo is None:
            self.open()
        return self._repo

    @property
    def is_valid(self) -> bool:
        """Checks if the path is a valid git repository."""
        try:
            pygit2.Repository(str(self.path))
            return True
        except Exception:
            return False

    @property
    def working_dir(self) -> Optional[Path]:
        """Returns the working directory root path."""
        try:
            wd = self.repo.workdir
            return Path(wd) if wd else None
        except Exception:
            return None

    @property
    def git_dir(self) -> Optional[Path]:
        """Returns the .git directory path."""
        try:
            gd = self.repo.path
            return Path(gd) if gd else None
        except Exception:
            return None

    # --- Configuration ---

    def get_config(self, key: str) -> Optional[Union[str, int, bool]]:
        """Gets a git config value."""
        try:
            # pygit2 config getter behaves like dict-ish but depends on type
            # We can use get_bool, get_int, etc if we know type.
            # But generic get usually returns string or value.
            # config[key] returns the value.
            return self.repo.config[key]
        except (KeyError, pygit2.GitError):
            return None

    def set_config(self, key: str, value: Any, local: bool = True) -> None:
        """Sets a git config value. Local defaults to repo config."""
        # For full "local vs global" control, we might need to access specific level
        # self.repo.config is the aggregate.
        # To write to local, we can use:
        # repo.config.add_file_ondisk(path, level, repo) ... but that's for loading.
        # repo.config[key] = value writes to the highest priority config file usually local.
        # To be safe for "local" specifically:
        try:
            if local:
                # This ensures it writes to the repository's configuration
                # pygit2's Config object handles writes to the appropriate place
                self.repo.config[key] = value
            else:
                pygit2.Config.get_global_config()[key] = value
        except pygit2.GitError as e:
            raise RuntimeError(f"Failed to set config {key}: {e}")

    def unset_config(self, key: str) -> None:
        """Unsets a git config value."""
        try:
            del self.repo.config[key]
        except (KeyError, pygit2.GitError):
            pass

    # --- Status & Inspection ---

    def get_current_branch(self) -> Optional[str]:
        """Returns the current branch name (shorthand), or None if detached HEAD."""
        if self.repo.head_is_detached:
            return None
        return self.repo.head.shorthand

    def get_head_commit(self) -> str:
        """Returns the full commit hash of HEAD."""
        return str(self.repo.head.target)

    def resolve_commit(self, revision: str) -> Optional[str]:
        """Resolves a revision (branch, tag, sha) to a full commit hash."""
        try:
            obj = self.repo.revparse_single(revision)
            return str(obj.id)
        except (KeyError, pygit2.GitError):
            return None

    def is_dirty(self, ignore_submodules: bool = True) -> bool:
        """Checks if working directory has uncommitted changes."""
        # pygit2 status returns a dict of path -> flags
        status_opts: dict[str, Any] = {}
        if ignore_submodules:
            # GIT_STATUS_OPT_EXCLUDE_SUBMODULES = (1 << 5) ? No directly in pygit2 main namespace sometimes?
            # pygit2 usually maps options to kwargs or Enums
            pass
        
        # Simple check: if status is not empty, it's dirty.
        # However, we only care about modification, addition, deletion, etc.
        # We generally want to ignore untracked files if 'git status --porcelain' behavior is mimic'd roughly,
        # but usually untracked files are considered dirty in some contexts.
        # existing git_manager uses: --untracked-files=no
        
        # pygit2.GIT_STATUS_SHOW_INDEX_AND_WORKDIR is default
        status = self.repo.status(untracked_files="no")
        return len(status) > 0

    def is_sparse_checkout(self) -> bool:
        """Checks git config for sparse checkout."""
        try:
            config = self.repo.config
            # core.sparseCheckout is a boolean
            val = config.get_bool("core.sparseCheckout")
            return val
        except (KeyError, ValueError):
            return False

    # --- Actions: Switch/Checkout ---

    def checkout(self, target: str, force: bool = False, dry_run: bool = False) -> None:
        """
        Switches to a branch or commit.
        Equivalents: 'git switch', 'git checkout'.
        """
        if dry_run:
            print(f"[Dry-run] would checkout '{target}' in {self.path}")
            return

        # Resolve target to an object (commit/branch)
        # If it's a branch name
        repo = self.repo
        
        # Check if local branch exists
        branch = repo.lookup_branch(target)
        if branch:
            ref = branch
            repo.checkout(ref)
            # Update HEAD
            # checkout(ref) usually updates HEAD if ref is a branch
        else:
            # Try as a commit/tag/remote branch
            try:
                # Resolve reference or revision
                obj = repo.revparse_single(target)
                repo.checkout_tree(obj)
                if not repo.head_is_detached:
                    repo.set_head(obj.id)
                else:
                    repo.set_head(obj.id)
            except (KeyError, pygit2.GitError):
                raise ValueError(f"Target '{target}' not found in repository.")

    def create_branch(self, branch_name: str, point_at: str, dry_run: bool = False) -> None:
        if dry_run:
            print(f"[Dry-run] would create branch '{branch_name}' at '{point_at}' in {self.path}")
            return
        # Implementation to come if needed
        pass

    # --- Actions: Sync ---

    def fetch(self, remote_name: str = "origin", dry_run: bool = False) -> None:
        """Fetches from the specified remote."""
        if dry_run:
            print(f"[Dry-run] would fetch remote '{remote_name}' in {self.path}")
            return

        try:
            remote = self.repo.remotes[remote_name]
        except KeyError:
            raise ValueError(f"Remote '{remote_name}' not found.")
        
        remote.fetch()

    def merge_fast_forward(self, target_commit: str, dry_run: bool = False) -> None:
        """
        Performs a fast-forward merge to target_commit.
        Raises error if not possible.
        """
        if dry_run:
            print(f"[Dry-run] would fast-forward merge HEAD to '{target_commit}' in {self.path}")
            return

        repo = self.repo
        try:
            target_obj = repo.revparse_single(target_commit)
        except KeyError:
            raise ValueError(f"Commit '{target_commit}' not found.")

        target_oid = target_obj.id

        # Merge analysis
        analysis, _ = repo.merge_analysis(target_oid)

        if analysis & pygit2.GIT_MERGE_ANALYSIS_FASTFORWARD:
            # Perform Fast-forward
            # 1. Checkout tree
            repo.checkout_tree(target_obj)
            # 2. Update HEAD to point to new commit
            # If HEAD is a branch, update the branch ref
            if not repo.head_is_detached:
                head_ref = repo.head
                head_ref.set_target(target_oid)
            else:
                repo.set_head(target_oid)
        elif analysis & pygit2.GIT_MERGE_ANALYSIS_UP_TO_DATE:
            # Nothing to do
            return
        else:
             raise RuntimeError(f"Cannot fast-forward merge to {target_commit}. Analysis result: {analysis}")

    # --- Actions: Stash ---

    def stash_save(self, message: str, dry_run: bool = False) -> bool:
        """
        Stashes changes. Returns True if stash was created.
        """
        if dry_run:
            dirty = self.is_dirty()
            if dirty:
                print(f"[Dry-run] would stash changes with message '{message}' in {self.path}")
            return dirty

        try:
            # Get default signature for stasher
            sig = self.repo.default_signature
            self.repo.stash(sig, message)
            return True
        except pygit2.GitError:
            # Usually raises if nothing to stash
            return False

    def stash_pop(self, dry_run: bool = False) -> None:
        """Pops the latest stash."""
        if dry_run:
            print(f"[Dry-run] would pop stash in {self.path}")
            return

        try:
            self.repo.stash_pop()
        except pygit2.GitError as e:
            raise RuntimeError(f"Stash pop failed: {e}")

    # --- Submodules ---

    def get_submodules(self) -> List[SubmoduleInfo]:
        """Lists all submodules with their status."""
        submodules = []
        for name in self.repo.listall_submodules():
            try:
                sub = self.repo.lookup_submodule(name)
                current_commit = str(sub.head_id) if sub.head_id else "0000000000000000000000000000000000000000"
                
                submodules.append(SubmoduleInfo(
                    path=sub.path,
                    url=sub.url or "",
                    current_commit=current_commit,
                    branch=sub.branch
                ))
            except Exception:
                continue
        return submodules

    def update_submodules(self, recursive: bool = True, init: bool = False, dry_run: bool = False) -> None:
        """Updates submodules."""
        if dry_run:
            print(f"[Dry-run] would update submodules (recursive={recursive}, init={init}) in {self.path}")
            return

        # pygit2's submodule update support is limited compared to CLI.
        # It typically requires iterating and calling update() on each submodule.
        for name in self.repo.listall_submodules():
            sub = self.repo.lookup_submodule(name)
            sub.update(init=init)
            if recursive:
                # Recursively open submodule repo and update its submodules
                try:
                    sub_repo_path = self.path / sub.path
                    if sub_repo_path.exists():
                        sub_repo = GitRepository(sub_repo_path)
                        sub_repo.update_submodules(recursive=True, init=init)
                except Exception:
                    pass

    # --- Factory/Init ---

    @staticmethod
    def init_repository(path: Path | str, origin_url: Optional[str] = None, dry_run: bool = False) -> GitRepository:
        """Initializes a new repo and optional remote."""
        path_obj = Path(path)
        
        if dry_run:
            print(f"[Dry-run] would init git repository at {path_obj}")
            if origin_url:
                print(f"[Dry-run] would add origin {origin_url}")
            return GitRepository(path_obj)

        path_obj.mkdir(parents=True, exist_ok=True)
        pygit2.init_repository(str(path_obj))
        
        repo_wrapper = GitRepository(path_obj)
        repo_wrapper.open()
        
        if origin_url:
            repo_wrapper.repo.remotes.create("origin", origin_url)
            
        return repo_wrapper
