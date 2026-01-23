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
        result = _RUNNER.run(['git'] + args, check=check)
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
    return run_git(['rev-parse', '--abbrev-ref', 'HEAD'])


def get_current_commit() -> str:
    """Get the current HEAD commit hash."""
    return run_git(['rev-parse', 'HEAD'])


def get_git_dir() -> str:
    """Correctly locate .git directory."""
    return run_git(['rev-parse', '--git-dir'])


def resolve_base_branch(provided_base: Optional[str] = None) -> str:
    """Resolve the base branch, defaulting to main/master if not provided."""
    if provided_base:
        return provided_base

    # 1. Configured base
    cfg_base = get_stack_base()
    if cfg_base:
        return cfg_base

    # 2. Try to guess default branch
    base_branch = 'main'
    if run_git(['rev-parse', '--verify', 'main'], check=False) == "":
        if run_git(['rev-parse', '--verify', 'master'], check=False) != "":
            base_branch = 'master'
        # Default to main anyway if neither found, or let it fail downstream
    return base_branch


def get_refs_map() -> Dict[str, str]:
    """Get a map of local branch names to their commit hashes."""
    # format: <refname:short> <objectname>
    out = run_git(
        ['for-each-ref', '--format=%(refname:short) %(objectname)', 'refs/heads/'])
    refs = {}
    for line in out.splitlines():
        if not line:
            continue
        parts = line.split(' ')
        if len(parts) == 2:
            refs[parts[0]] = parts[1]
    return refs


def get_config(key: str, default: str = "") -> str:
    """Read a git config value."""
    val = run_git(['config', '--get', key], check=False)
    return val if val else default


def slugify(text: str) -> str:
    """Simple slugify: lowercase, replace non-alphanumeric with -, trim."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '-', text)
    text = text.strip('-')
    return text[:50]


def get_stack_prefix() -> str:
    """Get the configured stack prefix (default: stack/)."""
    # 1. Check stack.prefix
    prefix = get_config('stack.prefix')
    if prefix:
        return prefix

    # 2. Check user.name for default prefix
    user_name = get_config('user.name')
    if user_name:
        slug = slugify(user_name)
        if slug:
            return f"{slug}/"

    return "stack/"


def get_stack_base() -> str:
    """Get the configured stack base branch."""
    return get_config('stack.base')
