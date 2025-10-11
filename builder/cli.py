"""Command line interface for the builder tool."""
from __future__ import annotations

from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Iterable, List
import os
import sys

from .build import BuildEngine, BuildMode, BuildOptions
from .command_runner import RecordingCommandRunner, SubprocessCommandRunner
from .config_loader import ConfigurationStore
from .git_manager import GitManager


def _make_runner(dry_run: bool) -> SubprocessCommandRunner | RecordingCommandRunner:
    return RecordingCommandRunner() if dry_run else SubprocessCommandRunner()


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


def _split_config_values(values: Iterable[str]) -> List[str]:
    parts: List[str] = []
    separator = os.pathsep
    for value in values:
        if not value:
            continue
        text = value.strip()
        if not text:
            continue
        segments = text.split(separator) if separator in text else [text]
        for segment in segments:
            trimmed = segment.strip()
            if trimmed:
                parts.append(trimmed)
    return parts


def _resolve_config_directories(workspace: Path, cli_values: Iterable[str]) -> List[Path]:
    config_dirs: List[Path] = [workspace / "config"]

    env_value = os.environ.get("BUILDER_CONFIG_DIR")
    if env_value:
        for entry in _split_config_values([env_value]):
            path = Path(entry)
            if not path.is_absolute():
                path = workspace / path
            config_dirs.append(path)

    for entry in _split_config_values(cli_values):
        path = Path(entry)
        if not path.is_absolute():
            path = workspace / path
        config_dirs.append(path)

    ordered: List[Path] = []
    for path in config_dirs:
        if path in ordered:
            ordered.remove(path)
        ordered.append(path)
    return ordered


def _load_configuration_store(args: Namespace, workspace: Path) -> ConfigurationStore:
    cli_dirs: Iterable[str] = getattr(args, "config_dirs", [])
    directories = _resolve_config_directories(workspace, cli_dirs)
    return ConfigurationStore.from_directories(workspace, directories)

def _emit_dry_run_output(runner: RecordingCommandRunner, *, workspace: Path) -> None:
    for line in runner.iter_formatted(workspace=workspace):
        print(line)


def _parse_arguments(argv: Iterable[str]) -> Namespace:
    parser = ArgumentParser(prog="builder", description="Preset-driven build orchestrator")
    parser.add_argument(
        "--config-dir",
        dest="config_dirs",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional configuration directory (repeat or separate with PATH separator)",
    )
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
    validate_parser.add_argument("project", nargs="?", help="Validate a single project by name")

    update_parser = subparsers.add_parser("update", help="Update Git repositories")
    update_parser.add_argument("project", nargs="?", help="Project to update; omit to update all")
    update_parser.add_argument("--branch", help="Branch to checkout during update")
    update_parser.add_argument("--submodule", choices=["default", "latest", "skip"], default="default", help="Submodule update strategy")
    update_parser.add_argument("--dry-run", action="store_true", help="Preview git commands without executing them")

    list_parser = subparsers.add_parser(
        "list",
        help="List project repositories, their commits, and submodule information",
    )
    list_parser.add_argument("project", nargs="?", help="Project to inspect; omit to list all projects")
    list_parser.add_argument("--branch", help="Git branch to inspect (switches repositories unless --no-switch-branch is used)")
    list_parser.add_argument(
        "--no-switch-branch",
        action="store_true",
        help="Do not switch Git branches automatically when inspecting repositories",
    )
    list_parser.add_argument(
        "--url",
        action="store_true",
        help="Include repository and submodule URLs in the listing",
    )
    list_parser.add_argument(
        "--presets",
        action="store_true",
        help="Include preset names in the listing",
    )
    list_parser.add_argument(
        "--dependencies",
        action="store_true",
        help="Include dependency information in the listing",
    )

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
    if args.command == "list":
        return _handle_list(args, workspace)
    raise ValueError(f"Unknown command: {args.command}")


def _handle_build(args: Namespace, workspace: Path) -> int:
    store = _load_configuration_store(args, workspace)
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

    runner = _make_runner(args.dry_run)

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

        target_branch = plan.branch
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
                git_manager.restore_checkout(
                    plan.source_dir,
                    state,
                    environment=plan.git_environment,
                )

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
    store = _load_configuration_store(args, workspace)
    if args.project:
        project_names = [args.project]
    else:
        project_names = sorted(store.list_projects())

    errors: List[tuple[str, str]] = []
    for project_name in project_names:
        try:
            _validate_project(store, project_name)
        except Exception as exc:
            message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            errors.append((project_name, message))

    if errors:
        print("Validation failed:")
        for project_name, message in errors:
            print(f"  [{project_name}] {message}")
        return 1

    print("Validation successful")
    return 0


def _handle_update(args: Namespace, workspace: Path) -> int:
    store = _load_configuration_store(args, workspace)
    runner = _make_runner(args.dry_run)
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
            main_branch=plan.branch,
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


