from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
import os
from unittest.mock import patch

from builder import cli
from builder.git_manager import GitWorkState


class ConfigDirectoryResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "config").mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_config_directories_order_and_expansion(self) -> None:
        env_rel = "extras"
        env_abs = (self.workspace / "env" / "absolute").resolve()
        env_abs.parent.mkdir(parents=True, exist_ok=True)

        original_env = os.environ.get("BUILDER_CONFIG_DIR")
        if original_env is None:
            self.addCleanup(os.environ.pop, "BUILDER_CONFIG_DIR", None)
        else:
            self.addCleanup(os.environ.__setitem__, "BUILDER_CONFIG_DIR", original_env)
        os.environ["BUILDER_CONFIG_DIR"] = os.pathsep.join([env_rel, str(env_abs)])

        cli_override = "overrides"
        extra_one = self.workspace / "extra-one"
        extra_two = self.workspace / "extra-two"

        result = cli._resolve_config_directories(
            self.workspace,
            [str(extra_one), os.pathsep.join([cli_override, str(extra_two)])],
        )

        expected = [
            self.workspace / "config",
            self.workspace / env_rel,
            env_abs,
            extra_one,
            self.workspace / cli_override,
            extra_two,
        ]

        self.assertEqual(result, expected)


class BuildCommandDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        config_dir = self.workspace / "config"
        projects_dir = config_dir / "projects"
        projects_dir.mkdir(parents=True)
        (config_dir / "config.toml").write_text(
            textwrap.dedent(
                """
                [global]
                default_build_type = "Release"
                default_operation = "auto"
                """
            )
        )
        (projects_dir / "demo.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "demo"
                source_dir = "{{builder.path}}/examples/demo"
                build_dir = "_build/{{user.branch}}_{{user.build_type}}"
                build_system = "cmake"

                [git]
                url = "https://example.com/demo.git"
                main_branch = "main"
                auto_stash = true

                [presets.dev]
                environment = { CC = "clang" }
                definitions = { CMAKE_BUILD_TYPE = "Debug" }
                """
            )
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_build_dry_run_outputs_formatted_commands(self) -> None:
        args = SimpleNamespace(
            project="demo",
            preset=["dev"],
            branch=None,
            build_type=None,
            generator=None,
            target=None,
            install=False,
            dry_run=True,
            show_vars=False,
            no_switch_branch=False,
            verbose=False,
            toolchain=None,
            install_dir=None,
            config_only=False,
            build_only=False,
            reconfig=False,
            extra_switches=[],
            extra_config_args=[],
            extra_build_args=[],
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._handle_build(args, self.workspace)
        output = buffer.getvalue()
        self.assertIn("[dry-run]", output)
        self.assertIn("Configure project", output)
        self.assertIn("cmake", output)
        # Build steps should include the resolved workspace path
        self.assertIn(str(self.workspace / "examples" / "demo"), output)

    def test_build_with_dependencies_runs_in_order(self) -> None:
        (self.workspace / "config" / "projects" / "lib.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "lib"
                source_dir = "{{builder.path}}/examples/lib"
                build_dir = "_build/lib"
                build_system = "cmake"

                [git]
                url = "https://example.com/lib.git"
                main_branch = "main"

                [presets.dev]
                environment = { CC = "clang" }
                """
            )
        )
        (self.workspace / "config" / "projects" / "demo.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "demo"
                source_dir = "{{builder.path}}/examples/demo"
                build_dir = "_build/{{user.branch}}_{{user.build_type}}"
                build_system = "cmake"

                [git]
                url = "https://example.com/demo.git"
                main_branch = "main"
                auto_stash = true

                [presets.dev]
                environment = { CC = "clang" }
                definitions = { CMAKE_BUILD_TYPE = "Debug" }

                [[dependencies]]
                name = "lib"
                presets = ["dev"]
                """
            )
        )

        args = SimpleNamespace(
            project="demo",
            preset=["dev"],
            branch=None,
            build_type=None,
            generator=None,
            target=None,
            install=False,
            dry_run=True,
            show_vars=False,
            no_switch_branch=False,
            verbose=False,
            toolchain=None,
            install_dir=None,
            config_only=False,
            build_only=False,
            reconfig=False,
            extra_switches=[],
            extra_config_args=[],
            extra_build_args=[],
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._handle_build(args, self.workspace)

        output = buffer.getvalue()
        dry_run_lines = [line for line in output.splitlines() if line.startswith("[dry-run]")]
        configure_lines = [line for line in dry_run_lines if "Configure project" in line]
        self.assertGreaterEqual(len(configure_lines), 2)
        self.assertIn("lib", configure_lines[0])
        self.assertIn("demo", configure_lines[1])
        recorded_commands = "\n".join(dry_run_lines)
        self.assertIn("lib", recorded_commands)

    def test_build_without_build_dir_prints_notice(self) -> None:
        (self.workspace / "config" / "projects" / "meta.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "meta"
                source_dir = "{{builder.path}}/meta"

                [git]
                url = "https://example.com/meta.git"
                main_branch = "main"
                """
            )
        )

        args = SimpleNamespace(
            project="meta",
            preset=[],
            branch=None,
            build_type=None,
            generator=None,
            target=None,
            install=False,
            dry_run=True,
            show_vars=False,
            no_switch_branch=False,
            verbose=False,
            toolchain=None,
            install_dir=None,
            config_only=False,
            build_only=False,
            reconfig=False,
            extra_switches=[],
            extra_config_args=[],
            extra_build_args=[],
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._handle_build(args, self.workspace)

        output = buffer.getvalue()
        self.assertIn("No build steps for project 'meta'", output)
        self.assertNotIn("[dry-run]", output)

    def test_build_dry_run_records_git_operations_when_repository_exists(self) -> None:
        source_dir = self.workspace / "examples" / "demo"
        (source_dir / ".git").mkdir(parents=True, exist_ok=True)

        args = SimpleNamespace(
            project="demo",
            preset=["dev"],
            branch=None,
            build_type=None,
            generator=None,
            target=None,
            install=False,
            dry_run=True,
            show_vars=False,
            no_switch_branch=False,
            verbose=False,
            toolchain=None,
            install_dir=None,
            config_only=False,
            build_only=False,
            reconfig=False,
            extra_switches=[],
            extra_config_args=[],
            extra_build_args=[],
        )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._handle_build(args, self.workspace)

        output = buffer.getvalue()
        dry_run_lines = [line for line in output.splitlines() if line.startswith("[dry-run]")]
        self.assertTrue(any("git switch" in line for line in dry_run_lines))
        self.assertTrue(any("git submodule update --recursive" in line for line in dry_run_lines))

    def test_build_passes_component_repo_to_git_manager(self) -> None:
        projects_dir = self.workspace / "config" / "projects"
        (projects_dir / "component.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "component"
                source_dir = "{{builder.path}}/examples/component"
                component_dir = "libs/core"
                build_dir = "_build/{{user.branch}}"
                build_system = "cmake"

                [git]
                url = "https://example.com/component.git"
                main_branch = "main"
                component_branch = "component/main"
                """
            )
        )

        repo_root = self.workspace / "examples" / "component"
        component_path = repo_root / "libs" / "core"
        (component_path / ".git").mkdir(parents=True, exist_ok=True)

        class RecordingGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                self.prepare_args: list[dict[str, object | None]] = []
                self.restore_calls: list[GitWorkState] = []
                RecordingGitManager.last_instance = self

            def prepare_checkout(
                self,
                *,
                repo_path: Path,
                target_branch: str,
                auto_stash: bool,
                no_switch_branch: bool,
                environment=None,
                component_dir=None,
                component_branch=None,
                dry_run: bool = False,
            ) -> GitWorkState:
                self.prepare_args.append(
                    {
                        "repo_path": repo_path,
                        "target_branch": target_branch,
                        "component_dir": component_dir,
                        "component_branch": component_branch,
                    }
                )
                return GitWorkState(
                    branch="main",
                    stash_applied=False,
                    component_branch="component/main",
                    component_path=repo_root / "libs" / "core",
                    component_stash_applied=True,
                )

            def restore_checkout(
                self,
                repo_path: Path,
                state: GitWorkState,
                *,
                environment=None,
                dry_run: bool = False,
            ) -> None:
                self.restore_calls.append(state)

        args = SimpleNamespace(
            project="component",
            preset=[],
            branch=None,
            build_type=None,
            generator=None,
            target=None,
            install=False,
            dry_run=True,
            show_vars=False,
            no_switch_branch=False,
            verbose=False,
            toolchain=None,
            install_dir=None,
            config_only=False,
            build_only=False,
            reconfig=False,
            extra_switches=[],
            extra_config_args=[],
            extra_build_args=[],
        )

        buffer = io.StringIO()
        with patch("builder.cli.GitManager", RecordingGitManager):
            with redirect_stdout(buffer):
                cli._handle_build(args, self.workspace)

        manager = RecordingGitManager.last_instance
        self.assertIsNotNone(manager)
        assert manager is not None  # for type checkers
        self.assertTrue(manager.prepare_args)
        call = manager.prepare_args[0]
        self.assertEqual(call.get("repo_path"), repo_root)
        self.assertEqual(call.get("target_branch"), "main")
        self.assertEqual(call.get("component_dir"), Path("libs/core"))
        self.assertEqual(call.get("component_branch"), "component/main")
        self.assertTrue(manager.restore_calls)

    def test_build_branch_override_applies_to_component_repo(self) -> None:
        projects_dir = self.workspace / "config" / "projects"
        (projects_dir / "component.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "component"
                source_dir = "{{builder.path}}/examples/component"
                component_dir = "libs/core"
                build_dir = "_build/{{user.branch}}"
                build_system = "cmake"

                [git]
                url = "https://example.com/component.git"
                main_branch = "main"
                component_branch = "component/main"
                """
            )
        )

        repo_root = self.workspace / "examples" / "component"
        component_path = repo_root / "libs" / "core"
        (component_path / ".git").mkdir(parents=True, exist_ok=True)

        class RecordingGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                self.prepare_args: list[dict[str, object | None]] = []
                RecordingGitManager.last_instance = self

            def prepare_checkout(
                self,
                *,
                repo_path: Path,
                target_branch: str,
                auto_stash: bool,
                no_switch_branch: bool,
                environment=None,
                component_dir=None,
                component_branch=None,
                dry_run: bool = False,
            ) -> GitWorkState:
                self.prepare_args.append(
                    {
                        "repo_path": repo_path,
                        "target_branch": target_branch,
                        "component_dir": component_dir,
                        "component_branch": component_branch,
                    }
                )
                return GitWorkState(
                    branch="main",
                    stash_applied=False,
                    component_branch="component/main",
                    component_path=component_path,
                    component_stash_applied=False,
                )

            def restore_checkout(
                self,
                repo_path: Path,
                state: GitWorkState,
                *,
                environment=None,
                dry_run: bool = False,
            ) -> None:
                return None

        args = SimpleNamespace(
            project="component",
            preset=[],
            branch="component/dev",
            build_type=None,
            generator=None,
            target=None,
            install=False,
            dry_run=True,
            show_vars=False,
            no_switch_branch=False,
            verbose=False,
            toolchain=None,
            install_dir=None,
            config_only=False,
            build_only=False,
            reconfig=False,
            extra_switches=[],
            extra_config_args=[],
            extra_build_args=[],
        )

        buffer = io.StringIO()
        with patch("builder.cli.GitManager", RecordingGitManager):
            with redirect_stdout(buffer):
                cli._handle_build(args, self.workspace)

        manager = RecordingGitManager.last_instance
        self.assertIsNotNone(manager)
        assert manager is not None
        self.assertTrue(manager.prepare_args)
        call = manager.prepare_args[0]
        self.assertEqual(call.get("target_branch"), "main")
        self.assertEqual(call.get("component_branch"), "component/dev")

    def test_build_branch_override_for_single_repo_targets_root(self) -> None:
        projects_dir = self.workspace / "config" / "projects"
        (projects_dir / "single.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "single"
                source_dir = "{{builder.path}}/examples/single"
                build_dir = "_build"
                build_system = "cmake"

                [git]
                url = "https://example.com/single.git"
                main_branch = "main"
                """
            )
        )

        repo_root = self.workspace / "examples" / "single"
        (repo_root / ".git").mkdir(parents=True, exist_ok=True)

        class RecordingGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                self.prepare_args: list[dict[str, object | None]] = []
                RecordingGitManager.last_instance = self

            def prepare_checkout(
                self,
                *,
                repo_path: Path,
                target_branch: str,
                auto_stash: bool,
                no_switch_branch: bool,
                environment=None,
                component_dir=None,
                component_branch=None,
                dry_run: bool = False,
            ) -> GitWorkState:
                self.prepare_args.append(
                    {
                        "repo_path": repo_path,
                        "target_branch": target_branch,
                        "component_dir": component_dir,
                        "component_branch": component_branch,
                    }
                )
                return GitWorkState(branch=target_branch, stash_applied=False)

            def restore_checkout(
                self,
                repo_path: Path,
                state: GitWorkState,
                *,
                environment=None,
                dry_run: bool = False,
            ) -> None:
                return None

        args = SimpleNamespace(
            project="single",
            preset=[],
            branch="release/1.2",
            build_type=None,
            generator=None,
            target=None,
            install=False,
            dry_run=True,
            show_vars=False,
            no_switch_branch=False,
            verbose=False,
            toolchain=None,
            install_dir=None,
            config_only=False,
            build_only=False,
            reconfig=False,
            extra_switches=[],
            extra_config_args=[],
            extra_build_args=[],
        )

        buffer = io.StringIO()
        with patch("builder.cli.GitManager", RecordingGitManager):
            with redirect_stdout(buffer):
                cli._handle_build(args, self.workspace)

        manager = RecordingGitManager.last_instance
        self.assertIsNotNone(manager)
        assert manager is not None
        self.assertTrue(manager.prepare_args)
        call = manager.prepare_args[0]
        self.assertEqual(call.get("target_branch"), "release/1.2")
        self.assertIsNone(call.get("component_branch"))


class ExtraSwitchParsingTests(unittest.TestCase):
    def test_parse_scoped_switches(self) -> None:
        config_args, build_args = cli._parse_extra_switches(
            [
                "config,-DCONFIG_ONLY",
                "build,--build-only",
                "--shared-flag",
                "build,--multi-a,--multi-b",
            ]
        )
        self.assertEqual(
            config_args,
            ["-DCONFIG_ONLY", "--shared-flag"],
        )
        self.assertEqual(
            build_args,
            ["--build-only", "--shared-flag", "--multi-a", "--multi-b"],
        )

    def test_flatten_arg_groups(self) -> None:
        flattened = cli._flatten_arg_groups([["-DA"], ["-DB", "-DC"]])
        self.assertEqual(flattened, ["-DA", "-DB", "-DC"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
