"""Configuration loading and validation logic for the builder CLI."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from core.config_loader import (
    ConfigLoader as SharedConfigLoader,
    collect_config_files,
    load_config_file,
    merge_mappings,
    normalize_string_list,
    resolve_config_paths,
)

from .toolchains import ToolchainRegistry

ConfigLoader = SharedConfigLoader


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
    default_toolchain: str | None
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
        default_toolchain = project_section.get("toolchain")
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

        extra_config_args = normalize_string_list(
            project_section.get("extra_config_args"),
            field_name="project.extra_config_args",
        )
        extra_build_args = normalize_string_list(
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
            default_toolchain=str(default_toolchain).strip() if isinstance(default_toolchain, str) and default_toolchain.strip() else None,
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

    def validate_structure(self) -> list[str]:
        """Return a list of structural validation errors for the project."""

        errors: list[str] = []

        if not self.source_dir:
            errors.append("project.source_dir must be defined")

        allowed_systems = {"cmake", "meson", "bazel", "cargo", "make"}
        if self.build_system is not None and self.build_system not in allowed_systems:
            allowed = ", ".join(sorted(allowed_systems))
            errors.append(f"project.build_system '{self.build_system}' is not supported (allowed: {allowed})")

        if self.build_dir:
            build_dir_path = Path(self.build_dir)
            if build_dir_path.is_absolute():
                errors.append("project.build_dir must be a relative path")

        required_build_dir = {"cmake", "meson", "cargo", "make"}
        if self.build_system in required_build_dir and not self.build_dir:
            errors.append(f"project.build_dir is required for build_system '{self.build_system}'")

        if self.component_dir:
            component_path = Path(self.component_dir)
            if component_path.is_absolute():
                errors.append("project.component_dir must be a relative path")

        return errors


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
    toolchains: ToolchainRegistry
    config_dirs: tuple[Path, ...] = field(default_factory=tuple)

    @classmethod
    def from_directory(cls, root: Path) -> "ConfigurationStore":
        return cls.from_directories(root, [root / "config"])

    @classmethod
    def from_directories(cls, root: Path, directories: Iterable[Path]) -> "ConfigurationStore":
        resolved_dirs, missing_dirs = resolve_config_paths(root, directories)
        if missing_dirs and not resolved_dirs:
            missing_display = ", ".join(str(path) for path in missing_dirs)
            raise FileNotFoundError(f"No configuration directories found. Missing: {missing_display}")
        if not resolved_dirs:
            raise FileNotFoundError("No configuration directories were provided")

        global_data: Mapping[str, Any] = {}
        shared_configs: Dict[str, Mapping[str, Any]] = {}
        toolchain_registry = ToolchainRegistry.with_builtins()
        projects: Dict[str, ProjectDefinition] = {}
        have_projects_dir = False

        for config_dir in resolved_dirs:
            top_level_files = collect_config_files(config_dir)
            global_path = top_level_files.pop("config", None)
            if global_path is not None:
                data = load_config_file(global_path)
                global_data = merge_mappings(global_data, data)

            toolchains_path = top_level_files.pop("toolchains", None)
            if toolchains_path is not None:
                toolchain_data = load_config_file(toolchains_path)
                toolchain_registry.merge_from_mapping(toolchain_data)

            for stem, path in sorted(top_level_files.items()):
                shared_configs[stem] = load_config_file(path)

            projects_dir = config_dir / "projects"
            if not projects_dir.exists():
                continue

            have_projects_dir = True
            project_files = collect_config_files(projects_dir)
            for _, path in sorted(project_files.items()):
                data = load_config_file(path)
                project = ProjectDefinition.from_mapping(data)
                projects[project.name] = project

        if not have_projects_dir or not projects:
            raise FileNotFoundError("No project configurations found in the provided directories")

        global_config = GlobalConfig.from_mapping(global_data)

        return cls(
            root=root,
            config_dirs=resolved_dirs,
            global_config=global_config,
            shared_configs=shared_configs,
            projects=projects,
            toolchains=toolchain_registry,
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

        requested_presets: Dict[str, List[str]] = {}
        visiting: List[str] = []
        visited: set[str] = set()
        order: List[str] = []

        def record_presets(target: str, presets: Iterable[str]) -> None:
            if not presets:
                requested_presets.setdefault(target, [])
                return
            bucket = requested_presets.setdefault(target, [])
            for preset in presets:
                if preset not in bucket:
                    bucket.append(preset)

        def visit(project_name: str) -> None:
            if project_name in visiting:
                cycle = " -> ".join([*visiting, project_name])
                raise ValueError(f"Circular dependency detected: {cycle}")
            if project_name in visited:
                return

            visiting.append(project_name)
            project_def = self.projects.get(project_name)
            if project_def is None:
                available_projects = ", ".join(sorted(self.projects)) or "<none>"
                raise KeyError(f"Dependency '{project_name}' not found. Available projects: {available_projects}")

            for dependency in project_def.dependencies:
                record_presets(dependency.name, dependency.presets)
                visit(dependency.name)

            visiting.pop()
            visited.add(project_name)
            order.append(project_name)

        visit(name)

        resolved_chain: List[ResolvedDependency] = []
        for project_name in order:
            if project_name == name:
                continue
            project_def = self.projects[project_name]
            resolved_chain.append(
                ResolvedDependency(
                    project=project_def,
                    presets=list(requested_presets.get(project_name, [])),
                )
            )

        return resolved_chain


__all__ = [
    "ConfigurationStore",
    "ConfigLoader",
    "GlobalConfig",
    "GitSettings",
    "ProjectDefinition",
    "ProjectDependency",
    "ResolvedDependency",
]
