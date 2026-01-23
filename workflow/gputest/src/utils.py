"""
Utility functions for gputest.
"""

import os
import platform
from pathlib import Path
from typing import Any, Dict, Optional

from core.config_loader import load_config_file
from core.template import TemplateResolver


def _get_default_context() -> Dict[str, Any]:
    """Get default context with env and system variables."""
    return {
        "env": os.environ.copy(),
        "system": {
            "os": platform.system().lower(),
            "architecture": platform.machine(),
        },
    }


def substitute(text: str, variables: Dict[str, Any]) -> str:
    """Substitute {{var}} in text with values from variables using core.template."""
    context = _get_default_context()
    deep_merge(context, variables)
    resolver = TemplateResolver(context)
    return str(resolver.resolve(text))


def resolve_env(
    env_config: Dict[str, str], variables: Dict[str, Any]
) -> Dict[str, str]:
    """Resolve environment variables with substitution."""
    context = _get_default_context()
    deep_merge(context, variables)
    resolver = TemplateResolver(context)
    resolved = {}
    for k, v in env_config.items():
        # Resolve value using template engine
        resolved[k] = str(resolver.resolve(v))
    return resolved


def deep_merge(target: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge source dict into target dict."""
    for key, value in source.items():
        if isinstance(value, dict) and key in target and isinstance(target[key], dict):
            deep_merge(target[key], value)
        else:
            target[key] = value
    return target


def load_merged_config(path: Path, console: Any = None) -> Dict[str, Any]:
    """
    Load configuration from a file or a directory of .toml files.
    If path is a directory, merges all .toml files alphabetically.
    """
    config = {}
    if path.is_dir():
        # Load all .toml files in directory
        config_files = sorted(path.glob("*.toml"))
        if not config_files:
            msg = f"No .toml configuration files found in: {path}"
            if console:
                console.error(msg)
            raise FileNotFoundError(msg)

        for cf in config_files:
            if console:
                console.debug(f"Loading config file: {cf}")
            file_config = load_config_file(cf)
            deep_merge(config, file_config)
    else:
        config = load_config_file(path)
    return config
