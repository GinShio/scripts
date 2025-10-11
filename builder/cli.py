"""Command line interface for the builder tool."""
from __future__ import annotations

from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Iterable, List
import sys

from .build import BuildEngine, BuildMode, BuildOptions
from .command_runner import RecordingCommandRunner, SubprocessCommandRunner
from .config_loader import ConfigurationStore
from .git_manager import GitManager


def _flatten_arg_groups(groups: Iterable[Iterable[str]]) -> List[str]:
    flattened: List[str] = []
    for group in groups:
        for value in group:
            if value:
                flattened.append(value)
    return flattened


def _parse_extra_switches(values: Iterable[str]) -> tuple[List[str], List[str]]:
    config_args: List[str] = []
    build_args: List[str] = []

    for raw in values:
        if raw is None:
            continue
        text = raw.strip()
        if not text:
            continue

        scope: str | None = None
        payload = text

        if "," in text:
            prefix, _, remainder = text.partition(",")
            candidate = prefix.strip().lower()
            if candidate in {"config", "build"} and remainder:
                scope = candidate
                payload = remainder
            else:
                payload = text

        parts = [part.strip() for part in payload.split(",") if part.strip()]
        if not parts:
            continue

        targets: List[List[str]]
        if scope == "config":
            targets = [config_args]
        elif scope == "build":
            targets = [build_args]
        else:
            targets = [config_args, build_args]

        for part in parts:
            for target in targets:
                target.append(part)

    return config_args, build_args

def _emit_dry_run_output(runner: RecordingCommandRunner, *, workspace: Path) -> None:
    for line in runner.iter_formatted(workspace=workspace):
        print(line)


