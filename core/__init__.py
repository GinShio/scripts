"""Shared core utilities for build orchestration and templating."""

from .archive import ArchiveArtifact, ArchiveConsole, ArchiveManager
from .template import TemplateError, TemplateResolver, build_dependency_map, extract_placeholders, topological_order
from .command_runner import (
    CommandError,
    CommandResult,
    CommandRunner,
    RecordingCommandRunner,
    SubprocessCommandRunner,
)
from .config_loader import (
    ConfigLoader,
    FILE_LOADERS,
    collect_config_files,
    load_config_file,
    merge_mappings,
    normalize_string_list,
    register_loader,
    resolve_config_paths,
)

__all__ = [
    "TemplateError",
    "TemplateResolver",
    "build_dependency_map",
    "extract_placeholders",
    "topological_order",
    "ArchiveConsole",
    "ArchiveManager",
    "ArchiveArtifact",
    "CommandError",
    "CommandResult",
    "CommandRunner",
    "RecordingCommandRunner",
    "SubprocessCommandRunner",
    "ConfigLoader",
    "FILE_LOADERS",
    "collect_config_files",
    "load_config_file",
    "merge_mappings",
    "normalize_string_list",
    "register_loader",
    "resolve_config_paths",
]
