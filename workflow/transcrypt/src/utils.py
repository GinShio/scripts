from __future__ import annotations

#
# transcrypt - https://github.com/elasticdog/transcrypt
#
# A script to configure transparent encryption of sensitive files stored in
# a Git repository. It utilizes OpenSSL's symmetric cipher routines and follows
# the gitattributes(5) man page regarding the use of filters.
#
# Copyright (c) 2019-2025 James Murty <james@murty.co>
# Copyright (c) 2014-2020 Aaron Bull Schaefer <aaron@elasticdog.com>
#
# Add PR https://github.com/elasticdog/transcrypt/pull/132 for salt and pbkdf2
# Copyright (c) 2022-2025 Jon Crall <jon.crall@kitware.com>
#
# This source code is provided under the terms of the MIT License
# that can be be found in the LICENSE file.
#
# --------------------------------------------------------------------------------
# Ported to Python by GinShio
# Copyright (c) 2026 GinShio
# --------------------------------------------------------------------------------
#

import sys
from pathlib import Path
from typing import Optional

from core.command_runner import SubprocessCommandRunner, CommandResult

runner = SubprocessCommandRunner()

def get_git_root() -> Path:
    """Get the root directory of the current git repository."""
    try:
        res = runner.run(["git", "rev-parse", "--show-toplevel"], check=True)
        return Path(res.stdout.strip())
    except Exception:
        # Fallback to current directory or exit if not in git
        print("Error: Not a git repository", file=sys.stderr)
        sys.exit(1)

def get_git_config(key: str) -> Optional[str]:
    """Get a git config value, returning None if not set."""
    res = runner.run(["git", "config", "--get", key], check=False)
    if res.returncode != 0:
        return None
    return res.stdout.strip()

def set_git_config(key: str, value: str):
    """Set a local git config value."""
    runner.run(["git", "config", "--local", key, value], check=True)

def unset_git_config(key: str):
    """Unset a local git config value."""
    runner.run(["git", "config", "--local", "--unset", key], check=False)

def get_git_dir() -> Path:
    """Get the .git directory path."""
    res = runner.run(["git", "rev-parse", "--git-dir"], check=True)
    return Path(res.stdout.strip())

def is_git_repo() -> bool:
    res = runner.run(["git", "rev-parse", "--is-inside-work-tree"], check=False)
    return res.returncode == 0

def get_relative_path(path: Path) -> Path:
    """Get path relative to git root."""
    root = get_git_root()
    try:
        return path.absolute().relative_to(root.absolute())
    except ValueError:
        # Not inside git root
        return path.absolute()
