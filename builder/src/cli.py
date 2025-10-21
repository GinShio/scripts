"""Command line interface for the builder tool."""
from __future__ import annotations

from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Iterable, List
import os
import sys

from .build import BuildEngine, BuildMode, BuildOptions, BuildPlan
from .command_runner import RecordingCommandRunner, SubprocessCommandRunner
from .config_loader import ConfigurationStore
from .git_manager import GitManager
from .validation import validate_project, validate_project_templates, validate_store_structure


def _make_runner(dry_run: bool) -> SubprocessCommandRunner | RecordingCommandRunner:
    return RecordingCommandRunner() if dry_run else SubprocessCommandRunner()


def _flatten_arg_groups(groups: Iterable[Iterable[str]]) -> List[str]:
    flattened: List[str] = []
    for group in groups:
        for value in group:
            if value:
                flattened.append(value)
    return flattened


def _parse_extra_switches(values: Iterable[str]) -> tuple[List[str], List[str], List[str]]:
    config_args: List[str] = []
    build_args: List[str] = []
    install_args: List[str] = []

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
            if candidate in {"config", "build", "install"} and remainder:
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
        elif scope == "install":
            targets = [install_args]
        else:
            targets = [config_args, build_args, install_args]

        for part in parts:
            for target in targets:
                target.append(part)

    return config_args, build_args, install_args


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


def _parse_definition_args(values: Iterable[str]) -> dict[str, str]:
    definitions: dict[str, str] = {}
    for raw in values:
        if raw is None:
            continue
        text = raw.strip()
        if not text:
            continue
        if "=" not in text:
            raise ValueError(f"Definition '{text}' must be in NAME=VALUE format")
        name, value = text.split("=", 1)
        key = name.strip()
        if not key:
            raise ValueError("Definition name cannot be empty")
        definitions[key] = value.strip()
    return definitions


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


def _component_dir_argument(plan: BuildPlan) -> Path | None:
    component_dir = plan.project.component_dir
    if not component_dir:
        return None

    component_dir_arg: Path | None = None
    project_ctx: dict[str, object] | object
    if isinstance(plan.context, dict):
        project_ctx = plan.context.get("project", {})
    else:
        project_ctx = {}
    resolved_component: object | None = None
    if isinstance(project_ctx, dict):
        resolved_component = project_ctx.get("component_dir")

    if isinstance(resolved_component, str):
        resolved_path = Path(resolved_component)
        try:
            component_dir_arg = resolved_path.relative_to(plan.source_dir)
        except ValueError:
            component_dir_arg = resolved_path
    else:
        component_dir_arg = Path(component_dir)

    return component_dir_arg


