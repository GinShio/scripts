"""Configuration loading and validation logic."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping
import json
import tomllib

try:  # Optional dependency for YAML support
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised when PyYAML absent
    yaml = None


ConfigLoader = Callable[[Any], Mapping[str, Any]]


def _raise_yaml_missing() -> None:
    raise RuntimeError("PyYAML is required to load YAML configuration files. Install with `pip install PyYAML`.")


_FILE_LOADERS: Dict[str, ConfigLoader] = {
    ".toml": lambda stream: tomllib.load(stream),
    ".json": lambda stream: json.load(stream),
    ".yaml": lambda stream: yaml.safe_load(stream) if yaml else _raise_yaml_missing(),
    ".yml": lambda stream: yaml.safe_load(stream) if yaml else _raise_yaml_missing(),
}


def _load_config_file(path: Path) -> Mapping[str, Any]:
    suffix = path.suffix.lower()
    loader = _FILE_LOADERS.get(suffix)
    if loader is None:
        raise ValueError(f"Unsupported configuration file extension: {suffix}")
    mode = "rb" if suffix == ".toml" else "r"
    kwargs: Dict[str, Any] = {}
    if mode == "r":
        kwargs["encoding"] = "utf-8"
    with path.open(mode, **kwargs) as handle:
        data = loader(handle)
    if not isinstance(data, Mapping):
        raise TypeError(f"Configuration file '{path}' must contain a mapping at the root")
    return data


def _collect_config_files(directory: Path) -> Dict[str, Path]:
    files: Dict[str, Path] = {}
    for path in directory.iterdir():
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in _FILE_LOADERS:
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


@dataclass(slots=True)
class GlobalConfig:
    default_build_type: str = "Debug"
    log_level: str = "info"
    log_file: str | None = None
    default_operation: str = "auto"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "GlobalConfig":
        global_section = data.get("global", {}) if isinstance(data, Mapping) else {}
        return cls(
            default_build_type=str(global_section.get("default_build_type", "Debug")),
            log_level=str(global_section.get("log_level", "info")),
            log_file=str(global_section.get("log_file")) if global_section.get("log_file") else None,
            default_operation=str(global_section.get("default_operation", "auto")),
        )


@dataclass(slots=True)
class GitSettings:
    url: str
    main_branch: str
    component_branch: str | None = None
    auto_stash: bool = False
    update_script: str | None = None
    clone_script: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "GitSettings":
        url = data.get("url")
        main_branch = data.get("main_branch")
        if not url:
            raise ValueError("git.url is required in project configuration")
        if not main_branch:
            raise ValueError("git.main_branch is required in project configuration")
        component_branch = data.get("component_branch")
        auto_stash = bool(data.get("auto_stash", False))
        update_script = data.get("update_script")
        clone_script = data.get("clone_script")
        return cls(
            url=str(url),
            main_branch=str(main_branch),
            component_branch=str(component_branch) if component_branch else None,
            auto_stash=auto_stash,
            update_script=str(update_script) if update_script else None,
            clone_script=str(clone_script) if clone_script else None,
        )


@dataclass(slots=True)
class ProjectDefinition:
    name: str
    source_dir: str
    build_dir: str
    install_dir: str | None
    build_system: str
    generator: str | None
    component_dir: str | None
    build_at_root: bool
    git: GitSettings
    presets: Dict[str, Mapping[str, Any]] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ProjectDefinition":
        project_section = data.get("project")
        if not isinstance(project_section, Mapping):
            raise ValueError("[project] section is required in project configuration")
        name = project_section.get("name")
        source_dir = project_section.get("source_dir")
        build_dir = project_section.get("build_dir")
        install_dir = project_section.get("install_dir")
        build_system = project_section.get("build_system")
        if not name or not source_dir or not build_dir or not build_system:
            raise ValueError("project.name, project.source_dir, project.build_dir, and project.build_system are required")
        generator = project_section.get("generator")
        component_dir = project_section.get("component_dir")
        build_at_root = bool(project_section.get("build_at_root", True))

        git_section = data.get("git")
        if not isinstance(git_section, Mapping):
            raise ValueError("[git] section is required in project configuration")
        git = GitSettings.from_mapping(git_section)

        presets_section = data.get("presets", {})
        presets: Dict[str, Mapping[str, Any]] = {}
        if isinstance(presets_section, Mapping):
            for key, value in presets_section.items():
                if isinstance(value, Mapping):
                    presets[str(key)] = value

        return cls(
            name=str(name),
            source_dir=str(source_dir),
            build_dir=str(build_dir),
            install_dir=str(install_dir) if install_dir else None,
            build_system=str(build_system).lower(),
            generator=str(generator) if generator else None,
            component_dir=str(component_dir) if component_dir else None,
            build_at_root=build_at_root,
            git=git,
            presets=presets,
            raw=data,
        )


@dataclass(slots=True)
class ConfigurationStore:
    root: Path
    global_config: GlobalConfig
    shared_configs: Dict[str, Mapping[str, Any]]
    projects: Dict[str, ProjectDefinition]

    @classmethod
    def from_directory(cls, root: Path) -> "ConfigurationStore":
        config_dir = root / "config"
        if not config_dir.exists():
            raise FileNotFoundError(f"Configuration directory not found: {config_dir}")

        top_level_files = _collect_config_files(config_dir)

        global_data: Mapping[str, Any] = {}
        global_path = top_level_files.pop("config", None)
        if global_path is not None:
            global_data = _load_config_file(global_path)
        global_config = GlobalConfig.from_mapping(global_data)

        shared_configs: Dict[str, Mapping[str, Any]] = {}
        for stem, path in sorted(top_level_files.items()):
            shared_configs[stem] = _load_config_file(path)

        projects_dir = config_dir / "projects"
        if not projects_dir.exists():
            raise FileNotFoundError("Directory config/projects does not exist")

        projects: Dict[str, ProjectDefinition] = {}
        project_files = _collect_config_files(projects_dir)
        for _, path in sorted(project_files.items()):
            data = _load_config_file(path)
            project = ProjectDefinition.from_mapping(data)
            projects[project.name] = project

        return cls(
            root=root,
            global_config=global_config,
            shared_configs=shared_configs,
            projects=projects,
        )

    def list_projects(self) -> Iterable[str]:
        return self.projects.keys()

    def get_project(self, name: str) -> ProjectDefinition:
        if name not in self.projects:
            available = ", ".join(sorted(self.projects)) or "<none>"
            raise KeyError(f"Project '{name}' not found. Available projects: {available}")
        return self.projects[name]
