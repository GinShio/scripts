"""Core build planning and execution logic."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence
import json
import shutil

from core.command_runner import CommandRunner, CommandResult
from .config_loader import ConfigurationStore, ProjectDefinition
from .environment import ContextBuilder
from .presets import PresetRepository
from .toolchains import ToolchainDefinition
from core.template import TemplateResolver, build_dependency_map, topological_order


class BuildMode(str, Enum):
    AUTO = "auto"
    CONFIG_ONLY = "config-only"
    BUILD_ONLY = "build-only"
    RECONFIG = "reconfig"


@dataclass(slots=True)
class BuildOptions:
    project_name: str
    presets: List[str]
    branch: str | None = None
    build_type: str | None = None
    generator: str | None = None
    target: str | None = None
    install: bool = False
    dry_run: bool = False
    show_vars: bool = False
    no_switch_branch: bool = False
    verbose: bool = False
    extra_config_args: List[str] = field(default_factory=list)
    extra_build_args: List[str] = field(default_factory=list)
    toolchain: str | None = None
    install_dir: str | None = None
    operation: BuildMode = BuildMode.AUTO
    definitions: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BuildStep:
    description: str
    command: Sequence[str]
    cwd: Path
    env: Dict[str, str]


@dataclass(slots=True)
class BuildPlan:
    project: ProjectDefinition
    build_dir: Path | None
    install_dir: Path | None
    source_dir: Path
    configure_source_dir: Path
    component_dir: Path | None
    steps: List[BuildStep]
    context: Dict[str, Any]
    presets: List[str]
    environment: Dict[str, str]
    definitions: Dict[str, Any]
    extra_config_args: List[str]
    extra_build_args: List[str]
    git_clone_script: str | None
    git_update_script: str | None
    git_environment: Dict[str, str]
    branch: str
    branch_slug: str


@dataclass(slots=True)
class _ResolvedPaths:
    source_dir: Path
    build_dir: Path | None
    install_dir: Path | None
    component_dir: Path | None
    target_source_dir: Path



class BuildEngine:
    def __init__(
        self,
        *,
        store: ConfigurationStore,
        command_runner: CommandRunner,
        workspace: Path,
    ) -> None:
        self._store = store
        self._command_runner = command_runner
        self._workspace = workspace

    @staticmethod
    def _select_branch(project: ProjectDefinition, options: BuildOptions) -> str:
        if options.branch:
            return options.branch
        if project.git.component_branch and project.component_dir:
            return project.git.component_branch
        return project.git.main_branch

    def _apply_toolchain_environment(
        self,
        *,
        project: ProjectDefinition,
        environment: Dict[str, str],
        definitions: Dict[str, Any],
        options: BuildOptions,
        toolchain: str,
        cc: str | None,
        cxx: str | None,
        linker: str | None,
        launcher: str | None,
        definition: ToolchainDefinition | None,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        self._ensure_toolchain_compatibility(
            build_system=project.build_system,
            toolchain=toolchain,
            definition=definition,
        )

        explicit = options.toolchain is not None
        resolved_launcher = launcher
        if definition is not None:
            definition.apply(
                build_system=project.build_system,
                environment=environment,
                definitions=definitions,
                explicit=explicit,
            )
            if definition.resolved_cc:
                cc = definition.resolved_cc
            if definition.resolved_cxx:
                cxx = definition.resolved_cxx
            if definition.resolved_linker:
                linker = definition.resolved_linker
            override_launcher = definition.resolve_launcher(project.build_system)
            if override_launcher is not None:
                resolved_launcher = override_launcher

        def apply_value(key: str, value: str | None) -> None:
            if not value:
                return
            if explicit:
                environment[key] = value
            else:
                existing = environment.get(key)
                if existing != value:
                    environment[key] = value

        apply_value("CC", cc)
        apply_value("CXX", cxx)

        if linker:
            apply_value("CC_LD", linker)
            apply_value("CXX_LD", linker)

        if resolved_launcher:
            def prepend(value: str | None) -> str | None:
                if not value:
                    return value
                prefix = f"{resolved_launcher} "
                return value if value.startswith(prefix) else f"{resolved_launcher} {value}"

            current_cc = environment.get("CC")
            current_cxx = environment.get("CXX")
            if current_cc:
                environment["CC"] = prepend(current_cc)
            if current_cxx:
                environment["CXX"] = prepend(current_cxx)

        return cc, cxx, linker, resolved_launcher

    @staticmethod
    def _resolve_generator(options: BuildOptions, project: ProjectDefinition) -> str | None:
        if options.generator is not None:
            return options.generator
        generator = project.generator
        if generator is not None:
            options.generator = generator
        return generator

    @staticmethod
    def _resolve_paths(
        *,
        project: ProjectDefinition,
        resolver: TemplateResolver,
        source_dir_str: str,
        build_dir_str: str | None,
        install_dir_str: str | None,
        component_dir_str: str | None,
        build_enabled: bool,
    ) -> _ResolvedPaths:
        source_dir = Path(resolver.resolve(source_dir_str)).expanduser()
        build_dir = Path(resolver.resolve(build_dir_str)) if build_dir_str else None
        install_dir = Path(resolver.resolve(install_dir_str)) if install_dir_str else None
        component_dir = Path(resolver.resolve(component_dir_str)) if component_dir_str else None

        target_source_dir = source_dir
        if component_dir and not project.source_at_root:
            target_source_dir = (source_dir / component_dir).resolve()

        build_dir_path: Path | None = None
        if build_enabled and build_dir is not None:
            if not project.build_at_root and component_dir:
                build_root = (source_dir / component_dir).resolve()
            else:
                build_root = source_dir
            build_dir_path = (build_root / build_dir).resolve()

        if install_dir:
            if install_dir.is_absolute():
                install_dir = install_dir
            else:
                install_dir = (source_dir / install_dir).resolve()

        return _ResolvedPaths(
            source_dir=source_dir,
            build_dir=build_dir_path,
            install_dir=install_dir,
            component_dir=component_dir,
            target_source_dir=target_source_dir,
        )

    @staticmethod
    def _extend_unique(target: List[str], values: Iterable[str]) -> None:
        existing = set(target)
        for value in values:
            if value not in existing:
                target.append(value)
                existing.add(value)

    def _resolve_environment_mapping(
        self,
        *,
        mapping: Mapping[str, Any],
        resolver: TemplateResolver,
        base_env: Mapping[str, str],
        namespace: str,
        namespace_base: Mapping[str, str] | None,
        prefixes: Sequence[str],
        additional_namespaces: Mapping[str, Mapping[str, str]] | None = None,
    ) -> Dict[str, str]:
        if not mapping:
            return {}

        normalized: Dict[str, Any] = {str(key): value for key, value in mapping.items()}
        dependency_map = build_dependency_map(
            normalized,
            prefixes=prefixes,
            pre_resolved=base_env.keys(),
        )
        order = topological_order(dependency_map)

        resolved: Dict[str, str] = {}
        namespace_base = namespace_base or {}
        additional_namespaces = additional_namespaces or {}

        context_template: Dict[str, Any] = dict(resolver.context)
        env_context: Dict[str, str] = dict(context_template.get("env", {}))
        context_template["env"] = env_context

        namespace_context: Dict[str, Any] = dict(context_template.get(namespace, {}))
        namespace_environment: Dict[str, str] = dict(namespace_base)
        namespace_context["environment"] = namespace_environment
        context_template[namespace] = namespace_context

        for other_namespace, values in additional_namespaces.items():
            other_context = dict(context_template.get(other_namespace, {}))
            other_env = dict(values)
            other_context["environment"] = other_env
            context_template[other_namespace] = other_context

        reusable_resolver = TemplateResolver(context_template)

        for key in order:
            env_context.clear()
            env_context.update(base_env)
            env_context.update(resolved)

            namespace_environment.clear()
            namespace_environment.update(namespace_base)
            namespace_environment.update(resolved)

            reusable_resolver.clear_cache()
            resolved_value = reusable_resolver.resolve(normalized[key])
            resolved[key] = str(resolved_value)

        return resolved

    def _apply_environment_to_context(
        self,
        *,
        context: Mapping[str, Any],
        environment: Mapping[str, str],
        definitions: Mapping[str, Any],
        preset_environment: Mapping[str, str] | None = None,
        preset_definitions: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        updated_context: Dict[str, Any] = dict(context)
        env_context = dict(updated_context.get("env", {}))
        env_context.update(environment)
        updated_context["env"] = env_context

        project_mapping = dict(updated_context.get("project", {}))
        project_mapping["environment"] = dict(environment)
        project_mapping["definitions"] = dict(definitions)
        updated_context["project"] = project_mapping

        if preset_environment or preset_definitions:
            preset_mapping = dict(updated_context.get("preset", {}))
            if preset_environment:
                preset_mapping["environment"] = dict(preset_environment)
            if preset_definitions:
                preset_mapping["definitions"] = dict(preset_definitions)
            updated_context["preset"] = preset_mapping

        return updated_context

    def plan(self, options: BuildOptions) -> BuildPlan:
        project = self._store.get_project(options.project_name)
        builder_path = self._workspace
        context_builder = ContextBuilder(builder_path)

        branch = self._select_branch(project, options)
        build_type = self._determine_build_type(options=options)
        generator = self._resolve_generator(options, project)
        system_ctx = context_builder.system()

        selected_toolchain = options.toolchain or project.default_toolchain
        if not selected_toolchain:
            raise ValueError(
                f"No toolchain specified for project '{project.name}'. Provide one via project.toolchain or --toolchain."
            )

        toolchain_key = selected_toolchain.strip().lower()
        if not toolchain_key:
            raise ValueError("Toolchain name cannot be empty")

        definition = self._store.toolchains.get(toolchain_key)
        if definition is None:
            available = ", ".join(sorted(self._store.toolchains.available())) or "<none>"
            raise ValueError(f"Unknown toolchain '{selected_toolchain}'. Available toolchains: {available}")

        preview_env: Dict[str, str] = {}
        preview_defs: Dict[str, Any] = {}
        definition.apply(
            build_system=project.build_system,
            environment=preview_env,
            definitions=preview_defs,
            explicit=True,
        )
        cc = preview_env.get("CC") or definition.resolved_cc
        cxx = preview_env.get("CXX") or definition.resolved_cxx
        linker = self._determine_linker(toolchain=toolchain_key, os_name=system_ctx.os_name, definition=definition)
        launcher = definition.resolve_launcher(project.build_system)

        user_ctx = context_builder.user(
            branch=branch,
            build_type=build_type,
            generator=generator,
            operation=options.operation.value,
            toolchain=toolchain_key,
            linker=linker,
            cc=cc,
            cxx=cxx,
            launcher=launcher,
        )

        source_dir_str = project.source_dir
        build_dir_str = project.build_dir
        install_dir_str = options.install_dir or project.install_dir
        component_dir_str = project.component_dir
        build_enabled = build_dir_str is not None and project.build_system is not None

        project_ctx = context_builder.project(
            name=project.name,
            source_dir=Path(source_dir_str),
            build_dir=Path(build_dir_str) if build_dir_str else None,
            install_dir=Path(install_dir_str) if install_dir_str else None,
            component_dir=Path(component_dir_str) if component_dir_str else None,
        )

        combined_context = context_builder.combined_context(user=user_ctx, project=project_ctx, system=system_ctx)
        resolver = TemplateResolver(combined_context)

        paths = self._resolve_paths(
            project=project,
            resolver=resolver,
            source_dir_str=source_dir_str,
            build_dir_str=build_dir_str,
            install_dir_str=install_dir_str,
            component_dir_str=component_dir_str,
            build_enabled=build_enabled,
        )

        updated_project_ctx = context_builder.project(
            name=project.name,
            source_dir=paths.source_dir,
            build_dir=paths.build_dir,
            install_dir=paths.install_dir,
            component_dir=paths.component_dir,
        )
        combined_context = context_builder.combined_context(user=user_ctx, project=updated_project_ctx, system=system_ctx)
        resolver = TemplateResolver(combined_context)

        base_env_context = {str(key): str(value) for key, value in resolver.context.get("env", {}).items()}
        project_environment = self._resolve_environment_mapping(
            mapping=project.environment,
            resolver=resolver,
            base_env=base_env_context,
            namespace="project",
            namespace_base={},
            prefixes=("env.", "project.environment."),
        )

        if project_environment:
            updated_context: Dict[str, Any] = dict(combined_context)
            env_context = dict(updated_context.get("env", {}))
            env_context.update(project_environment)
            updated_context["env"] = env_context
            project_mapping = dict(updated_context.get("project", {}))
            project_mapping["environment"] = dict(project_environment)
            updated_context["project"] = project_mapping
            combined_context = updated_context
            resolver = TemplateResolver(combined_context)
        else:
            project_environment = {}

        preset_repo = PresetRepository(
            project_presets=project.presets,
            shared_presets=[cfg.get("presets", {}) for cfg in self._store.shared_configs.values()],
        )
        presets_to_resolve = self._determine_presets(
            provided_presets=options.presets,
            build_type=build_type,
            generator=generator,
            preset_repo=preset_repo,
        )
        resolved_presets = preset_repo.resolve(presets_to_resolve, template_resolver=resolver)

        environment = dict(project_environment)
        environment.update(resolved_presets.environment)
        definitions = dict(resolved_presets.definitions)

        if options.definitions:
            for key, value in options.definitions.items():
                definitions[key] = value

        combined_context = self._apply_environment_to_context(
            context=combined_context,
            environment=environment,
            definitions=definitions,
            preset_environment=resolved_presets.environment,
            preset_definitions=resolved_presets.definitions,
        )
        resolver = TemplateResolver(combined_context)

        project_config_args_resolved = [str(resolver.resolve(arg)) for arg in project.extra_config_args]
        project_build_args_resolved = [str(resolver.resolve(arg)) for arg in project.extra_build_args]

        extra_config_args = list(resolved_presets.extra_config_args)
        extra_build_args = list(resolved_presets.extra_build_args)
        self._extend_unique(extra_config_args, project_config_args_resolved)
        self._extend_unique(extra_build_args, project_build_args_resolved)
        self._extend_unique(extra_config_args, options.extra_config_args)
        self._extend_unique(extra_build_args, options.extra_build_args)

        plan_steps: List[BuildStep] = []
        if build_enabled and paths.build_dir is not None:
            self._apply_cmake_build_type(
                project=project,
                definitions=definitions,
                build_type=build_type,
                build_type_override=options.build_type,
                generator=generator,
            )

            if project.build_system == "cargo":
                environment.setdefault("CARGO_TARGET_DIR", str(paths.build_dir))

            cc, cxx, linker, launcher = self._apply_toolchain_environment(
                project=project,
                environment=environment,
                definitions=definitions,
                options=options,
                toolchain=toolchain_key,
                cc=cc,
                cxx=cxx,
                linker=linker,
                launcher=launcher,
                definition=definition,
            )
            self._apply_color_diagnostics(
                project=project,
                environment=environment,
                definitions=definitions,
                toolchain=toolchain_key,
            )
            self._apply_cmake_toolchain(
                build_system=project.build_system,
                definitions=definitions,
                environment=environment,
                cc=cc,
                cxx=cxx,
                linker=linker,
                launcher=launcher,
            )

            plan_steps = self._create_build_steps(
                project=project,
                effective_source_dir=paths.target_source_dir,
                build_dir=paths.build_dir,
                install_dir=paths.install_dir,
                environment=environment,
                definitions=definitions,
                extra_config_args=extra_config_args,
                extra_build_args=extra_build_args,
                options=options,
            )

        user_ctx.toolchain = toolchain_key
        user_ctx.linker = linker
        user_ctx.cc = cc
        user_ctx.cxx = cxx
        user_ctx.launcher = launcher

        combined_context = dict(combined_context)
        combined_context["user"] = user_ctx.to_mapping()

        combined_context = self._apply_environment_to_context(
            context=combined_context,
            environment=environment,
            definitions=definitions,
            preset_environment=resolved_presets.environment,
            preset_definitions=resolved_presets.definitions,
        )
        resolver = TemplateResolver(combined_context)

        git_environment = self._resolve_environment_mapping(
            mapping=project.git.environment,
            resolver=resolver,
            base_env={str(key): str(value) for key, value in resolver.context.get("env", {}).items()},
            namespace="git",
            namespace_base={},
            prefixes=("env.", "project.environment.", "git.environment."),
            additional_namespaces={
                "project": dict(environment),
                "preset": dict(resolved_presets.environment),
            },
        )

        if git_environment:
            updated_context = dict(combined_context)
            env_context = dict(updated_context.get("env", {}))
            env_context.update(git_environment)
            updated_context["env"] = env_context
            git_mapping = dict(updated_context.get("git", {}))
            git_mapping["environment"] = dict(git_environment)
            updated_context["git"] = git_mapping
            combined_context = updated_context
            resolver = TemplateResolver(combined_context)

        clone_script = None
        if project.git.clone_script:
            clone_script = str(resolver.resolve(project.git.clone_script))
        update_script = None
        if project.git.update_script:
            update_script = str(resolver.resolve(project.git.update_script))

        return BuildPlan(
            project=project,
            build_dir=paths.build_dir,
            install_dir=paths.install_dir,
            source_dir=paths.source_dir,
            configure_source_dir=paths.target_source_dir,
            component_dir=paths.component_dir,
            steps=plan_steps,
            context=combined_context,
            presets=presets_to_resolve,
            environment=environment,
            definitions=definitions,
            extra_config_args=extra_config_args,
            extra_build_args=extra_build_args,
            git_clone_script=clone_script,
            git_update_script=update_script,
            git_environment=git_environment,
            branch=user_ctx.branch_raw,
            branch_slug=user_ctx.branch_slug,
        )

    def execute(self, plan: BuildPlan, *, dry_run: bool) -> List[CommandResult]:
        results: List[CommandResult] = []
        if not plan.steps:
            return results
        if dry_run:
            for step in plan.steps:
                self._command_runner.run(
                    step.command,
                    cwd=step.cwd,
                    env=step.env,
                    check=False,
                    note=step.description,
                    stream=False,
                )
            return results

        if plan.build_dir is not None:
            plan.build_dir.parent.mkdir(parents=True, exist_ok=True)

        for step in plan.steps:
            cwd = step.cwd
            cwd.mkdir(parents=True, exist_ok=True)
            result = self._command_runner.run(step.command, cwd=cwd, env=step.env, stream=True)
            results.append(result)
        return results

    def _determine_build_type(self, *, options: BuildOptions) -> str:
        if options.build_type:
            return options.build_type
        return self._store.global_config.default_build_type

    def _create_build_steps(
        self,
        *,
        project: ProjectDefinition,
        effective_source_dir: Path,
        build_dir: Path,
        install_dir: Path | None,
        environment: Dict[str, str],
        definitions: Dict[str, Any],
        extra_config_args: List[str],
        extra_build_args: List[str],
        options: BuildOptions,
    ) -> List[BuildStep]:
        steps: List[BuildStep] = []
        env = environment

        if project.build_system == "cmake":
            steps.extend(
                self._cmake_steps(
                    effective_source_dir=effective_source_dir,
                    build_dir=build_dir,
                    install_dir=install_dir,
                    environment=env,
                    definitions=definitions,
                    extra_config_args=extra_config_args,
                    extra_build_args=extra_build_args,
                    options=options,
                )
            )
        elif project.build_system == "meson":
            steps.extend(
                self._meson_steps(
                    effective_source_dir=effective_source_dir,
                    build_dir=build_dir,
                    install_dir=install_dir,
                    environment=env,
                    definitions=definitions,
                    extra_config_args=extra_config_args,
                    extra_build_args=extra_build_args,
                    options=options,
                )
            )
        elif project.build_system == "bazel":
            steps.extend(
                self._bazel_steps(
                    effective_source_dir=effective_source_dir,
                    environment=env,
                    definitions=definitions,
                    extra_build_args=extra_build_args,
                    options=options,
                )
            )
        elif project.build_system == "cargo":
            steps.extend(
                self._cargo_steps(
                    effective_source_dir=effective_source_dir,
                    build_dir=build_dir,
                    environment=env,
                    extra_config_args=extra_config_args,
                    extra_build_args=extra_build_args,
                    options=options,
                )
            )
        else:
            raise ValueError(f"Unsupported build system: {project.build_system}")
        return steps

    def _determine_presets(
        self,
        *,
        provided_presets: Iterable[str] | None,
        build_type: str,
        generator: str | None,
        preset_repo: PresetRepository,
    ) -> List[str]:
        resolved: List[str] = []
        if provided_presets:
            for preset in provided_presets:
                for part in preset.split(","):
                    stripped = part.strip()
                    if stripped and stripped not in resolved:
                        resolved.append(stripped)

        available = set(preset_repo.available())
        for preset in self._default_presets(build_type=build_type, generator=generator):
            if preset in available and preset not in resolved:
                resolved.append(preset)
        return resolved

    def _default_presets(self, *, build_type: str, generator: str | None) -> List[str]:
        if self._is_multi_config_generator(generator):
            return ["configs.debug", "configs.release"]
        preset_name = f"configs.{build_type.lower()}"
        return [preset_name]

    def _is_multi_config_generator(self, generator: str | None) -> bool:
        if not generator:
            return False
        normalized = generator.lower()
        multi_keywords = ["multi-config", "visual studio", "xcode"]
        return any(keyword in normalized for keyword in multi_keywords)

    def _cmake_steps(
        self,
        *,
        effective_source_dir: Path,
        build_dir: Path,
        install_dir: Path | None,
        environment: Dict[str, str],
        definitions: Dict[str, Any],
        extra_config_args: List[str],
        extra_build_args: List[str],
        options: BuildOptions,
    ) -> List[BuildStep]:
        steps: List[BuildStep] = []
        mode = options.operation
        build_dir_exists = build_dir.exists()
        configured = build_dir_exists and self._cmake_is_configured(build_dir)

        if install_dir is not None:
            definitions.setdefault("CMAKE_INSTALL_PREFIX", str(install_dir))

        if mode is BuildMode.BUILD_ONLY and (not build_dir_exists or not configured):
            raise ValueError("Build directory is not configured; run configuration first or use auto mode")

        if mode is BuildMode.RECONFIG and build_dir_exists:
            shutil.rmtree(build_dir)
            build_dir_exists = False
            configured = False

        should_configure = (
            mode in {BuildMode.CONFIG_ONLY, BuildMode.RECONFIG}
            or not configured
        )
        should_build = mode in {BuildMode.AUTO, BuildMode.BUILD_ONLY}

        is_multi_config = self._is_multi_config_generator(options.generator)

        if should_configure:
            args: List[str] = ["cmake"]
            if options.generator:
                args.extend(["-G", options.generator])
            for key, value in definitions.items():
                if key == "CMAKE_BUILD_TYPE" and is_multi_config:
                    continue
                typed_flag = self._cmake_definition_flag(name=key, value=value)
                args.extend(["-D", typed_flag])
            args.extend(["-B", str(build_dir), "-S", str(effective_source_dir)])
            args.extend(extra_config_args)
            steps.append(
                BuildStep(
                    description="Configure project",
                    command=args,
                    cwd=effective_source_dir,
                    env=environment,
                )
            )

        if should_build:
            cmd = ["cmake", "--build", str(build_dir)]
            if options.target:
                cmd.extend(["--target", options.target])
            if is_multi_config:
                build_type = self._determine_build_type(options=options)
                cmd.extend(["--config", build_type])
            cmd.extend(extra_build_args)
            steps.append(
                BuildStep(
                    description="Build project",
                    command=cmd,
                    cwd=effective_source_dir,
                    env=environment,
                )
            )

        if options.install:
            if install_dir is None:
                raise ValueError("Install directory is not defined for this project")
            install_cmd = self._install_command(
                build_system="cmake",
                build_dir=build_dir,
                is_multi_config=is_multi_config,
                options=options,
            )
            steps.append(
                BuildStep(
                    description="Install project",
                    command=install_cmd,
                    cwd=effective_source_dir,
                    env=environment,
                )
            )
        return steps

    def _meson_steps(
        self,
        *,
        effective_source_dir: Path,
        build_dir: Path,
        install_dir: Path | None,
        environment: Dict[str, str],
        definitions: Dict[str, Any],
        extra_config_args: List[str],
        extra_build_args: List[str],
        options: BuildOptions,
    ) -> List[BuildStep]:
        steps: List[BuildStep] = []
        mode = options.operation
        build_dir_exists = build_dir.exists()
        configured = build_dir_exists and self._meson_is_configured(build_dir)
        prefix_args: List[str] = []

        if install_dir is not None:
            prefix_args = ["--prefix", str(install_dir)]

        should_configure = (
            mode in {BuildMode.CONFIG_ONLY, BuildMode.RECONFIG}
            or not configured
        )
        should_build = mode in {BuildMode.AUTO, BuildMode.BUILD_ONLY}

        if mode is BuildMode.BUILD_ONLY and (not build_dir_exists or not configured):
            raise ValueError("Build directory is not configured; run configuration first or use auto mode")

        if mode is BuildMode.RECONFIG and build_dir_exists:
            shutil.rmtree(build_dir)
            build_dir_exists = False
            configured = False
            should_configure = True

        if should_configure:
            args = ["meson", "setup", str(build_dir), str(effective_source_dir)]
            if prefix_args:
                args.extend(prefix_args)
            for key, value in definitions.items():
                formatted = self._format_meson_value(value)
                args.append(f"-D{key}={formatted}")
            args.extend(extra_config_args)
            steps.append(
                BuildStep(
                    description="Configure project",
                    command=args,
                    cwd=effective_source_dir,
                    env=environment,
                )
            )

        if should_build:
            cmd = ["meson", "compile", "-C", str(build_dir)]
            if options.target:
                cmd.extend(["--target", options.target])
            cmd.extend(extra_build_args)
            steps.append(
                BuildStep(
                    description="Build project",
                    command=cmd,
                    cwd=effective_source_dir,
                    env=environment,
                )
            )

        if options.install:
            if install_dir is None:
                raise ValueError("Install directory is not defined for this project")
            install_cmd = self._install_command(
                build_system="meson",
                build_dir=build_dir,
                is_multi_config=False,
                options=options,
            )
            steps.append(
                BuildStep(
                    description="Install project",
                    command=install_cmd,
                    cwd=effective_source_dir,
                    env=environment,
                )
            )
        return steps

    def _cmake_is_configured(self, build_dir: Path) -> bool:
        cache_file = build_dir / "CMakeCache.txt"
        return cache_file.exists()

    def _meson_is_configured(self, build_dir: Path) -> bool:
        coredata = build_dir / "meson-private" / "coredata.dat"
        return coredata.exists()

    def _bazel_steps(
        self,
        *,
        effective_source_dir: Path,
        environment: Dict[str, str],
        definitions: Dict[str, Any],
        extra_build_args: List[str],
        options: BuildOptions,
    ) -> List[BuildStep]:
        steps: List[BuildStep] = []
        target = options.target or definitions.get("TARGET")
        if not target:
            raise ValueError("Bazel builds require a target (use --target or preset definitions.TARGET)")
        cmd = ["bazel", "build", str(target)]
        build_opts = definitions.get("BUILD_OPTS")
        if isinstance(build_opts, str):
            cmd.append(build_opts)
        cmd.extend(extra_build_args)
        steps.append(
            BuildStep(
                description="Build project",
                command=cmd,
                cwd=effective_source_dir,
                env=environment,
            )
        )
        return steps

    def _cargo_steps(
        self,
        *,
        effective_source_dir: Path,
        build_dir: Path,
        environment: Dict[str, str],
        extra_config_args: List[str],
        extra_build_args: List[str],
        options: BuildOptions,
    ) -> List[BuildStep]:
        steps: List[BuildStep] = []
        mode = options.operation

        if options.target:
            raise ValueError(
                "Cargo builds do not support --target; pass cargo-specific flags via --extra-build-args instead"
            )

        if mode is BuildMode.RECONFIG:
            steps.append(
                BuildStep(
                    description="Clean cargo workspace",
                    command=["cargo", "clean", "--target-dir", str(build_dir)],
                    cwd=effective_source_dir,
                    env=environment,
                )
            )

        should_configure = mode in {BuildMode.CONFIG_ONLY, BuildMode.RECONFIG}
        if should_configure:
            cmd = ["cargo", "fetch"]
            cmd.extend(extra_config_args)
            steps.append(
                BuildStep(
                    description="Fetch cargo dependencies",
                    command=cmd,
                    cwd=effective_source_dir,
                    env=environment,
                )
            )

        should_build = mode in {BuildMode.AUTO, BuildMode.BUILD_ONLY}
        if should_build:
            cmd = ["cargo", "build", "--target-dir", str(build_dir)]
            build_type = self._determine_build_type(options=options)
            normalized = build_type.lower()
            if normalized == "release":
                cmd.append("--release")
            elif normalized not in {"debug"}:
                cmd.extend(["--profile", normalized])
            cmd.extend(extra_build_args)
            steps.append(
                BuildStep(
                    description="Build cargo project",
                    command=cmd,
                    cwd=effective_source_dir,
                    env=environment,
                )
            )

        if options.install:
            raise ValueError("Install mode is not supported for cargo projects")

        return steps

    def _install_command(
        self,
        *,
        build_system: str,
        build_dir: Path,
        is_multi_config: bool,
        options: BuildOptions,
    ) -> List[str]:
        build_type = self._determine_build_type(options=options)

        if build_system == "cmake":
            cmd = ["cmake", "--install", str(build_dir)]
            if is_multi_config:
                cmd.extend(["--config", build_type])
            return cmd

        if build_system == "meson":
            cmd = ["meson", "install", "-C", str(build_dir)]
            return cmd

        if build_system == "cargo":
            raise ValueError("Install mode is not supported for cargo projects")

        raise ValueError(f"Install mode is not supported for build system '{build_system}'")

    def _ensure_toolchain_compatibility(
        self,
        *,
        build_system: str | None,
        toolchain: str,
        definition: ToolchainDefinition | None,
    ) -> None:
        if not build_system:
            return
        if definition is None:
            raise ValueError(f"Toolchain '{toolchain}' is not defined")
        if definition.supported_build_systems and build_system not in definition.supported_build_systems:
            allowed_systems = ", ".join(sorted(definition.supported_build_systems))
            raise ValueError(
                f"Toolchain '{toolchain}' is not compatible with build system '{build_system}'. "
                f"Allowed systems: {allowed_systems}"
            )

    def _determine_linker(
        self,
        *,
        toolchain: str,
        os_name: str,
        definition: ToolchainDefinition | None = None,
    ) -> str | None:
        if definition is None:
            definition = self._store.toolchains.get(toolchain)
        if definition:
            resolved = definition.resolved_linker
            if resolved:
                return resolved
        if os_name == "windows" or toolchain == "msvc":
            return None
        if shutil.which("mold"):
            return "mold"
        if toolchain == "clang" and shutil.which("lld"):
            return "lld"
        if toolchain == "gcc" and shutil.which("gold"):
            return "gold"
        return "ld"

    def _append_flag(self, container: Dict[str, str], key: str, flag: str) -> None:
        existing = container.get(key)
        if existing:
            if flag in existing.split():
                return
            container[key] = f"{existing} {flag}".strip()
        else:
            container[key] = flag

    def _append_definition_flag(self, definitions: Dict[str, Any], key: str, flag: str) -> None:
        existing = definitions.get(key)
        if existing:
            str_value = str(existing)
            if flag in str_value.split():
                return
            definitions[key] = f"{str_value} {flag}".strip()
        else:
            definitions[key] = flag

    def _apply_color_diagnostics(
        self,
        *,
        project: ProjectDefinition,
        environment: Dict[str, str],
        definitions: Dict[str, Any],
        toolchain: str,
    ) -> None:
        flag_map = {
            "clang": "-fcolor-diagnostics",
            "gcc": "-fdiagnostics-color=always",
            "msvc": "/d2ColorizeDiagnostics",
        }
        flag = flag_map.get(toolchain)
        if not flag:
            return

        if toolchain == "msvc":
            self._append_flag(environment, "CL", flag)
        else:
            self._append_flag(environment, "CFLAGS", flag)
            self._append_flag(environment, "CXXFLAGS", flag)

        if project.build_system == "cmake":
            self._append_definition_flag(definitions, "CMAKE_C_FLAGS", flag)
            self._append_definition_flag(definitions, "CMAKE_CXX_FLAGS", flag)
        elif project.build_system == "meson":
            pass  # Meson respects CFLAGS/CXXFLAGS environment

    def _apply_cmake_build_type(
        self,
        *,
        project: ProjectDefinition,
        definitions: Dict[str, Any],
        build_type: str,
        build_type_override: str | None,
        generator: str | None,
    ) -> None:
        if project.build_system != "cmake":
            return
        if self._is_multi_config_generator(generator):
            return
        if build_type_override is not None:
            definitions["CMAKE_BUILD_TYPE"] = build_type
            return
        if "CMAKE_BUILD_TYPE" not in definitions:
            definitions["CMAKE_BUILD_TYPE"] = build_type

    def _apply_cmake_toolchain(
        self,
        *,
        build_system: str,
        definitions: Dict[str, Any],
        environment: Dict[str, str],
        cc: str | None,
        cxx: str | None,
        linker: str | None,
        launcher: str | None,
    ) -> None:
        if build_system != "cmake":
            return
        if cc and "CMAKE_C_COMPILER" not in definitions:
            definitions["CMAKE_C_COMPILER"] = cc
        if cxx and "CMAKE_CXX_COMPILER" not in definitions:
            definitions["CMAKE_CXX_COMPILER"] = cxx
        if launcher:
            if "CMAKE_C_COMPILER_LAUNCHER" not in definitions:
                definitions["CMAKE_C_COMPILER_LAUNCHER"] = launcher
            if "CMAKE_CXX_COMPILER_LAUNCHER" not in definitions:
                definitions["CMAKE_CXX_COMPILER_LAUNCHER"] = launcher
        cmake_linker = linker or environment.get("CXX_LD") or environment.get("CC_LD")
        if cmake_linker and "CMAKE_LINKER" not in definitions:
            definitions["CMAKE_LINKER"] = cmake_linker
        if "CMAKE_EXPORT_COMPILE_COMMANDS" not in definitions:
            definitions["CMAKE_EXPORT_COMPILE_COMMANDS"] = True

    def _format_cmake_value(self, value: Any) -> str:
        if isinstance(value, bool):
            return "ON" if value else "OFF"
        if isinstance(value, (int, float)):
            return str(value)
        return str(value)

    def _format_meson_value(self, value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def _cmake_definition_flag(self, *, name: str, value: Any) -> str:
        type_hint = self._cmake_definition_type(value)
        formatted_value = self._format_cmake_value(value)
        if type_hint:
            return f"{name}:{type_hint}={formatted_value}"
        return f"{name}={formatted_value}"

    def _cmake_definition_type(self, value: Any) -> str | None:
        if isinstance(value, bool):
            return "BOOL"
        if isinstance(value, int) and not isinstance(value, bool):
            return "NUMBER"
        if isinstance(value, float):
            return "NUMBER"
        if isinstance(value, str):
            return "STRING"
        return None

    def serialize_plan(self, plan: BuildPlan) -> str:
        data = {
            "project": plan.project.name,
            "build_dir": str(plan.build_dir),
            "install_dir": str(plan.install_dir) if plan.install_dir else None,
            "source_dir": str(plan.source_dir),
            "configure_source_dir": str(plan.configure_source_dir),
            "component_dir": str(plan.component_dir) if plan.component_dir else None,
            "steps": [
                {
                    "description": step.description,
                    "command": list(step.command),
                    "cwd": str(step.cwd),
                }
                for step in plan.steps
            ],
            "context": plan.context,
            "presets": plan.presets,
            "environment": plan.environment,
            "definitions": plan.definitions,
                "extra_config_args": plan.extra_config_args,
                "extra_build_args": plan.extra_build_args,
        }
        return json.dumps(data, indent=2)
