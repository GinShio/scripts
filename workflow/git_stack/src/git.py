"""Git operations wrapper module using GitRepository API.

Design: Functions in this module are convenience wrappers around
:class:`core.git_api.GitRepository`.  They operate on the repository
that contains the current working directory (``Path.cwd()``).

- READ operations are routed through *pygit2* for speed.
- WRITE / arbitrary commands still go through ``run_git`` (CLI).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from core.git_api import GitRepository


def _get_repo() -> GitRepository:
    """Return a :class:`GitRepository` rooted at the CWD repository."""
    return GitRepository(Path.cwd())


def run_git(args: List[str], check: bool = True, trim: bool = True) -> str:
    """
    Run a git command and return its stdout.

    This is the escape hatch for commands not yet covered by
    :class:`GitRepository` (e.g. ``git log``, ``git branch -f``).

    Args:
        args: List of git sub-command arguments (without leading ``git``).
        check: Whether to raise/exit on error.
        trim: Whether to strip whitespace from stdout.
    """
    try:
        repo = _get_repo()
        result = repo.run_git_cmd(args, check=check)
        output = result.stdout
        return output.strip() if trim else output
    except SystemExit:
        raise
    except Exception as e:
        if check:
            print(f"Git execution failed: {' '.join(args)}", file=sys.stderr)
            print(e, file=sys.stderr)
            sys.exit(1)
        return ""


def get_current_branch() -> str:
    """Get the current checked-out branch name (pygit2)."""
    repo = _get_repo()
    branch = repo.get_head_branch()
    return branch if branch else "HEAD"


def get_current_commit() -> str:
    """Get the current HEAD commit hash (pygit2)."""
    return _get_repo().get_head_commit()


def get_git_dir() -> str:
    """Return the ``.git`` directory path (pygit2)."""
    return str(_get_repo().git_dir)


def get_remote_names() -> List[str]:
    """List configured remote names (pygit2)."""
    return _get_repo().list_remotes()


def get_upstream_remote_name() -> str:
    """Get the name of the upstream remote (upstream if exists, else origin)."""
    remotes = get_remote_names()
    return "upstream" if "upstream" in remotes else "origin"


def resolve_base_branch(provided_base: Optional[str] = None) -> str:
    """Resolve the base branch, defaulting to main/master if not provided."""
    if provided_base:
        return provided_base

    # 1. Configured base
    cfg_base = get_stack_base()
    if cfg_base:
        return cfg_base

    # 2. Prefer upstream -> origin, then delegate to pygit2 heuristic
    remote_name = get_upstream_remote_name()
    return _get_repo().resolve_default_branch(remote=remote_name)


def get_refs_map() -> Dict[str, str]:
    """Map of local branch names → commit hashes (pygit2)."""
    return dict(_get_repo().get_branches())


def get_config(key: str, default: str = "") -> str:
    """Read a git config value (CLI, for format-safety)."""
    val = _get_repo().get_config(key)
    return val if val else default


def slugify(text: str) -> str:
    """Simple slugify: lowercase, replace non-alphanumeric with -, trim."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:50]


def get_stack_prefix() -> str:
    """Get the configured stack prefix (default: stack/)."""
    prefix = get_config("workflow.branch-prefix")
    if prefix:
        return prefix

    user_name = get_config("user.name")
    if user_name:
        slug = slugify(user_name)
        if slug:
            return f"{slug}/"

    return "stack/"


def get_stack_base() -> str:
    """Get the configured stack base branch."""
    return get_config("workflow.base-branch")
