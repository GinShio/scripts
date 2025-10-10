"""Core build planning and execution logic."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence
import json
import shutil

from .command_runner import CommandRunner, CommandResult
from .config_loader import ConfigurationStore, ProjectDefinition
from .environment import ContextBuilder
from .presets import PresetRepository
from .template import TemplateResolver


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
    extra_args: List[str] = field(default_factory=list)
    toolchain: str | None = None
    install_dir: str | None = None
    operation: BuildMode = BuildMode.AUTO


@dataclass(slots=True)
class BuildStep:
    description: str
    command: Sequence[str]
    cwd: Path
    env: Dict[str, str]


@dataclass(slots=True)
class BuildPlan:
    project: ProjectDefinition
    build_dir: Path
    install_dir: Path | None
    source_dir: Path
    steps: List[BuildStep]
    context: Dict[str, Any]


_TOOLCHAIN_MATRIX: Dict[str, set[str]] = {
    "cmake": {"clang", "gcc", "msvc"},
    "meson": {"clang", "gcc", "msvc"},
    "bazel": {"clang", "gcc", "msvc"},
    "cargo": {"rustc"},
    "make": {"clang", "gcc"},
}


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

    def plan(self, options: BuildOptions) -> BuildPlan:
        project = self._store.get_project(options.project_name)
        builder_path = self._workspace
        context_builder = ContextBuilder(builder_path)

        branch = options.branch or project.git.main_branch
        build_type = options.build_type or self._store.global_config.default_build_type
        generator = options.generator or project.generator
        user_ctx = context_builder.user(branch=branch, build_type=build_type, generator=generator, operation=options.operation.value)
        system_ctx = context_builder.system()

        source_dir_str = project.source_dir
        build_dir_str = project.build_dir
        install_dir_str = options.install_dir or project.install_dir
        component_dir_str = project.component_dir

        project_ctx = context_builder.project(
            name=project.name,
            source_dir=Path(source_dir_str),
            build_dir=Path(build_dir_str),
            install_dir=Path(install_dir_str) if install_dir_str else None,
            component_dir=Path(component_dir_str) if component_dir_str else None,
        )

        combined_context = context_builder.combined_context(user=user_ctx, project=project_ctx, system=system_ctx)
        resolver = TemplateResolver(combined_context)

        source_dir = Path(resolver.resolve(source_dir_str)).expanduser()
        build_dir = Path(resolver.resolve(build_dir_str))
        install_dir = None
        if install_dir_str:
            install_dir = Path(resolver.resolve(install_dir_str))
        component_dir = None
        if component_dir_str:
            component_dir = Path(resolver.resolve(component_dir_str))

        effective_source_dir = source_dir
        if component_dir:
            effective_source_dir = (source_dir / component_dir).resolve()

        if not project.build_at_root and component_dir:
            build_root = effective_source_dir
        else:
            build_root = source_dir
        build_dir_path = (build_root / build_dir).resolve()
        if install_dir and not install_dir.is_absolute():
            install_dir = (source_dir / install_dir).resolve()

        updated_project_ctx = context_builder.project(
            name=project.name,
            source_dir=source_dir,
            build_dir=build_dir_path,
            install_dir=install_dir,
            component_dir=component_dir,
        )
        combined_context = context_builder.combined_context(user=user_ctx, project=updated_project_ctx, system=system_ctx)
        resolver = TemplateResolver(combined_context)

        preset_repo = PresetRepository(
            project_presets=project.presets,
            shared_presets=[cfg.get("presets", {}) for cfg in self._store.shared_configs.values()],
        )
        presets_to_resolve = options.presets or []
        resolved_presets = preset_repo.resolve(presets_to_resolve, template_resolver=resolver)

        environment = dict(resolved_presets.environment)
        definitions = dict(resolved_presets.definitions)
        extra_args = [*resolved_presets.extra_args, *options.extra_args]

        toolchain = options.toolchain or self._default_toolchain(system_ctx.os_name)
        self._ensure_toolchain_compatibility(project.build_system, toolchain)
        environment.setdefault("CC", self._default_cc(toolchain))
        environment.setdefault("CXX", self._default_cxx(toolchain))

        plan_steps = self._create_build_steps(
            project=project,
            effective_source_dir=effective_source_dir,
            build_dir=build_dir_path,
            install_dir=install_dir,
            environment=environment,
            definitions=definitions,
            extra_args=extra_args,
            options=options,
        )

        return BuildPlan(
            project=project,
            build_dir=build_dir_path,
            install_dir=install_dir,
            source_dir=effective_source_dir,
            steps=plan_steps,
            context=combined_context,
        )

    def execute(self, plan: BuildPlan, *, dry_run: bool) -> List[CommandResult]:
        results: List[CommandResult] = []
        if dry_run:
            for step in plan.steps:
                print(f"[dry-run] {step.description}: {' '.join(step.command)}")
            return results

        build_dir_parent = plan.build_dir.parent
        build_dir_parent.mkdir(parents=True, exist_ok=True)

        for step in plan.steps:
            cwd = step.cwd
            cwd.mkdir(parents=True, exist_ok=True)
            result = self._command_runner.run(step.command, cwd=cwd, env=step.env)
            results.append(result)
        return results

    def _create_build_steps(
        self,
        *,
        project: ProjectDefinition,
        effective_source_dir: Path,
        build_dir: Path,
        install_dir: Path | None,
        environment: Dict[str, str],
        definitions: Dict[str, Any],
        extra_args: List[str],
        options: BuildOptions,
    ) -> List[BuildStep]:
        steps: List[BuildStep] = []
        mode = options.operation
        build_dir_exists = build_dir.exists()
        env = environment

        if mode is BuildMode.BUILD_ONLY and not build_dir_exists:
            raise ValueError("Build directory does not exist; run configuration first or use auto mode")

        if mode is BuildMode.RECONFIG and build_dir_exists:
            shutil.rmtree(build_dir)
            build_dir_exists = False

        if project.build_system == "cmake":
            steps.extend(
                self._cmake_steps(
                    effective_source_dir=effective_source_dir,
                    build_dir=build_dir,
                    install_dir=install_dir,
                    environment=env,
                    definitions=definitions,
                    extra_args=extra_args,
                    options=options,
                    build_dir_exists=build_dir_exists,
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
                    extra_args=extra_args,
                    options=options,
                    build_dir_exists=build_dir_exists,
                )
            )
        elif project.build_system == "bazel":
            steps.extend(
                self._bazel_steps(
                    effective_source_dir=effective_source_dir,
                    environment=env,
                    definitions=definitions,
                    extra_args=extra_args,
                    options=options,
                )
            )
        else:
            raise ValueError(f"Unsupported build system: {project.build_system}")
        return steps

    def _cmake_steps(
        self,
        *,
        effective_source_dir: Path,
        build_dir: Path,
        install_dir: Path | None,
        environment: Dict[str, str],
        definitions: Dict[str, Any],
        extra_args: List[str],
        options: BuildOptions,
        build_dir_exists: bool,
    ) -> List[BuildStep]:
        steps: List[BuildStep] = []
        mode = options.operation

        should_configure = mode in {BuildMode.AUTO, BuildMode.CONFIG_ONLY, BuildMode.RECONFIG} or not build_dir_exists
        should_build = mode in {BuildMode.AUTO, BuildMode.BUILD_ONLY}

        if should_configure:
            args: List[str] = ["cmake"]
            if options.generator:
                args.extend(["-G", options.generator])
            for key, value in definitions.items():
                args.extend(["-D", f"{key}={self._format_cmake_value(value)}"])
            args.extend(["-B", str(build_dir), "-S", str(effective_source_dir)])
            args.extend(extra_args)
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
            cmd.extend(extra_args)
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
            cmd = ["cmake", "--install", str(build_dir), "--prefix", str(install_dir)]
            steps.append(
                BuildStep(
                    description="Install project",
                    command=cmd,
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
        extra_args: List[str],
        options: BuildOptions,
        build_dir_exists: bool,
    ) -> List[BuildStep]:
        steps: List[BuildStep] = []
        mode = options.operation

        should_configure = mode in {BuildMode.AUTO, BuildMode.CONFIG_ONLY, BuildMode.RECONFIG} or not build_dir_exists
        should_build = mode in {BuildMode.AUTO, BuildMode.BUILD_ONLY}

        if should_configure:
            args = ["meson", "setup", str(build_dir), str(effective_source_dir)]
            for key, value in definitions.items():
                args.append(f"--{key}={value}")
            args.extend(extra_args)
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
            cmd.extend(extra_args)
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
            cmd = ["meson", "install", "-C", str(build_dir), "--destdir", str(install_dir)]
            steps.append(
                BuildStep(
                    description="Install project",
                    command=cmd,
                    cwd=effective_source_dir,
                    env=environment,
                )
            )
        return steps

    def _bazel_steps(
        self,
        *,
        effective_source_dir: Path,
        environment: Dict[str, str],
        definitions: Dict[str, Any],
        extra_args: List[str],
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
        cmd.extend(extra_args)
        steps.append(
            BuildStep(
                description="Build project",
                command=cmd,
                cwd=effective_source_dir,
                env=environment,
            )
        )
        return steps

    def _ensure_toolchain_compatibility(self, build_system: str, toolchain: str) -> None:
        allowed = _TOOLCHAIN_MATRIX.get(build_system)
        if not allowed:
            return
        if toolchain not in allowed:
            raise ValueError(
                f"Toolchain '{toolchain}' is not compatible with build system '{build_system}'. "
                f"Allowed: {', '.join(sorted(allowed))}"
            )

    def _default_toolchain(self, os_name: str) -> str:
        return "msvc" if os_name == "windows" else "clang"

    def _default_cc(self, toolchain: str) -> str:
        if toolchain == "msvc":
            return "cl"
        if toolchain == "gcc":
            return "gcc"
        return "clang"

    def _default_cxx(self, toolchain: str) -> str:
        if toolchain == "msvc":
            return "cl"
        if toolchain == "gcc":
            return "g++"
        return "clang++"

    def _format_cmake_value(self, value: Any) -> str:
        if isinstance(value, bool):
            return "ON" if value else "OFF"
        if isinstance(value, (int, float)):
            return str(value)
        return str(value)

    def serialize_plan(self, plan: BuildPlan) -> str:
        data = {
            "project": plan.project.name,
            "build_dir": str(plan.build_dir),
            "install_dir": str(plan.install_dir) if plan.install_dir else None,
            "source_dir": str(plan.source_dir),
            "steps": [
                {
                    "description": step.description,
                    "command": list(step.command),
                    "cwd": str(step.cwd),
                }
                for step in plan.steps
            ],
            "context": plan.context,
        }
        return json.dumps(data, indent=2)
