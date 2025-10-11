"""Configuration loading and validation logic."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Sequence
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


def _normalize_string_list(value: Any, *, field_name: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        text = str(value).strip()
        return [text] if text else []
    if isinstance(value, Sequence):
        result: List[str] = []
        for item in value:
            if isinstance(item, (str, bytes)):
                text = str(item).strip()
                if text:
                    result.append(text)
            else:
                raise TypeError(f"{field_name} entries must be strings")
        return result
    raise TypeError(f"{field_name} must be a string or sequence of strings")


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
    environment: Dict[str, Any] = field(default_factory=dict)

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
        environment_section = data.get("environment") if isinstance(data, Mapping) else None
        environment: Dict[str, Any] = {}
        if isinstance(environment_section, Mapping):
            for key, value in environment_section.items():
                environment[str(key)] = value
        return cls(
            url=str(url),
            main_branch=str(main_branch),
            component_branch=str(component_branch) if component_branch else None,
            auto_stash=auto_stash,
            update_script=str(update_script) if update_script else None,
            clone_script=str(clone_script) if clone_script else None,
            environment=environment,
        )


@dataclass(slots=True)
class ProjectDependency:
    name: str
    presets: List[str] = field(default_factory=list)

    @classmethod
    def from_value(cls, value: Any) -> "ProjectDependency":
        if isinstance(value, str):
            name = value.strip()
            if not name:
                raise ValueError("Dependency entries cannot be empty strings")
            return cls(name=name)
        if isinstance(value, Mapping):
            raw_name = value.get("name") or value.get("project")
            if not raw_name or not str(raw_name).strip():
                raise ValueError("Dependency entries must include a non-empty 'name'")
            presets = cls._normalize_presets(value.get("presets"))
            return cls(name=str(raw_name), presets=presets)
        raise TypeError("Dependencies must be specified as strings or mappings")

    @staticmethod
    def _normalize_presets(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            presets: List[str] = []
            for item in value:
                if not isinstance(item, str):
                    raise TypeError("Dependency presets must be strings")
                item = item.strip()
                if item:
                    presets.append(item)
            return presets
        raise TypeError("Dependency presets must be a string or a sequence of strings")


@dataclass(slots=True)
class ProjectDefinition:
    name: str
    source_dir: str
    build_dir: str | None
    install_dir: str | None
    build_system: str | None
    generator: str | None
    component_dir: str | None
    build_at_root: bool
    source_at_root: bool
    git: GitSettings
    presets: Dict[str, Mapping[str, Any]] = field(default_factory=dict)
    dependencies: List[ProjectDependency] = field(default_factory=list)
    extra_config_args: List[str] = field(default_factory=list)
    extra_build_args: List[str] = field(default_factory=list)
    environment: Dict[str, Any] = field(default_factory=dict)
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
        if not name or not source_dir:
            raise ValueError("project.name and project.source_dir are required")
        if build_dir and not build_system:
            raise ValueError("project.build_system is required when project.build_dir is specified")
        generator = project_section.get("generator")
        component_dir = project_section.get("component_dir")
        build_at_root = bool(project_section.get("build_at_root", True))
        raw_source_at_root = project_section.get("source_at_root")
        if raw_source_at_root is None:
            if component_dir:
                source_at_root = bool(build_at_root)
            else:
                source_at_root = True
        elif isinstance(raw_source_at_root, bool):
            source_at_root = raw_source_at_root
        else:
            raise TypeError("project.source_at_root must be a boolean if specified")
        environment_section = project_section.get("environment")
        project_environment: Dict[str, Any] = {}
        if isinstance(environment_section, Mapping):
            for key, value in environment_section.items():
                project_environment[str(key)] = value

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

        extra_config_args = _normalize_string_list(
            project_section.get("extra_config_args"),
            field_name="project.extra_config_args",
        )
        extra_build_args = _normalize_string_list(
            project_section.get("extra_build_args"),
            field_name="project.extra_build_args",
        )
        dependencies_section = data.get("dependencies", [])
        dependencies: List[ProjectDependency] = []
        if dependencies_section:
            if isinstance(dependencies_section, Sequence) and not isinstance(dependencies_section, (str, bytes)):
                for entry in dependencies_section:
                    dependencies.append(ProjectDependency.from_value(entry))
            else:
                raise TypeError("[dependencies] must be an array of tables or strings")

        return cls(
            name=str(name),
            source_dir=str(source_dir),
            build_dir=str(build_dir) if build_dir else None,
            install_dir=str(install_dir) if install_dir else None,
            build_system=str(build_system).lower() if build_system else None,
            generator=str(generator) if generator else None,
            component_dir=str(component_dir) if component_dir else None,
            build_at_root=build_at_root,
            source_at_root=source_at_root,
            git=git,
            presets=presets,
            dependencies=dependencies,
            extra_config_args=extra_config_args,
            extra_build_args=extra_build_args,
            environment=project_environment,
            raw=data,
        )


@dataclass(slots=True)
class ResolvedDependency:
    project: "ProjectDefinition"
    presets: List[str] = field(default_factory=list)


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

    def resolve_dependency_chain(self, name: str) -> List[ResolvedDependency]:
        if name not in self.projects:
            available = ", ".join(sorted(self.projects)) or "<none>"
            raise KeyError(f"Project '{name}' not found. Available projects: {available}")

        resolved: Dict[str, ResolvedDependency] = {}
        visiting: List[str] = []
        visited: set[str] = set()
        order: List[str] = []

        def visit(project_name: str) -> None:
            if project_name in visiting:
                cycle = " -> ".join([*visiting, project_name])
                raise ValueError(f"Dependency cycle detected: {cycle}")
            if project_name in visited:
                return

            visiting.append(project_name)
            project = self.get_project(project_name)
            for dependency in project.dependencies:
                dep_name = dependency.name
                if dep_name not in self.projects:
                    raise KeyError(
                        f"Dependency '{dep_name}' referenced by project '{project_name}' was not found"
                    )
                dep_project = self.projects[dep_name]
                entry = resolved.get(dep_name)
                if entry is None:
                    entry = ResolvedDependency(
                        project=dep_project,
                        presets=list(dependency.presets),
                    )
                    resolved[dep_name] = entry
                else:
                    if dependency.presets:
                        entry.presets = list(dependency.presets)
                visit(dep_name)
            visiting.pop()
            visited.add(project_name)
            order.append(project_name)

        visit(name)

        if order and order[-1] == name:
            order.pop()

        chain: List[ResolvedDependency] = []
        for dep_name in order:
            entry = resolved.get(dep_name)
            if entry is not None:
                chain.append(entry)
        return chain