def _handle_list(args: Namespace, workspace: Path) -> int:
    store = _load_configuration_store(args, workspace)
    runner = SubprocessCommandRunner()
    git_manager = GitManager(runner)
    planning_engine = BuildEngine(store=store, command_runner=RecordingCommandRunner(), workspace=workspace)

    if args.project:
        project_names = [args.project]
    else:
        project_names = sorted(store.list_projects())

    include_url = bool(getattr(args, "url", False))
    include_presets = bool(getattr(args, "presets", False))
    include_dependencies = bool(getattr(args, "dependencies", False))
    rows: List[dict[str, str]] = []
    include_submodules = not (include_presets or include_dependencies)

    for name in project_names:
        try:
            project = store.get_project(name)
        except KeyError as exc:
            print(str(exc))
            continue

        try:
            options = BuildOptions(
                project_name=project.name,
                presets=[],
                branch=args.branch,
                no_switch_branch=args.no_switch_branch,
                operation=BuildMode.CONFIG_ONLY,
            )
            plan = planning_engine.plan(options)
        except ValueError as exc:
            print(f"Warning: Configuration error for project '{name}': {exc}")
            continue
        except Exception as exc:
            print(f"Warning: Error processing project '{name}': {exc}")
            continue

        repo_path = plan.source_dir
        project_url = project.git.url or "-"

        branch: str | None = None
        commit: str | None = None
        state = None
        repo_ready = repo_path.exists() and (repo_path / ".git").exists()
        submodules: List[dict[str, str]] = []

        try:
            if repo_ready:
                component_dir_arg: Path | None = None
                if project.component_dir:
                    if isinstance(plan.context, dict):
                        project_ctx = plan.context.get("project", {})  # type: ignore[arg-type]
                    else:
                        project_ctx = {}
                    resolved_component = None
                    if isinstance(project_ctx, dict):
                        resolved_component = project_ctx.get("component_dir")
                    if resolved_component:
                        resolved_path = Path(resolved_component)
                        try:
                            component_dir_arg = resolved_path.relative_to(repo_path)
                        except ValueError:
                            component_dir_arg = Path(project.component_dir)
                    else:
                        component_dir_arg = Path(project.component_dir)

                state = git_manager.prepare_checkout(
                    repo_path=repo_path,
                    target_branch=plan.branch,
                    auto_stash=plan.project.git.auto_stash,
                    no_switch_branch=args.no_switch_branch,
                    environment=plan.git_environment,
                    component_dir=component_dir_arg,
                    component_branch=plan.branch if component_dir_arg else None,
                )
            branch, commit = git_manager.get_repository_state(repo_path, environment=plan.git_environment)
            if repo_path.exists():
                submodules = git_manager.list_submodules(repo_path, environment=plan.git_environment)
        except RuntimeError as exc:
            print(f"Warning: Could not prepare repository '{name}': {exc}")
        finally:
            if state is not None:
                try:
                    git_manager.restore_checkout(
                        repo_path,
                        state,
                        environment=plan.git_environment,
                    )
                except RuntimeError as exc:
                    print(f"Warning: Could not restore repository '{name}': {exc}")

        branch_display = branch or "-"
        commit_display = commit[:11] if commit else "<missing>"
        row: dict[str, str] = {
            "Project": project.name,
            "Branch": branch_display,
            "Commit": commit_display,
            "Path": str(repo_path),
        }
        if include_url:
            row["URL"] = project_url
        if include_presets:
            preset_names = ", ".join(sorted(project.presets)) if project.presets else "-"
            row["Presets"] = preset_names
        if include_dependencies:
            dependency_entries: List[str] = []
            for dependency in project.dependencies:
                if dependency.presets:
                    dependency_entries.append(
                        f"{dependency.name} ({', '.join(dependency.presets)})"
                    )
                else:
                    dependency_entries.append(dependency.name)
            row["Dependencies"] = ", ".join(dependency_entries) if dependency_entries else "-"
        rows.append(row)

        if not repo_path.exists():
            print(f"Warning: Repository path '{repo_path}' does not exist for project '{name}'")
            continue

        if not include_submodules:
            continue

        for submodule in submodules:
            hash_value = submodule.get("hash")
            hash_display = hash_value[:11] if hash_value else "<missing>"
            url_display = submodule.get("url") or "-"
            submodule_row: dict[str, str] = {
                "Project": "",
                "Branch": "",
                "Commit": hash_display,
                "Path": submodule.get("path", "-"),
            }
            if include_url:
                submodule_row["URL"] = url_display
            if include_presets:
                submodule_row["Presets"] = ""
            if include_dependencies:
                submodule_row["Dependencies"] = ""
            rows.append(submodule_row)

    if not rows:
        print("No projects found")
        return 0

    headers: List[str] = ["Project", "Branch", "Commit", "Path"]
    if include_url:
        headers.append("URL")
    if include_presets:
        headers.append("Presets")
    if include_dependencies:
        headers.append("Dependencies")

    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            value = row.get(header, "")
            widths[header] = max(widths[header], len(value))

    def _format(row: dict[str, str]) -> str:
        return "  ".join(row.get(header, "").ljust(widths[header]) for header in headers)

    header_row = {header: header for header in headers}
    print(_format(header_row))
    print("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        print(_format(row))
    return 0


def _validate_project(store: ConfigurationStore, name: str) -> None:
    project = store.get_project(name)
    errors: List[str] = []

    if not project.source_dir:
        errors.append("project.source_dir must be defined")

    build_system = project.build_system
    allowed_systems = {"cmake", "meson", "bazel", "cargo", "make"}

    if build_system is not None and build_system not in allowed_systems:
        errors.append(
            f"project.build_system '{build_system}' is not supported (allowed: {', '.join(sorted(allowed_systems))})"
        )

    if project.build_dir:
        build_dir_path = Path(project.build_dir)
        if build_dir_path.is_absolute():
            errors.append("project.build_dir must be a relative path")
    required_build_dir_systems = {"cmake", "meson", "cargo", "make"}
    if build_system in required_build_dir_systems and not project.build_dir:
        errors.append(f"project.build_dir is required for build_system '{build_system}'")

    if project.component_dir:
        component_path = Path(project.component_dir)
        if component_path.is_absolute():
            errors.append("project.component_dir must be a relative path")

    if errors:
        raise ValueError("; ".join(errors))


def _collect_presets(values: List[str]) -> List[str]:
    presets: List[str] = []
    for value in values:
        if not value:
            continue
        presets.extend(part.strip() for part in value.split(",") if part.strip())
    return presets


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
