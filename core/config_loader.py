"""Shared helpers for locating and loading configuration mappings."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

import json
import tomllib

try:  # Optional dependency for YAML support
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised when PyYAML absent
    yaml = None


ConfigLoader = Callable[[Any], Mapping[str, Any]]


def _raise_yaml_missing() -> Mapping[str, Any]:
    raise RuntimeError(
        "PyYAML is required to load YAML configuration files. Install with `pip install PyYAML`."
    )


FILE_LOADERS: Dict[str, ConfigLoader] = {
    ".toml": lambda stream: tomllib.load(stream),
    ".json": lambda stream: json.load(stream),
    ".yaml": lambda stream: yaml.safe_load(stream) if yaml else _raise_yaml_missing(),
    ".yml": lambda stream: yaml.safe_load(stream) if yaml else _raise_yaml_missing(),
}
"""Mapping of file suffixes to loader callables."""


def register_loader(suffix: str, loader: ConfigLoader) -> None:
    """Register ``loader`` for files ending with ``suffix``."""

    normalized = suffix.lower()
    if not normalized.startswith("."):
        raise ValueError("Suffix must start with '.'")
    FILE_LOADERS[normalized] = loader


def load_config_file(path: Path) -> Mapping[str, Any]:
    """Load and decode a configuration mapping from ``path``."""

    suffix = path.suffix.lower()
    loader = FILE_LOADERS.get(suffix)
    if loader is None:
        supported = ", ".join(sorted(FILE_LOADERS)) or "<none>"
        raise ValueError(
            f"Unsupported configuration file extension: {suffix}. Supported: {supported}"
        )

    mode = "rb" if suffix == ".toml" else "r"
    kwargs: Dict[str, Any] = {}
    if mode == "r":
        kwargs["encoding"] = "utf-8"

    with path.open(mode, **kwargs) as handle:
        data = loader(handle)

    if not isinstance(data, Mapping):
        raise TypeError(f"Configuration file '{path}' must contain a mapping at the root")

    return data


def collect_config_files(directory: Path, *, suffixes: Iterable[str] | None = None) -> Dict[str, Path]:
    """Return a mapping of filename stems to configuration files within ``directory``."""

    allowed = {suffix.lower() for suffix in (suffixes or FILE_LOADERS.keys())}
    files: Dict[str, Path] = {}

    for path in directory.iterdir():
        if not path.is_file():
            continue

        suffix = path.suffix.lower()
        if suffix not in allowed:
            continue

        stem = path.stem
        if stem in files:
            other = files[stem]
            raise ValueError(
                f"Multiple configuration files found for '{stem}': '{other.name}' and '{path.name}'. "
                "Only one format per configuration entry is allowed."
            )

        files[stem] = path

    return files


def merge_mappings(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """Deep merge two mapping objects."""

    result: Dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            result[key] = merge_mappings(existing, value)
        else:
            result[key] = value
    return result


def normalize_string_list(value: Any, *, field_name: str | None = None) -> List[str]:
    """Coerce ``value`` into a list of trimmed strings."""

    if value is None:
        return []

    if isinstance(value, (str, bytes)):
        text = str(value).strip()
        return [text] if text else []

    if isinstance(value, Sequence):
        items: List[str] = []
        for item in value:
            if not isinstance(item, (str, bytes)):
                label = f"{field_name} " if field_name else ""
                raise TypeError(f"{label}entries must be strings")
            text = str(item).strip()
            if text:
                items.append(text)
        return items

    label = f"{field_name} " if field_name else ""
    raise TypeError(f"{label}must be a string or sequence of strings")


def resolve_config_paths(root: Path, directories: Iterable[Path]) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Resolve ``directories`` relative to ``root`` and partition existing/missing paths."""

    resolved: List[Path] = []
    missing: List[Path] = []
    seen: set[Path] = set()

    for raw in directories:
        path = raw if raw.is_absolute() else (root / raw).resolve()
        if path in seen:
            if path in resolved:
                resolved = [candidate for candidate in resolved if candidate != path]
            else:
                missing = [candidate for candidate in missing if candidate != path]
        seen.add(path)

        if path.exists():
            resolved.append(path)
        else:
            missing.append(path)

    return tuple(resolved), tuple(missing)


__all__ = [
    "ConfigLoader",
    "FILE_LOADERS",
    "collect_config_files",
    "load_config_file",
    "merge_mappings",
    "normalize_string_list",
    "register_loader",
    "resolve_config_paths",
]
