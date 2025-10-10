from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

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
                generator = "Ninja"

                [git]
                url = "https://example.com/demo.git"
                main_branch = "main"
                auto_stash = true

                [presets.dev]
                environment = { CC = "clang" }
                definitions = { CMAKE_BUILD_TYPE = "Debug", DEMO_FOO = true, DEMO_NAME = "demo-name", DEMO_THREADS = 4 }

                [presets."configs.debug".environment]
                BUILD_MODE = "debug"

                [presets."configs.release".environment]
                BUILD_MODE = "release"
                """
            )
        )
        (projects_dir / "meson-app.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "meson-app"
                source_dir = "{{builder.path}}/examples/meson-app"
                build_dir = "_build"
                build_system = "meson"

                [git]
                url = "https://example.com/meson-app.git"
                main_branch = "main"

                [presets.dev]
                extra_args = ["--default-library=static"]

                [presets.dev.environment]
                CC = "clang"

                [presets.dev.definitions]
                opt = "value"
                """
            )
        )
        (projects_dir / "bazel-app.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "bazel-app"
                source_dir = "{{builder.path}}/examples/bazel-app"
                build_dir = "unused"
                build_system = "bazel"

                [git]
                url = "https://example.com/bazel-app.git"
                main_branch = "main"

                [presets.dev.definitions]
                TARGET = "//app:all"
                BUILD_OPTS = "--k=1"
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
        with patch("builder.build.shutil.which", side_effect=lambda exe: None if exe != "ccache" else "/usr/bin/ccache"):
            plan = self.engine.plan(options)
        self.assertIn("_build/feature-x_Release", plan.build_dir.as_posix())
        commands = [step.command for step in plan.steps]
        self.assertTrue(any(cmd[0] == "cmake" and "--build" in cmd for cmd in commands))
        self.assertIn("configs.release", plan.presets)
        self.assertEqual(plan.steps[0].env.get("BUILD_MODE"), "release")
        self.assertEqual(plan.steps[0].env.get("CC"), "ccache clang")
        self.assertEqual(plan.steps[0].env.get("CXX"), "ccache clang++")
        self.assertEqual(plan.steps[0].env.get("CC_LD"), "ld")
        self.assertEqual(plan.steps[0].env.get("CXX_LD"), "ld")
        self.assertEqual(plan.steps[0].env.get("CLANG_FORCE_COLOR_DIAGNOSTICS"), "1")
        configure_cmd = plan.steps[0].command
        configure_str = " ".join(configure_cmd)
        self.assertIn("-G", configure_cmd)
        ninja_index = configure_cmd.index("-G") + 1
        self.assertEqual(configure_cmd[ninja_index], "Ninja")
        self.assertIn("CMAKE_EXPORT_COMPILE_COMMANDS:BOOL=ON", configure_str)
        self.assertIn("DEMO_FOO:BOOL=ON", configure_str)
        self.assertIn("DEMO_NAME:STRING=demo-name", configure_str)
        self.assertIn("DEMO_THREADS:NUMBER=4", configure_str)

    def test_cli_build_type_overrides_presets(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            build_type="Release",
            operation=BuildMode.AUTO,
        )
        with patch("builder.build.shutil.which", side_effect=lambda exe: None if exe != "ccache" else "/usr/bin/ccache"):
            plan = self.engine.plan(options)
        configure_cmd = plan.steps[0].command
        configure_str = " ".join(configure_cmd)
        self.assertIn("CMAKE_BUILD_TYPE:STRING=Release", configure_str)
        self.assertNotIn("CMAKE_BUILD_TYPE:STRING=Debug", configure_str)

    def test_multi_config_adds_debug_and_release_presets(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            generator="Ninja Multi-Config",
            operation=BuildMode.AUTO,
        )
        with patch("builder.build.shutil.which", side_effect=lambda exe: None if exe != "ccache" else "/usr/bin/ccache"):
            plan = self.engine.plan(options)
        self.assertIn("configs.debug", plan.presets)
        self.assertIn("configs.release", plan.presets)
        # Release preset is applied last, so environment reflects release
        self.assertEqual(plan.steps[0].env.get("BUILD_MODE"), "release")

    def test_build_only_requires_existing_build_dir(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            operation=BuildMode.BUILD_ONLY,
        )
        with self.assertRaises(ValueError):
            self.engine.plan(options)

    def test_explicit_toolchain_sets_linker(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            toolchain="gcc",
        )
        def fake_which(exe: str) -> str | None:
            return {
                "mold": None,
                "lld": None,
                "gold": "/usr/bin/gold",
                "ccache": "/usr/bin/ccache",
            }.get(exe)

        with patch("builder.build.shutil.which", side_effect=fake_which):
            plan = self.engine.plan(options)
        configure = plan.steps[0].command
        self.assertIn("CMAKE_LINKER:STRING=gold", " ".join(configure))
        self.assertEqual(plan.steps[0].env.get("CC"), "ccache gcc")
        self.assertEqual(plan.steps[0].env.get("CXX"), "ccache g++")
        self.assertEqual(plan.steps[0].env.get("CC_LD"), "gold")
        self.assertEqual(plan.steps[0].env.get("CXX_LD"), "gold")
        self.assertEqual(plan.steps[0].env.get("GCC_COLORS"), "auto")
        configure_str = " ".join(configure)
        self.assertIn("CMAKE_C_COMPILER:STRING=gcc", configure_str)
        self.assertIn("CMAKE_C_COMPILER_LAUNCHER:STRING=ccache", configure_str)
        self.assertIn("CMAKE_EXPORT_COMPILE_COMMANDS:BOOL=ON", configure_str)

    def test_toolchain_prefers_mold_then_lld(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            toolchain="clang",
        )

        def fake_which(exe: str) -> str | None:
            return {
                "mold": "/usr/bin/mold",
                "lld": "/usr/bin/lld",
                "gold": None,
                "ccache": "/usr/bin/ccache",
            }.get(exe)

        with patch("builder.build.shutil.which", side_effect=fake_which):
            plan = self.engine.plan(options)
        configure = plan.steps[0].command
        self.assertIn("CMAKE_LINKER:STRING=mold", " ".join(configure))
        self.assertEqual(plan.steps[0].env.get("CC_LD"), "mold")
        self.assertEqual(plan.steps[0].env.get("CXX_LD"), "mold")
        self.assertEqual(plan.steps[0].env.get("CLANG_FORCE_COLOR_DIAGNOSTICS"), "1")
        configure_str = " ".join(configure)
        self.assertIn("CMAKE_C_COMPILER:STRING=clang", configure_str)
        self.assertIn("CMAKE_C_COMPILER_LAUNCHER:STRING=ccache", configure_str)
        self.assertIn("CMAKE_EXPORT_COMPILE_COMMANDS:BOOL=ON", configure_str)

        def fake_which_no_mold(exe: str) -> str | None:
            return {
                "mold": None,
                "lld": "/usr/bin/lld",
                "gold": None,
                "ccache": "/usr/bin/ccache",
            }.get(exe)

        with patch("builder.build.shutil.which", side_effect=fake_which_no_mold):
            plan = self.engine.plan(options)
        configure = plan.steps[0].command
        self.assertIn("CMAKE_LINKER:STRING=lld", " ".join(configure))
        self.assertEqual(plan.steps[0].env.get("CC_LD"), "lld")
        self.assertEqual(plan.steps[0].env.get("CXX_LD"), "lld")
        self.assertEqual(plan.steps[0].env.get("CLANG_FORCE_COLOR_DIAGNOSTICS"), "1")

    def test_toolchain_compatibility(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            toolchain="rustc",
        )
        with self.assertRaises(ValueError):
            self.engine.plan(options)

    def test_meson_plan(self) -> None:
        options = BuildOptions(
            project_name="meson-app",
            presets=["dev"],
            operation=BuildMode.AUTO,
        )
        plan = self.engine.plan(options)
        self.assertEqual(plan.project.build_system, "meson")
        self.assertEqual(len(plan.steps), 2)
        configure_cmd = plan.steps[0].command
        build_cmd = plan.steps[1].command
        self.assertEqual(configure_cmd[:3], ["meson", "setup", str(plan.build_dir)])
        self.assertIn("--opt=value", configure_cmd)
        self.assertIn("--default-library=static", configure_cmd)
        self.assertIn("clang", plan.steps[0].env.get("CC", ""))
        self.assertEqual(build_cmd[:3], ["meson", "compile", "-C"])

    def test_bazel_plan(self) -> None:
        options = BuildOptions(
            project_name="bazel-app",
            presets=["dev"],
            target="//app:all",
            operation=BuildMode.AUTO,
        )
        plan = self.engine.plan(options)
        self.assertEqual(plan.project.build_system, "bazel")
        self.assertEqual(len(plan.steps), 1)
        cmd = plan.steps[0].command
        self.assertEqual(cmd[0], "bazel")
        self.assertIn("//app:all", cmd)
        self.assertIn("--k=1", cmd)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