def _parse_arguments(argv: Iterable[str]) -> Namespace:
    parser = ArgumentParser(prog="builder", description="Preset-driven build orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="Configure and build a project")
    build_parser.add_argument("project", help="Project name to build")
    build_parser.add_argument("--preset", action="append", default=[], help="Preset name(s) to apply (comma-separated)")
    build_parser.add_argument("--branch", help="Git branch to use for the build")
    build_parser.add_argument("--build-type", help="Override build type (Debug/Release)")
    build_parser.add_argument("--generator", help="Override build system generator")
    build_parser.add_argument("--target", help="Build a specific target")
    build_parser.add_argument("--install", action="store_true", help="Install after build")
    build_parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them")
    build_parser.add_argument("--show-vars", action="store_true", help="Display resolved variables before building")
    build_parser.add_argument("--no-switch-branch", action="store_true", help="Do not switch Git branches automatically")
    build_parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    build_parser.add_argument("--toolchain", help="Specify the toolchain to use")
    build_parser.add_argument("--install-dir", help="Override install directory")
    build_parser.add_argument("--config-only", action="store_true", help="Run configuration only")
    build_parser.add_argument("--build-only", action="store_true", help="Run build only")
    build_parser.add_argument("--reconfig", action="store_true", help="Clean and reconfigure the build directory")
    build_parser.add_argument(
        "-X",
        dest="extra_switches",
        action="append",
        default=[],
        metavar="SCOPE,ARG",
        help="Extra arguments (use -Xconfig,<arg> or -Xbuild,<arg>; omit scope for both)",
    )
    build_parser.add_argument(
        "--extra-config-args",
        dest="extra_config_args",
        action="append",
        nargs="+",
        default=[],
        metavar="ARG",
        help="Additional arguments appended to configuration commands",
    )
    build_parser.add_argument(
        "--extra-build-args",
        dest="extra_build_args",
        action="append",
        nargs="+",
        default=[],
        metavar="ARG",
        help="Additional arguments appended to build commands",
    )

    validate_parser = subparsers.add_parser("validate", help="Validate configuration files")
    validate_parser.add_argument("--project", help="Validate a single project by name")

    update_parser = subparsers.add_parser("update", help="Update Git repositories")
    update_parser.add_argument("project", nargs="?", help="Project to update; omit to update all")
    update_parser.add_argument("--branch", help="Branch to checkout during update")
    update_parser.add_argument("--submodule", choices=["default", "latest", "skip"], default="default", help="Submodule update strategy")
    update_parser.add_argument("--dry-run", action="store_true", help="Preview git commands without executing them")

    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_arguments(argv or sys.argv[1:])
    workspace = Path.cwd()

    if args.command == "build":
        return _handle_build(args, workspace)
    if args.command == "validate":
        return _handle_validate(args, workspace)
    if args.command == "update":
        return _handle_update(args, workspace)
    raise ValueError(f"Unknown command: {args.command}")


def _handle_build(args: Namespace, workspace: Path) -> int:
    store = ConfigurationStore.from_directory(workspace)
    switch_config_args, switch_build_args = _parse_extra_switches(getattr(args, "extra_switches", []))
    cli_extra_config_args = _flatten_arg_groups(getattr(args, "extra_config_args", []))
    cli_extra_build_args = _flatten_arg_groups(getattr(args, "extra_build_args", []))
    extra_config_args = [*switch_config_args, *cli_extra_config_args]
    extra_build_args = [*switch_build_args, *cli_extra_build_args]
    dependencies = store.resolve_dependency_chain(args.project)
    presets = _collect_presets(args.preset)
    operation = BuildMode.AUTO
    if args.config_only:
        operation = BuildMode.CONFIG_ONLY
    if args.build_only:
        operation = BuildMode.BUILD_ONLY
    if args.reconfig:
        operation = BuildMode.RECONFIG

    build_options = BuildOptions(
        project_name=args.project,
        presets=presets,
        branch=args.branch,
        build_type=args.build_type,
        generator=args.generator,
        target=args.target,
        install=args.install,
        dry_run=args.dry_run,
        show_vars=args.show_vars,
        no_switch_branch=args.no_switch_branch,
        verbose=args.verbose,
        extra_config_args=extra_config_args,
        extra_build_args=extra_build_args,
        toolchain=args.toolchain,
        install_dir=args.install_dir,
        operation=operation,
    )

    runner: SubprocessCommandRunner | RecordingCommandRunner
    if args.dry_run:
        runner = RecordingCommandRunner()
    else:
        runner = SubprocessCommandRunner()

    engine = BuildEngine(store=store, command_runner=runner, workspace=workspace)
    git_manager = GitManager(runner)

    def run_project(options: BuildOptions, *, show_vars: bool) -> None:
        plan = engine.plan(options)

        if show_vars:
            from pprint import pprint

            print("Resolved variables:")
            pprint(plan.context)
            if plan.environment:
                print("Preset environment overrides:")
                pprint(plan.environment)

        target_branch = options.branch or plan.project.git.main_branch
        state = None
        plan_has_steps = bool(plan.steps)
        if plan_has_steps and not args.dry_run:
            state = git_manager.prepare_checkout(
                repo_path=plan.source_dir,
                target_branch=target_branch,
                auto_stash=plan.project.git.auto_stash,
                no_switch_branch=options.no_switch_branch,
                environment=plan.git_environment,
            )

        if not plan_has_steps:
            print(f"No build steps for project '{plan.project.name}' (build directory not configured)")
            return

        try:
            engine.execute(plan, dry_run=args.dry_run)
        finally:
            if state is not None:
                git_manager.restore_checkout(plan.source_dir, state)

    for dependency in dependencies:
        dep_options = BuildOptions(
            project_name=dependency.project.name,
            presets=list(dependency.presets) if dependency.presets else [],
            branch=build_options.branch,
            build_type=build_options.build_type,
            generator=build_options.generator,
            target=None,
            install=False,
            dry_run=build_options.dry_run,
            show_vars=False,
            no_switch_branch=build_options.no_switch_branch,
            verbose=build_options.verbose,
            extra_config_args=list(build_options.extra_config_args),
            extra_build_args=list(build_options.extra_build_args),
            toolchain=build_options.toolchain,
            install_dir=None,
            operation=build_options.operation,
        )
        run_project(dep_options, show_vars=False)

    run_project(build_options, show_vars=args.show_vars)

    if args.dry_run and isinstance(runner, RecordingCommandRunner):
        _emit_dry_run_output(runner, workspace=workspace)
    return 0


def _handle_validate(args: Namespace, workspace: Path) -> int:
    store = ConfigurationStore.from_directory(workspace)
    if args.project:
        _validate_project(store, args.project)
    else:
        for project_name in store.list_projects():
            _validate_project(store, project_name)
    print("Validation successful")
    return 0


def _handle_update(args: Namespace, workspace: Path) -> int:
    store = ConfigurationStore.from_directory(workspace)
    runner: SubprocessCommandRunner | RecordingCommandRunner
    if args.dry_run:
        runner = RecordingCommandRunner()
    else:
        runner = SubprocessCommandRunner()
    git_manager = GitManager(runner)
    planning_engine = BuildEngine(store=store, command_runner=RecordingCommandRunner(), workspace=workspace)

    if args.project:
        projects = [store.get_project(args.project)]
    else:
        projects = [store.projects[name] for name in store.projects]

    for project in projects:
        options = BuildOptions(
            project_name=project.name,
            presets=[],
            branch=args.branch,
            extra_config_args=[],
            extra_build_args=[],
            operation=BuildMode.CONFIG_ONLY,
        )
        plan = planning_engine.plan(options)
        source_dir = plan.source_dir
        git_manager.update_repository(
            repo_path=source_dir,
            url=project.git.url,
            main_branch=args.branch or project.git.main_branch,
            component_branch=project.git.component_branch,
            clone_script=plan.git_clone_script,
            update_script=plan.git_update_script,
            auto_stash=project.git.auto_stash,
            environment=plan.git_environment,
            dry_run=args.dry_run,
        )
    if args.dry_run and isinstance(runner, RecordingCommandRunner):
        _emit_dry_run_output(runner, workspace=workspace)
    return 0


def _validate_project(store: ConfigurationStore, name: str) -> None:
    project = store.get_project(name)
    if not project.source_dir:
        raise ValueError(f"Project '{name}' has empty source_dir")
    if project.build_dir is None:
        return
    if project.build_system not in {"cmake", "meson", "bazel", "cargo", "make"}:
        raise ValueError(f"Project '{name}' uses unsupported build system '{project.build_system}'")


def _collect_presets(values: List[str]) -> List[str]:
    presets: List[str] = []
    for value in values:
        if not value:
            continue
        presets.extend(part.strip() for part in value.split(",") if part.strip())
    return presets


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
