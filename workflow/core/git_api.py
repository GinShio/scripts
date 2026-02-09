"""High-level Git API wrapper integrating pygit2 for reads and CLI for writes."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Generator, List, Mapping, Optional, Union

import pygit2

from .command_runner import (
    CommandError,
    CommandResult,
    CommandRunner,
    SubprocessCommandRunner,
)


@dataclass
class SubmoduleInfo:
    """Information about a submodule."""

    path: str
    url: str
    current_commit: str
    branch: Optional[str] = None


@dataclass
class GitCommit:
    """Represents a git commit."""

    oid: str
    message: str
    author_name: str
    author_email: str
    date: int
    parents: List[str]

    @property
    def subject(self) -> str:
        return self.message.splitlines()[0] if self.message else ""

    @property
    def body(self) -> str:
        lines = self.message.splitlines()
        if len(lines) <= 1:
            return ""
        return "\n".join(lines[1:]).strip()


class GitService(Enum):
    """Supported git hosting services."""

    AUTO = "auto"
    GITHUB = "github"
    GITLAB = "gitlab"
    GITEA = "gitea"
    CODEBERG = "codeberg"
    BITBUCKET = "bitbucket"
    AZURE = "azure"


@dataclass(frozen=True)
class RemoteInfo:
    host: str
    owner: str
    repo: str
    service: GitService

    @property
    def project_path(self) -> str:
        """Return 'owner/repo' string."""
        return f"{self.owner}/{self.repo}"

    @property
    def is_github(self) -> bool:
        return self.service == GitService.GITHUB

    @property
    def is_gitlab(self) -> bool:
        return self.service == GitService.GITLAB

    @property
    def is_gitea(self) -> bool:
        return self.service == GitService.GITEA

    @property
    def is_codeberg(self) -> bool:
        return self.service == GitService.CODEBERG

    @property
    def is_bitbucket(self) -> bool:
        return self.service == GitService.BITBUCKET

    @property
    def is_azure(self) -> bool:
        return self.service == GitService.AZURE


# --- Standalone Utilities ---


def resolve_ssh_alias(host: str) -> str:
    """Resolve SSH alias using 'ssh -G'."""
    try:
        proc = subprocess.run(
            ["ssh", "-G", host], capture_output=True, text=True, timeout=2
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if line.lower().startswith("hostname "):
                    return line.split(" ", 1)[1].strip()
    except Exception:
        pass
    return host


def normalize_domain(domain: str) -> str:
    """Normalize specialized domains to their main service domain."""
    domain = domain.lower()
    mapping = {
        "ssh.github.com": "github.com",
        "altssh.gitlab.com": "gitlab.com",
        "ssh.dev.azure.com": "dev.azure.com",
        "vs-ssh.visualstudio.com": "visualstudio.com",
        "altssh.bitbucket.org": "bitbucket.org",
    }
    return mapping.get(domain, domain)


def parse_remote_url(url: str) -> Optional[RemoteInfo]:
    """
    Parse a Git remote URL into (host, owner, repo).
    Handles SSH aliases, scp-syntax, and standard URIs.
    """
    if not url:
        return None

    domain = ""
    path = ""

    # 1. Check for standard SCP-like SSH syntax: user@host:path/to/repo.git
    sc_match = re.match(r"^(?:[^@]+@)?([^:]+):(.+)$", url)
    is_uri = any(url.startswith(p) for p in ["http:", "https:", "ssh:", "git:"])

    if sc_match and not is_uri:
        raw_host = sc_match.group(1)
        path = sc_match.group(2)
        resolved_host = resolve_ssh_alias(raw_host)
        domain = normalize_domain(resolved_host)
    else:
        # 2. Generic URI matching
        match = re.search(
            r"^(?:ssh|git|https?)://(?:[^@/]+@)?([^:/]+)(?::\d+)?/(.+)$", url
        )
        if match:
            raw_host = match.group(1)
            path = match.group(2)
            resolved_host = resolve_ssh_alias(raw_host)
            domain = normalize_domain(resolved_host)

    if domain and path:
        if path.endswith(".git"):
            path = path[:-4]
        path = path.strip("/")

        parts = path.split("/")
        if len(parts) >= 2:
            repo = parts[-1]
            owner = "/".join(parts[:-1])

            service = GitService.AUTO
            if domain in ("github.com", "www.github.com"):
                service = GitService.GITHUB
            elif domain in ("gitlab.com", "www.gitlab.com") or "gitlab" in domain:
                service = GitService.GITLAB
            elif domain in ("codeberg.org", "www.codeberg.org"):
                service = GitService.CODEBERG
            elif "gitea" in domain:
                service = GitService.GITEA
            elif domain in ("bitbucket.org", "www.bitbucket.org"):
                service = GitService.BITBUCKET
            elif domain in ("dev.azure.com", "visualstudio.com"):
                service = GitService.AZURE

            return RemoteInfo(host=domain, owner=owner, repo=repo, service=service)

    return None


class GitRepository:
    """
    High-level API for git operations.

    Design Philosophy:
    - READ operations use pygit2 for performance and structured data.
    - WRITE operations use Git CLI to ensure hooks run and config is respected.
    - CONFIG operations use Git CLI to avoid libgit2/git format incompatibilities.
    """

    def __init__(
        self, path: Path | str, runner: Optional[CommandRunner] = None
    ) -> None:
        self.path = Path(path).resolve()
        self._repo: Optional[pygit2.Repository] = None
        self._runner = runner or SubprocessCommandRunner()

    # --- Core & Properties (pygit2) ---

    def open(self) -> None:
        """Opens the repository. Raises exception if not found."""
        try:
            self._repo = pygit2.Repository(str(self.path))
        except pygit2.GitError as e:
            raise RuntimeError(f"Failed to open repository at {self.path}: {e}")

    @property
    def repo(self) -> pygit2.Repository:
        """Access the underlying pygit2 Repository object (Read-only usage recommended)."""
        if self._repo is None:
            self.open()
        return self._repo  # type: ignore

    @property
    def is_valid(self) -> bool:
        """Checks if the path is a valid git repository."""
        if not self.path.exists():
            return False
        try:
            # We explicitly check for .git or if it's a bare repo
            self.open()
            return True
        except Exception:
            return False

    @property
    def root_dir(self) -> Path:
        """Returns the working directory root path."""
        # Bare repos have no working directory, return path
        if self.repo.is_bare:
            return Path(self.repo.path)
        workdir = self.repo.workdir
        return Path(workdir) if workdir else self.path

    @property
    def working_dir(self) -> Path:
        """Alias for root_dir for backward compatibility."""
        return self.root_dir

    @property
    def git_dir(self) -> Path:
        """Returns the .git directory path."""
        return Path(self.repo.path)

    def relpath(self, path: Union[str, Path]) -> Path:
        """Returns path relative to the repository root."""
        try:
            p = Path(path).resolve()
            return p.relative_to(self.root_dir)
        except ValueError:
            # If path is not relative to root, return absolute or original
            return Path(path)

    # --- CLI Helper ---

    def _run_git(
        self,
        args: List[str],
        check: bool = True,
        capture: bool = True,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        """Internal helper to run git CLI commands in this repo."""
        # Use root_dir for standard repos, or path for bare repos
        # If repo is not valid (e.g. during init), use path
        try:
            cwd = self.root_dir
        except Exception:
            cwd = self.path

        return self._runner.run(
            ["git"] + args, cwd=cwd, env=env, check=check, stream=not capture
        )

    def run_git_cmd(
        self,
        args: List[str],
        check: bool = True,
        capture: bool = True,
        env: Optional[Mapping[str, str]] = None,
    ) -> CommandResult:
        """
        Run arbitrary git command in the repository context.
        Useful for complex commands not covered by high-level API.
        """
        return self._run_git(args, check, capture, env)

    # --- Configuration (CLI) ---

    def get_config(self, key: str) -> Optional[str]:
        """Gets a git config value via CLI."""
        res = self._run_git(["config", "--get", key], check=False)
        if res.returncode != 0:
            return None
        return res.stdout.strip()

    def get_config_all(self, key: str) -> List[str]:
        """Gets all values for a git config key via CLI."""
        res = self._run_git(["config", "--get-all", key], check=False)
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def set_config(self, key: str, value: str, scope: str = "local") -> None:
        """
        Sets a git config value via CLI.
        scope: 'local', 'global', or 'system'
        """
        self._run_git(["config", f"--{scope}", key, value])

    def unset_config(self, key: str, scope: str = "local") -> None:
        """Unsets a git config value via CLI."""
        self._run_git(["config", f"--{scope}", "--unset", key], check=False)

    def is_sparse_checkout(self) -> bool:
        """Checks if sparse checkout is enabled."""
        val = self.get_config("core.sparseCheckout")
        return val is not None and val.lower() in ("true", "1", "yes", "on")

    # --- Status & Inspection (pygit2) ---

    def get_head_branch(self) -> Optional[str]:
        """Returns the current branch name, or None if detached HEAD."""
        try:
            if self.repo.head_is_detached:
                return None
            return self.repo.head.shorthand
        except pygit2.GitError:
            # Handle unborn HEAD (empty repo properly initialized)
            try:
                head = self.repo.lookup_reference("HEAD")
                target = head.target
                # target is str for symbolic ref
                if isinstance(target, str) and target.startswith("refs/heads/"):
                    return target[11:]
            except Exception:
                pass
            return None

    def get_current_branch(self) -> Optional[str]:
        """Alias for get_head_branch."""
        return self.get_head_branch()

    def get_head_commit(self) -> str:
        """Returns the full commit hash of HEAD."""
        return str(self.repo.head.target)

    def resolve_rev(self, spec: str) -> Optional[str]:
        """Resolves a revision (branch, tag, sha) to a full commit hash."""
        try:
            obj = self.repo.revparse_single(spec)
            return str(obj.id)
        except (KeyError, pygit2.GitError):
            return None

    def resolve_commit(self, spec: str) -> Optional[str]:
        """Alias for resolve_rev."""
        return self.resolve_rev(spec)

    def is_dirty(self, untracked: bool = False) -> bool:
        """
        Checks if working directory has uncommitted changes.
        Uses pygit2 for speed.
        """
        try:
            mode = "normal" if untracked else "no"
            status = self.repo.status(untracked_files=mode)
            return len(status) > 0
        except Exception:
            return False

    def resolve_default_branch(self, remote: str = "origin") -> str:
        """
        Heuristic to resolve the default branch (main/master).
        """
        # 1. Configured base
        cfg_base = self.get_config("workflow.base-branch")
        if cfg_base:
            return cfg_base

        # 2. Try to guess from remote HEAD
        remote_prefix = f"refs/remotes/{remote}/"
        try:
            sym_ref = self.repo.lookup_reference(f"{remote_prefix}HEAD")
            target = sym_ref.target
            if target.startswith(remote_prefix):
                return target[len(remote_prefix) :]
        except (KeyError, ValueError, pygit2.GitError):
            pass

        # 3. Fallback check for existence of main/master
        for candidate in ["main", "master", "trunk", "development"]:
            try:
                self.repo.lookup_reference(f"{remote_prefix}{candidate}")
                return candidate
            except (KeyError, pygit2.GitError):
                continue

        return "main"

    def get_commits(
        self, rev_range: str, order: int = pygit2.GIT_SORT_TOPOLOGICAL
    ) -> List[GitCommit]:
        """
        Get list of commits for a range (e.g. 'main..feature').
        Uses pygit2 for efficiency.
        """
        try:
            if ".." in rev_range:
                start, end = rev_range.split("..", 1)
                # handle empty start implies HEAD if not careful, but usually explicit.
                # If start is empty, revparse fails?
                if not start:
                    # ..end -> reachable from end? No, usually range implies DAG subset.
                    # let's assume valid git range refs.
                    pass

                end_obj = self.repo.revparse_single(end)
                start_obj = self.repo.revparse_single(start)

                walker = self.repo.walk(end_obj.id, order)
                walker.hide(start_obj.id)
            else:
                # Just reachable from rev
                obj = self.repo.revparse_single(rev_range)
                walker = self.repo.walk(obj.id, order)

            commits = []
            for commit in walker:
                commits.append(
                    GitCommit(
                        oid=str(commit.id),
                        message=commit.message,
                        author_name=commit.author.name,
                        author_email=commit.author.email,
                        date=commit.commit_time,
                        parents=[str(p.id) for p in commit.parents],
                    )
                )
            return commits
        except Exception:
            return []

    def get_branches(self) -> Mapping[str, str]:
        """
        Get all local branches and their tips.
        Returns: Dict[branch_name, commit_sha]
        """
        branches = {}
        try:
            for ref_name in self.repo.listall_references():
                if ref_name.startswith("refs/heads/"):
                    name = ref_name[11:]
                    target = self.repo.lookup_reference(ref_name).target
                    if hasattr(target, "hex"):
                        branches[name] = target.hex
                    else:
                        branches[name] = str(target)
        except Exception:
            pass
        return branches

    # --- Remote Management (pygit2 READ / CLI WRITE) ---

    def list_remotes(self) -> List[str]:
        """List remote names."""
        return [r.name for r in self.repo.remotes]

    def get_remote_url(self, name: str, push: bool = False) -> Optional[str]:
        """Get fetch or push URL for a remote (returns first one)."""
        urls = self.get_remote_urls(name, push=push)
        return urls[0] if urls else None

    def get_remote_urls(self, name: str, push: bool = False) -> List[str]:
        """(CLI) Get all fetch or push URLs for a remote."""
        args = ["remote", "get-url", "--all"]
        if push:
            args.append("--push")
        args.append(name)

        res = self._run_git(args, check=False)
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def add_remote(self, name: str, url: str) -> None:
        """Add a new remote via CLI."""
        self._run_git(["remote", "add", name, url])

    def rename_remote(self, old_name: str, new_name: str) -> None:
        """Rename a remote via CLI."""
        self._run_git(["remote", "rename", old_name, new_name])

    def set_remote_url(
        self, name: str, url: str, push: bool = False, add: bool = False
    ) -> None:
        """
        Set remote URL via CLI.
        If add=True, adds an extra URL (e.g. for pushing to multiple mirrors).
        """
        args = ["remote", "set-url"]
        if push:
            args.append("--push")
        if add:
            args.append("--add")
        args.extend([name, url])
        self._run_git(args)

    # --- Write Actions (CLI) ---

    def fetch(
        self, remote: str = "origin", prune: bool = True, all_remotes: bool = False
    ) -> None:
        """Fetch from remote."""
        if all_remotes:
            args = ["fetch", "--all"]
        else:
            args = ["fetch", remote]

        if prune:
            args.append("--prune")
        self._run_git(args)

    def checkout(
        self, target: str, force: bool = False, create_branch: Optional[str] = None
    ) -> None:
        """
        Checkout a branch or commit.
        """
        args = ["checkout"]
        if force:
            args.append("-f")
        if create_branch:
            args.extend(["-b", create_branch])

        args.append(target)
        self._run_git(args)

    def add(self, paths: List[str] | List[Path]) -> None:
        """Stage files."""
        if not paths:
            return
        args = ["add"] + [str(p) for p in paths]
        self._run_git(args)

    def commit(self, message: str, allow_empty: bool = False) -> None:
        """Commit staged changes."""
        args = ["commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        self._run_git(args)

    def push(
        self,
        remote: str = "origin",
        refspec: Optional[str] = None,
        force: bool = False,
        force_with_lease: bool = False,
    ) -> None:
        """Push to remote."""
        args = ["push"]
        if force:
            args.append("--force")
        if force_with_lease:
            args.append("--force-with-lease")
        args.append(remote)
        if refspec:
            args.append(refspec)
        self._run_git(args)

    def merge(self, target: str, fast_forward_only: bool = False) -> None:
        """Merge target into current branch."""
        args = ["merge", target]
        if fast_forward_only:
            args.append("--ff-only")
        self._run_git(args)

    def stash(
        self, message: Optional[str] = None, include_untracked: bool = False
    ) -> bool:
        """
        Stash changes. Returns True if a stash was created.
        """
        args = ["stash", "push"]
        if include_untracked:
            args.append("--include-untracked")
        if message:
            args.extend(["-m", message])

        res = self._run_git(args, check=False)
        return res.returncode == 0 and "No local changes to save" not in res.stdout

    def stash_pop(self) -> None:
        """Pop the latest stash."""
        self._run_git(["stash", "pop"])

    @contextmanager
    def safe_checkout(
        self, target: str, auto_stash: bool = True, force: bool = False
    ) -> Generator[None, None, None]:
        """
        Context manager to safely checkout a target, optionally stashing changes.
        Restores original state (branch) on exit can be tricky, so this specifically
        just handles the "stash -> checkout" flow safely.

        If you want to return to the original branch, you should handle that in the caller.
        """
        dirty = self.is_dirty()
        stashed = False

        if dirty:
            if auto_stash:
                stashed = self.stash(
                    message=f"Safe checkout stash for {target}", include_untracked=True
                )
            elif not force:
                raise RuntimeError(
                    "Working directory is dirty and auto-stash is disabled."
                )

        try:
            self.checkout(target, force=force)
            yield
        finally:
            if stashed:
                # Check if we can pop. If checkout changed base significantly, pop might conflict.
                # But we attempt it.
                try:
                    self.stash_pop()
                except Exception:
                    print("Warning: Failed to pop stash after safe checkout.")

    # --- Submodules (Reads via pygit2, Updates via CLI) ---

    def get_submodules(self) -> List[SubmoduleInfo]:
        """
        Lists all submodules with their status.
        Uses pygit2 for listing paths and accessing submodule repos,
        but CLI for config reading since pygit2.Repository.lookup_submodule is missing in this version.
        """
        submodules = []
        # Force reload to ensure freshness
        self._repo = None

        paths = []
        try:
            paths = self.repo.listall_submodules()
        except Exception:
            pass

        for path in paths:
            try:
                # 1. Get current commit in submodule workdir
                current_commit = "0000000000000000000000000000000000000000"
                sub_path_abs = self.root_dir / path

                # Try to open submodule repo to get HEAD
                if sub_path_abs.exists():
                    try:
                        # Open directly using pygit2
                        sub_repo = pygit2.Repository(str(sub_path_abs))
                        if not sub_repo.head_is_unborn:
                            current_commit = str(sub_repo.head.target)
                    except Exception:
                        pass

                # 2. Get URL and Branch
                # Helper to get config value
                def get_cfg(source_args: List[str], key: str) -> Optional[str]:
                    cmd = (
                        ["git", "-C", str(self.root_dir), "config"]
                        + source_args
                        + ["--get", key]
                    )
                    res = self._runner.run(cmd, check=False)
                    return res.stdout.strip() if res.returncode == 0 else None

                # URL: prefer .git/config (active), fallback to .gitmodules
                url = get_cfg([], f"submodule.{path}.url")
                if not url:
                    url = (
                        get_cfg(["--file", ".gitmodules"], f"submodule.{path}.url")
                        or ""
                    )

                # Branch: .gitmodules
                branch = get_cfg(["--file", ".gitmodules"], f"submodule.{path}.branch")

                submodules.append(
                    SubmoduleInfo(
                        path=path, url=url, current_commit=current_commit, branch=branch
                    )
                )
            except Exception:
                continue

        return submodules

    def update_submodules(self, recursive: bool = True, init: bool = False) -> None:
        """Updates submodules (CLI)."""
        args = ["submodule", "update"]
        if init:
            args.append("--init")
        if recursive:
            args.append("--recursive")
        self._run_git(args)

    # --- Factory ---

    @staticmethod
    def init(path: Path | str, initial_branch: str = "main") -> GitRepository:
        """Initialize a new git repository (CLI)."""
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        runner = SubprocessCommandRunner()
        runner.run(["git", "init", "-b", initial_branch, str(path)], check=True)
        return GitRepository(path, runner=runner)

    @staticmethod
    def init_repository(
        path: Path | str, origin_url: Optional[str] = None, dry_run: bool = False
    ) -> GitRepository:
        """Legacy compatibility wrapper for init."""
        if dry_run:
            print(f"[Dry-run] init repo at {path}")
            return GitRepository(path)

        repo = GitRepository.init(path)
        if origin_url:
            repo.add_remote("origin", origin_url)
        return repo
