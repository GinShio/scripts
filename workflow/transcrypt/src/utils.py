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

from core.git_api import GitRepository


def _get_repo() -> GitRepository:
    """Return a :class:`GitRepository` for the CWD repository."""
    return GitRepository(Path.cwd())


def get_git_root() -> Path:
    """Get the root directory of the current git repository."""
    try:
        return _get_repo().root_dir
    except Exception:
        print("Error: Not a git repository", file=sys.stderr)
        sys.exit(1)


def get_git_config(key: str) -> Optional[str]:
    """Get a git config value, returning None if not set."""
    return _get_repo().get_config(key)


def set_git_config(key: str, value: str):
    """Set a local git config value."""
    _get_repo().set_config(key, value, scope="local")


def unset_git_config(key: str):
    """Unset a local git config value."""
    _get_repo().unset_config(key, scope="local")


def get_git_dir() -> Path:
    """Get the .git directory path."""
    return _get_repo().git_dir


def is_git_repo() -> bool:
    """Check if the current directory is inside a git repository."""
    return _get_repo().is_valid


def get_relative_path(path: Path) -> Path:
    """Get path relative to git root."""
    return _get_repo().relpath(path)