def _parse_arguments(argv: Iterable[str]) -> Namespace:
    parser = ArgumentParser(prog="builder", description="Preset-driven build orchestrator")
    parser.add_argument(
        "-C",
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
    build_parser.add_argument("-p", "--preset", action="append", default=[], help="Preset name(s) to apply (comma-separated)")
    build_parser.add_argument("-b", "--branch", help="Git branch to use for the build")
    build_parser.add_argument("-B", "--build-type", help="Override build type (Debug/Release)")
    build_parser.add_argument("-G", "--generator", help="Override build system generator")
    build_parser.add_argument("-t", "--target", help="Build a specific target")
    build_parser.add_argument("--install", action="store_true", help="Install after build")
    build_parser.add_argument("-n", "--dry-run", action="store_true", help="Print commands without executing them")
    build_parser.add_argument("--show-vars", action="store_true", help="Display resolved variables before building")
    build_parser.add_argument("--no-switch-branch", action="store_true", help="Do not switch Git branches automatically")
    build_parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    build_parser.add_argument("-T", "--toolchain", help="Specify the toolchain to use")
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
        help="Extra arguments (use -Xconfig,<arg>, -Xbuild,<arg>, or -Xinstall,<arg>; omit scope for all phases)",
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
    build_parser.add_argument(
        "--extra-install-args",
        dest="extra_install_args",
        action="append",
        nargs="+",
        default=[],
        metavar="ARG",
        help="Additional arguments appended to install commands",
    )
    build_parser.add_argument(
        "-D",
        "--definition",
        dest="definitions",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Temporarily define a build variable (repeatable)",
    )

    validate_parser = subparsers.add_parser("validate", help="Validate configuration files")
    validate_parser.add_argument("project", nargs="?", help="Validate a single project by name")

    update_parser = subparsers.add_parser("update", help="Update Git repositories")
    update_parser.add_argument("project", nargs="?", help="Project to update; omit to update all")
    update_parser.add_argument("-b", "--branch", help="Branch to checkout during update")
    update_parser.add_argument("-s", "--submodule", choices=["default", "latest", "skip"], default="default", help="Submodule update strategy")
    update_parser.add_argument("-n", "--dry-run", action="store_true", help="Preview git commands without executing them")

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
    switch_config_args, switch_build_args, switch_install_args = _parse_extra_switches(getattr(args, "extra_switches", []))
    cli_extra_config_args = _flatten_arg_groups(getattr(args, "extra_config_args", []))
    cli_extra_build_args = _flatten_arg_groups(getattr(args, "extra_build_args", []))
    cli_extra_install_args = _flatten_arg_groups(getattr(args, "extra_install_args", []))
    extra_config_args = [*switch_config_args, *cli_extra_config_args]
    extra_build_args = [*switch_build_args, *cli_extra_build_args]
    extra_install_args = [*switch_install_args, *cli_extra_install_args]
    try:
        cli_definitions = _parse_definition_args(getattr(args, "definitions", []))
    except ValueError as exc:
        print(f"Error: {exc}")
        return 2
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
        extra_install_args=extra_install_args,
        toolchain=args.toolchain,
        install_dir=args.install_dir,
        operation=operation,
        definitions=cli_definitions,
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

        build_branch = plan.branch
        state = None
        plan_has_steps = bool(plan.steps)
        should_prepare_checkout = plan_has_steps and (
            not args.dry_run or plan.source_dir.exists()
        )
        if should_prepare_checkout:
            component_dir_arg = _component_dir_argument(plan)
            branch_override = options.branch
            root_target_branch = branch_override or build_branch
            component_branch_arg: str | None = None
            if component_dir_arg and not options.no_switch_branch:
                root_target_branch = plan.project.git.main_branch or root_target_branch
                component_branch_arg = branch_override or plan.project.git.component_branch or build_branch
            state = git_manager.prepare_checkout(
                repo_path=plan.source_dir,
                target_branch=root_target_branch,
                auto_stash=plan.project.git.auto_stash,
                no_switch_branch=options.no_switch_branch,
                environment=plan.git_environment,
                component_dir=component_dir_arg,
                component_branch=component_branch_arg,
                dry_run=args.dry_run,
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
                    dry_run=args.dry_run,
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
            extra_install_args=list(build_options.extra_install_args),
            toolchain=build_options.toolchain,
            install_dir=None,
            operation=build_options.operation,
            definitions=dict(build_options.definitions),
        )
        run_project(dep_options, show_vars=False)

    run_project(build_options, show_vars=args.show_vars)

    if args.dry_run and isinstance(runner, RecordingCommandRunner):
        _emit_dry_run_output(runner, workspace=workspace)
    return 0


def _handle_validate(args: Namespace, workspace: Path) -> int:
    store = _load_configuration_store(args, workspace)
    global_errors = validate_store_structure(store)
    if args.project:
        project_names = [args.project]
    else:
        project_names = sorted(store.list_projects())

    errors: List[tuple[str, str]] = []
    for message in global_errors:
        errors.append(("config", message))
    for project_name in project_names:
        try:
            validate_project(
                store,
                project_name,
                workspace=workspace,
            )
            validate_project_templates(
                store,
                project_name,
                workspace=workspace,
            )
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
        component_dir_arg = _component_dir_argument(plan)
        git_manager.update_repository(
            repo_path=source_dir,
            url=project.git.url,
            main_branch=project.git.main_branch,
            component_branch=project.git.component_branch,
            clone_script=plan.git_clone_script,
            update_script=plan.git_update_script,
            auto_stash=project.git.auto_stash,
            environment=plan.git_environment,
            dry_run=args.dry_run,
            component_dir=component_dir_arg,
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
        checkout_error: RuntimeError | None = None
        repo_ready = git_manager.is_repository(repo_path, environment=plan.git_environment)
        submodules: List[dict[str, str]] = []

        if repo_ready:
            component_dir_arg = _component_dir_argument(plan)
            branch_override = getattr(args, "branch", None)
            root_target_branch = branch_override or plan.branch
            component_branch_arg: str | None = None
            if component_dir_arg and not args.no_switch_branch:
                root_target_branch = plan.project.git.main_branch or root_target_branch
                component_branch_arg = branch_override or plan.project.git.component_branch or plan.branch

            try:
                state = git_manager.prepare_checkout(
                    repo_path=repo_path,
                    target_branch=root_target_branch,
                    auto_stash=plan.project.git.auto_stash,
                    no_switch_branch=args.no_switch_branch,
                    environment=plan.git_environment,
                    component_dir=component_dir_arg,
                    component_branch=component_branch_arg,
                )
            except RuntimeError as exc:
                checkout_error = exc

            try:
                branch, commit = git_manager.get_repository_state(repo_path, environment=plan.git_environment)
            except RuntimeError as exc:
                if checkout_error is None:
                    checkout_error = exc
            else:
                if repo_path.exists():
                    submodules = git_manager.list_submodules(repo_path, environment=plan.git_environment)

            if checkout_error is not None:
                print(f"Warning: Could not prepare repository '{name}': {checkout_error}")

            if state is not None:
                try:
                    git_manager.restore_checkout(
                        repo_path,
                        state,
                        environment=plan.git_environment,
                    )
                except RuntimeError as exc:
                    print(f"Warning: Could not restore repository '{name}': {exc}")
        else:
            branch, commit = git_manager.get_repository_state(repo_path, environment=plan.git_environment)

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

        if not repo_ready:
            print(f"Warning: Git repository not found at '{repo_path}' for project '{name}'")
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


def _collect_presets(values: List[str]) -> List[str]:
    presets: List[str] = []
    for value in values:
        if not value:
            continue
        presets.extend(part.strip() for part in value.split(",") if part.strip())
    return presets


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
