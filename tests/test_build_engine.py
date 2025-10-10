from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import unittest

from builder.build import BuildEngine, BuildMode, BuildOptions
from builder.command_runner import RecordingCommandRunner
from builder.config_loader import ConfigurationStore


class BuildEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        config_dir = root / "config"
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
        self.workspace = root
        self.store = ConfigurationStore.from_directory(self.workspace)
        self.runner = RecordingCommandRunner()
        self.engine = BuildEngine(store=self.store, command_runner=self.runner, workspace=self.workspace)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_plan_generates_cmake_commands(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            branch="feature-x",
            operation=BuildMode.AUTO,
        )
        plan = self.engine.plan(options)
        self.assertIn("_build/feature-x_Release", plan.build_dir.as_posix())
        commands = [step.command for step in plan.steps]
        self.assertTrue(any(cmd[0] == "cmake" and "--build" in cmd for cmd in commands))

    def test_build_only_requires_existing_build_dir(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            operation=BuildMode.BUILD_ONLY,
        )
        with self.assertRaises(ValueError):
            self.engine.plan(options)

    def test_toolchain_compatibility(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            toolchain="rustc",
        )
        with self.assertRaises(ValueError):
            self.engine.plan(options)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
