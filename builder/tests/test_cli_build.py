from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
import os

from builder import cli


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
