"""Git CLI wrapper module."""

from __future__ import annotations

import re
import sys
from typing import Dict, List, Optional

from core.command_runner import CommandError, SubprocessCommandRunner

_RUNNER = SubprocessCommandRunner()


def run_git(args: List[str], check: bool = True, trim: bool = True) -> str:
    """
    Run a git command and return its stdout.

    Args:
        args: List of git arguments.
        check: Whether to raise/exit on error.
        trim: Whether to strip whitespace from stdout.
    """
    try:
        result = _RUNNER.run(["git"] + args, check=check)
        output = result.stdout
        return output.strip() if trim else output
    except CommandError as e:
        if check:
            # Propagate error message to stderr but keep it clean for CLI tools
            print(f"Git execution failed: {' '.join(args)}", file=sys.stderr)
            print(e, file=sys.stderr)
            sys.exit(1)
        return ""
    except Exception as e:
        if check:
            print(f"Unexpected git error: {e}", file=sys.stderr)
            sys.exit(1)
        return ""


def get_current_branch() -> str:
    """Get the current checked out branch name."""
    return run_git(["rev-parse", "--abbrev-ref", "HEAD"])


def get_current_commit() -> str:
    """Get the current HEAD commit hash."""
    return run_git(["rev-parse", "HEAD"])


def get_git_dir() -> str:
    """Correctly locate .git directory."""
    return run_git(["rev-parse", "--git-dir"])


def get_remote_names() -> List[str]:
    """Get list of configured remotes."""
    out = run_git(["remote"], check=False)
    return [r.strip() for r in out.splitlines() if r.strip()]


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

    # Prefer upstream -> origin
    remote_name = get_upstream_remote_name()
    remote_prefix = f"refs/remotes/{remote_name}/"

    # 1. Check local tracking info (fastest)
    # 1.1 Verify if 'refs/remotes/<remote>/HEAD' is missing, try to detect it once?
    # This invokes network and is slow, so we only implicitly trust if cached.
    # Alternatively, users should run `git remote set-head <remote> -a`
    base_branch = run_git(["symbolic-ref", f"{remote_prefix}HEAD"], check=False)
    if base_branch != "":
        return base_branch.removeprefix(remote_prefix)

    # 2. Guess common names
    for candidate in ("main", "master", "trunk", "development"):
        # Check remote ref
        if run_git(
            ["show-ref", "--verify", "--quiet", f"{remote_prefix}{candidate}"],
            check=False,
        ):
            # If show-ref --quiet outputs nothing but succeeds (exit 0), run_git returns "".
            # If it fails (exit 1), run_git returns "".
            # We can't distinguish with current run_git wrapper checking only output.
            # So we must NOT use --quiet and rely on output presence.
            pass

        # We rely on output being non-empty if found. remove --quiet.
        if run_git(
            ["show-ref", "--verify", f"{remote_prefix}{candidate}"],
            check=False,
        ):
            return candidate

        if run_git(["show-ref", "--verify", f"refs/heads/{candidate}"], check=False):
            return candidate

    # 3. Fallback
    return "master"


def get_refs_map() -> Dict[str, str]:
    """Get a map of local branch names to their commit hashes."""
    # format: <refname:short> <objectname>
    out = run_git(
        ["for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/"]
    )
    refs = {}
    for line in out.splitlines():
        if not line:
            continue
        parts = line.split(" ")
        if len(parts) == 2:
            refs[parts[0]] = parts[1]
    return refs


def get_config(key: str, default: str = "") -> str:
    """Read a git config value."""
    val = run_git(["config", "--get", key], check=False)
    return val if val else default


def slugify(text: str) -> str:
    """Simple slugify: lowercase, replace non-alphanumeric with -, trim."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text[:50]


def get_stack_prefix() -> str:
    """Get the configured stack prefix (default: stack/)."""
    # 1. Check stack.prefix
    prefix = get_config("stack.prefix")
    if prefix:
        return prefix

    # 2. Check user.name for default prefix
    user_name = get_config("user.name")
    if user_name:
        slug = slugify(user_name)
        if slug:
            return f"{slug}/"

    return "stack/"


def get_stack_base() -> str:
    """Get the configured stack base branch."""
    return get_config("stack.base")
