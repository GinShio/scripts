from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from core.command_runner import RecordingCommandRunner

from builder.build import BuildEngine, BuildMode, BuildOptions
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
        (config_dir / "toolchains.toml").write_text(
            textwrap.dedent(
                """
                [toolchains.clang]
                launcher = "ccache"

                [toolchains.gcc]
                launcher = "ccache"
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
                extra_config_args = ["-DUSE_PRESET_CC={{preset.environment.CC}}"]
                toolchain = "clang"

                [git]
                url = "https://example.com/demo.git"
                main_branch = "main"
                auto_stash = true

                [project.environment]
                TOOLS_ROOT = "{{builder.path}}/env/tools"
                BIN_DIR = "{{project.environment.TOOLS_ROOT}}/bin"
                CUSTOM_PATH = "{{env.PATH}}:{{project.environment.BIN_DIR}}"

                [git.environment]
                BOOTSTRAP_ROOT = "{{project.environment.TOOLS_ROOT}}"

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
                toolchain = "clang"

                [git]
                url = "https://example.com/meson-app.git"
                main_branch = "main"

                [presets.dev]
                extra_config_args = ["--default-library=static"]

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
                toolchain = "clang"

                [git]
                url = "https://example.com/bazel-app.git"
                main_branch = "main"

                [presets.dev.definitions]
                TARGET = "//app:all"
                BUILD_OPTS = "--k=1"
                """
            )
        )
        (projects_dir / "cargo-app.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "cargo-app"
                source_dir = "{{builder.path}}/examples/cargo-app"
                build_dir = "_build/{{user.branch}}"
                build_system = "cargo"
                toolchain = "rustc"

                [git]
                url = "https://example.com/cargo-app.git"
                main_branch = "main"
                """
            )
        )
        (projects_dir / "branch-override.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "branch-override"
                source_dir = "{{builder.path}}/examples/branch-override"
                component_dir = "components/a"
                build_dir = "_build/{{user.branch}}"
                build_system = "cmake"
                toolchain = "clang"

                [git]
                url = "https://example.com/branch_override.git"
                main_branch = "monorepo/main"
                component_branch = "component/main"
                """
            )
        )
        (projects_dir / "mono-root.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "mono-root"
                source_dir = "{{builder.path}}/examples/mono-root"
                component_dir = "components/library"
                build_dir = "_build/{{user.branch}}"
                build_system = "cmake"
                generator = "Ninja"
                build_at_root = true
                source_at_root = true
                toolchain = "clang"

                [git]
                url = "https://example.com/mono-root.git"
                main_branch = "main"
                """
            )
        )
        (projects_dir / "component-source-root.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "component-source-root"
                source_dir = "{{builder.path}}/examples/component-source-root"
                component_dir = "libs/core"
                build_dir = "_build/{{user.branch}}"
                build_system = "cmake"
                generator = "Ninja"
                build_at_root = true
                source_at_root = false
                toolchain = "clang"

                [git]
                url = "https://example.com/component-source-root.git"
                main_branch = "main"
                """
            )
        )
        (projects_dir / "meta.toml").write_text(
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
        self.workspace = root
        self.store = ConfigurationStore.from_directory(self.workspace)
        self.runner = RecordingCommandRunner()
        self.engine = BuildEngine(
            store=self.store, command_runner=self.runner, workspace=self.workspace
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_plan_generates_cmake_commands(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            branch="feature-x",
            operation=BuildMode.AUTO,
        )
        with patch(
            "builder.build.shutil.which",
            side_effect=lambda exe: None if exe != "ccache" else "/usr/bin/ccache",
        ):
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
        self.assertEqual(plan.steps[0].env.get("CFLAGS"), "-fcolor-diagnostics")
        self.assertEqual(plan.steps[0].env.get("CXXFLAGS"), "-fcolor-diagnostics")
        expected_tools_root = str(self.workspace / "env" / "tools")
        expected_bin_dir = str(self.workspace / "env" / "tools" / "bin")
        self.assertEqual(plan.environment.get("TOOLS_ROOT"), expected_tools_root)
        self.assertEqual(plan.environment.get("BIN_DIR"), expected_bin_dir)
        self.assertTrue(
            plan.environment.get("CUSTOM_PATH", "").endswith(f":{expected_bin_dir}"),
            msg="CUSTOM_PATH should append the resolved BIN_DIR",
        )
        self.assertEqual(
            plan.git_environment.get("BOOTSTRAP_ROOT"), expected_tools_root
        )
        configure_cmd = plan.steps[0].command
        configure_str = " ".join(configure_cmd)
        self.assertIn("-G", configure_cmd)
        ninja_index = configure_cmd.index("-G") + 1
        self.assertEqual(configure_cmd[ninja_index], "Ninja")
        self.assertIn("CMAKE_EXPORT_COMPILE_COMMANDS:BOOL=ON", configure_str)
        self.assertIn("CMAKE_C_FLAGS:STRING=-fcolor-diagnostics", configure_str)
        self.assertIn("CMAKE_CXX_FLAGS:STRING=-fcolor-diagnostics", configure_str)
        self.assertIn("DEMO_FOO:BOOL=ON", configure_str)
        self.assertIn("DEMO_NAME:STRING=demo-name", configure_str)
        self.assertIn("DEMO_THREADS:NUMBER=4", configure_str)
        self.assertIn("-DUSE_PRESET_CC=clang", configure_str)

    def test_context_exposes_toolchain_and_linker(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            operation=BuildMode.AUTO,
        )
        with patch("builder.build.shutil.which", return_value=None):
            plan = self.engine.plan(options)
        user_context = plan.context["user"]
        self.assertEqual(user_context.get("toolchain"), "clang")
        self.assertEqual(user_context.get("linker"), "ld")

    def test_plan_generates_cargo_commands(self) -> None:
        options = BuildOptions(
            project_name="cargo-app",
            presets=[],
            operation=BuildMode.AUTO,
        )
        plan = self.engine.plan(options)

        self.assertIsNotNone(plan.build_dir)
        expected_target_dir = str(plan.build_dir)
        self.assertEqual(plan.environment.get("CARGO_TARGET_DIR"), expected_target_dir)
        self.assertEqual(plan.context["user"].get("toolchain"), "rustc")

        self.assertEqual(len(plan.steps), 1)
        build_cmd = plan.steps[0].command
        self.assertEqual(build_cmd[0], "cargo")
        self.assertEqual(build_cmd[1], "build")
        self.assertIn("--target-dir", build_cmd)
        self.assertIn(expected_target_dir, build_cmd)
        self.assertIn("--release", build_cmd)

    def test_cargo_config_only_runs_fetch(self) -> None:
        options = BuildOptions(
            project_name="cargo-app",
            presets=[],
            operation=BuildMode.CONFIG_ONLY,
        )
        plan = self.engine.plan(options)

        self.assertEqual(len(plan.steps), 1)
        fetch_cmd = plan.steps[0].command
        self.assertEqual(fetch_cmd[0], "cargo")
        self.assertEqual(fetch_cmd[1], "fetch")
        self.assertEqual(plan.steps[0].env.get("CARGO_TARGET_DIR"), str(plan.build_dir))

    def test_branch_placeholder_sanitizes_slashes(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            branch="feature/awesome",
            operation=BuildMode.AUTO,
        )
        with patch("builder.build.shutil.which", return_value=None):
            plan = self.engine.plan(options)
        user_context = plan.context["user"]
        self.assertEqual(user_context.get("branch"), "feature_awesome")
        self.assertEqual(user_context.get("branch_slug"), "feature_awesome")
        self.assertEqual(user_context.get("branch_raw"), "feature/awesome")
        self.assertEqual(plan.branch, "feature/awesome")
        self.assertEqual(plan.branch_slug, "feature_awesome")
        self.assertIn("feature_awesome", str(plan.build_dir))

    def test_reconfig_dry_run_preserves_cmake_build_dir(self) -> None:
        demo_source = self.workspace / "examples" / "demo"
        build_dir = demo_source / "_build" / "main_Release"
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "CMakeCache.txt").write_text("")

        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            operation=BuildMode.RECONFIG,
            dry_run=True,
        )

        with patch(
            "builder.build.shutil.which",
            side_effect=lambda exe: None if exe != "ccache" else "/usr/bin/ccache",
        ):
            plan = self.engine.plan(options)

        self.assertTrue(build_dir.exists())
        self.assertTrue((build_dir / "CMakeCache.txt").exists())
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].description, "Configure project")

    def test_reconfig_dry_run_preserves_meson_build_dir(self) -> None:
        meson_source = self.workspace / "examples" / "meson-app"
        build_dir = meson_source / "_build"
        (build_dir / "meson-private").mkdir(parents=True, exist_ok=True)
        (build_dir / "meson-private" / "coredata.dat").write_text("")

        options = BuildOptions(
            project_name="meson-app",
            presets=["dev"],
            operation=BuildMode.RECONFIG,
            dry_run=True,
        )

        with patch("builder.build.shutil.which", return_value=None):
            plan = self.engine.plan(options)

        self.assertTrue(build_dir.exists())
        self.assertTrue((build_dir / "meson-private" / "coredata.dat").exists())
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].description, "Configure project")

    def test_build_at_root_true_uses_project_source_dir(self) -> None:
        options = BuildOptions(
            project_name="mono-root",
            presets=[],
            branch="dev/one",
            operation=BuildMode.AUTO,
        )
        with patch("builder.build.shutil.which", return_value=None):
            plan = self.engine.plan(options)

        expected_source = self.workspace / "examples" / "mono-root"
        component_subdir = expected_source / "components" / "library"

        self.assertEqual(plan.source_dir, expected_source)
        self.assertNotEqual(plan.source_dir, component_subdir)
        self.assertEqual(plan.configure_source_dir, expected_source)
        self.assertEqual(plan.branch, "dev/one")
        self.assertEqual(plan.branch_slug, "dev_one")
        self.assertTrue(plan.steps)
        configure_step = plan.steps[0]
        self.assertIn(str(expected_source), configure_step.command)
        self.assertEqual(configure_step.cwd, expected_source)

    def test_component_branch_overrides_default_branch(self) -> None:
        options = BuildOptions(
            project_name="branch-override",
            presets=[],
            operation=BuildMode.AUTO,
        )
        with patch("builder.build.shutil.which", return_value=None):
            plan = self.engine.plan(options)

        self.assertEqual(plan.branch, "component/main")
        self.assertEqual(plan.branch_slug, "component_main")
        user_context = plan.context["user"]
        self.assertEqual(user_context.get("branch_raw"), "component/main")
        self.assertEqual(user_context.get("branch"), "component_main")

    def test_source_at_root_false_uses_component_directory(self) -> None:
        options = BuildOptions(
            project_name="component-source-root",
            presets=[],
            operation=BuildMode.AUTO,
        )
        with patch("builder.build.shutil.which", return_value=None):
            plan = self.engine.plan(options)

        repo_root = self.workspace / "examples" / "component-source-root"
        expected_source = repo_root / "libs" / "core"
        self.assertEqual(plan.source_dir, repo_root)
        self.assertEqual(plan.configure_source_dir, expected_source)
        self.assertTrue(plan.steps)
        configure_step = plan.steps[0]
        self.assertIn(str(expected_source), configure_step.command)
        self.assertEqual(configure_step.cwd, expected_source)

    def test_context_includes_preset_environment_and_definitions(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            operation=BuildMode.AUTO,
        )
        with patch("builder.build.shutil.which", return_value=None):
            plan = self.engine.plan(options)

        preset_ctx = plan.context.get("preset")
        self.assertIsNotNone(preset_ctx)
        self.assertEqual(preset_ctx.get("environment", {}).get("CC"), "clang")
        self.assertTrue(preset_ctx.get("definitions", {}).get("DEMO_FOO"))

    def test_auto_skips_cmake_config_when_already_configured(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            operation=BuildMode.AUTO,
        )
        cmake_source = self.workspace / "examples" / "demo"
        cmake_build = cmake_source / "_build" / "main_Release"
        cmake_build.mkdir(parents=True, exist_ok=True)
        (cmake_build / "CMakeCache.txt").write_text("# configured")

        plan = self.engine.plan(options)

        descriptions = [step.description for step in plan.steps]
        self.assertIn("Build project", descriptions)
        self.assertNotIn("Configure project", descriptions)

    def test_cmake_configures_install_prefix(self) -> None:
        install_dir = Path(self.workspace) / "install-root"
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            operation=BuildMode.AUTO,
            install=True,
            install_dir=str(install_dir),
        )
        with patch(
            "builder.build.shutil.which",
            side_effect=lambda exe: None if exe != "ccache" else "/usr/bin/ccache",
        ):
            plan = self.engine.plan(options)

        configure_cmd = plan.steps[0].command
        build_cmd = plan.steps[1].command
        install_cmd = plan.steps[2].command

        configure_str = " ".join(configure_cmd)
        self.assertIn("CMAKE_INSTALL_PREFIX:STRING=" + str(install_dir), configure_str)
        self.assertEqual(build_cmd[:3], ["cmake", "--build", str(plan.build_dir)])
        self.assertEqual(install_cmd, ["cmake", "--install", str(plan.build_dir)])

    def test_cmake_multi_config_installs_with_config_flag(self) -> None:
        install_dir = Path(self.workspace) / "install-root"
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            generator="Ninja Multi-Config",
            operation=BuildMode.AUTO,
            install=True,
            install_dir=str(install_dir),
        )
        with patch(
            "builder.build.shutil.which",
            side_effect=lambda exe: None if exe != "ccache" else "/usr/bin/ccache",
        ):
            plan = self.engine.plan(options)

        install_cmd = plan.steps[-1].command
        self.assertEqual(
            install_cmd,
            ["cmake", "--install", str(plan.build_dir), "--config", "Release"],
        )

    def test_meson_install_omits_config_flag(self) -> None:
        install_dir = Path(self.workspace) / "meson-install"
        options = BuildOptions(
            project_name="meson-app",
            presets=["dev"],
            operation=BuildMode.AUTO,
            install=True,
            install_dir=str(install_dir),
        )
        with patch("builder.build.shutil.which", return_value=None):
            plan = self.engine.plan(options)

        install_cmd = plan.steps[-1].command
        self.assertEqual(install_cmd, ["meson", "install", "-C", str(plan.build_dir)])

    def test_cli_build_type_overrides_presets(self) -> None:
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            build_type="Release",
            operation=BuildMode.AUTO,
        )
        with patch(
            "builder.build.shutil.which",
            side_effect=lambda exe: None if exe != "ccache" else "/usr/bin/ccache",
        ):
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
        with patch(
            "builder.build.shutil.which",
            side_effect=lambda exe: None if exe != "ccache" else "/usr/bin/ccache",
        ):
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
        self.assertEqual(plan.steps[0].env.get("CFLAGS"), "-fdiagnostics-color=always")
        self.assertEqual(
            plan.steps[0].env.get("CXXFLAGS"), "-fdiagnostics-color=always"
        )
        configure_str = " ".join(configure)
        self.assertIn("CMAKE_C_COMPILER:STRING=gcc", configure_str)
        self.assertIn("CMAKE_C_COMPILER_LAUNCHER:STRING=ccache", configure_str)
        self.assertIn("CMAKE_EXPORT_COMPILE_COMMANDS:BOOL=ON", configure_str)
        self.assertIn("CMAKE_C_FLAGS:STRING=-fdiagnostics-color=always", configure_str)
        self.assertIn(
            "CMAKE_CXX_FLAGS:STRING=-fdiagnostics-color=always", configure_str
        )

    def test_toolchain_config_overrides_builtins(self) -> None:
        toolchains_path = self.workspace / "config" / "toolchains.toml"
        toolchains_path.write_text(
            textwrap.dedent(
                """
                [toolchains.clang]
                launcher = "ccache"

                [toolchains.clang.environment]
                CC = "clang-17"
                CXX = "clang++-17"
                AR = "llvm-ar-17"

                [toolchains.clang.build_systems.cmake.definitions]
                CMAKE_AR = "llvm-ar-17"
                """
            )
        )

        store = ConfigurationStore.from_directory(self.workspace)
        engine = BuildEngine(
            store=store, command_runner=self.runner, workspace=self.workspace
        )
        options = BuildOptions(
            project_name="demo",
            presets=["dev"],
            operation=BuildMode.AUTO,
        )

        def fake_which(exe: str) -> str | None:
            return {
                "ccache": "/usr/bin/ccache",
            }.get(exe)

        with patch("builder.build.shutil.which", side_effect=fake_which):
            plan = engine.plan(options)

        self.assertEqual(plan.environment.get("AR"), "llvm-ar-17")
        configure_cmd = " ".join(plan.steps[0].command)
        self.assertIn("CMAKE_AR:STRING=llvm-ar-17", configure_cmd)
        self.assertEqual(plan.steps[0].env.get("CC"), "ccache clang-17")
        self.assertEqual(plan.steps[0].env.get("CXX"), "ccache clang++-17")

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
        self.assertEqual(plan.steps[0].env.get("CFLAGS"), "-fcolor-diagnostics")
        self.assertEqual(plan.steps[0].env.get("CXXFLAGS"), "-fcolor-diagnostics")
        configure_str = " ".join(configure)
        self.assertIn("CMAKE_C_COMPILER:STRING=clang", configure_str)
        self.assertIn("CMAKE_C_COMPILER_LAUNCHER:STRING=ccache", configure_str)
        self.assertIn("CMAKE_EXPORT_COMPILE_COMMANDS:BOOL=ON", configure_str)
        self.assertIn("CMAKE_C_FLAGS:STRING=-fcolor-diagnostics", configure_str)
        self.assertIn("CMAKE_CXX_FLAGS:STRING=-fcolor-diagnostics", configure_str)

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
        self.assertEqual(plan.steps[0].env.get("CFLAGS"), "-fcolor-diagnostics")
        self.assertEqual(plan.steps[0].env.get("CXXFLAGS"), "-fcolor-diagnostics")

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
        self.assertIn("-Dopt=value", configure_cmd)
        self.assertIn("--default-library=static", configure_cmd)
        self.assertIn("clang", plan.steps[0].env.get("CC", ""))
        self.assertEqual(plan.steps[0].env.get("CFLAGS"), "-fcolor-diagnostics")
        self.assertEqual(plan.steps[0].env.get("CXXFLAGS"), "-fcolor-diagnostics")
        self.assertEqual(build_cmd[:3], ["meson", "compile", "-C"])

    def test_auto_skips_meson_config_when_already_configured(self) -> None:
        options = BuildOptions(
            project_name="meson-app",
            presets=["dev"],
            operation=BuildMode.AUTO,
        )
        meson_source = self.workspace / "examples" / "meson-app"
        meson_build = meson_source / "_build"
        coredata = meson_build / "meson-private" / "coredata.dat"
        coredata.parent.mkdir(parents=True, exist_ok=True)
        coredata.write_text("configured")

        plan = self.engine.plan(options)

        descriptions = [step.description for step in plan.steps]
        self.assertEqual(descriptions, ["Build project"])

    def test_meson_configures_install_prefix(self) -> None:
        install_dir = Path(self.workspace) / "meson-prefix"
        options = BuildOptions(
            project_name="meson-app",
            presets=["dev"],
            operation=BuildMode.AUTO,
            install=True,
            install_dir=str(install_dir),
        )
        plan = self.engine.plan(options)

        configure_cmd = plan.steps[0].command
        build_cmd = plan.steps[1].command
        install_cmd = plan.steps[2].command

        self.assertIn("--prefix", configure_cmd)
        self.assertIn(str(install_dir), configure_cmd)
        self.assertEqual(build_cmd[:3], ["meson", "compile", "-C"])
        self.assertEqual(install_cmd, ["meson", "install", "-C", str(plan.build_dir)])

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

    def test_plan_without_build_dir_has_no_steps(self) -> None:
        options = BuildOptions(
            project_name="meta",
            presets=[],
            operation=BuildMode.AUTO,
        )
        plan = self.engine.plan(options)
        self.assertIsNone(plan.build_dir)
        self.assertEqual(plan.steps, [])
        self.assertIsNone(plan.context["user"].get("toolchain"))
        results = self.engine.execute(plan, dry_run=False)
        self.assertEqual(results, [])

    def test_extra_args_are_merged_without_duplicates(self) -> None:
        projects_dir = self.workspace / "config" / "projects"
        (projects_dir / "split-args.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "split-args"
                source_dir = "{{builder.path}}/examples/split-args"
                build_dir = "_build"
                build_system = "cmake"
                install_dir = "_install"
                extra_config_args = ["-DCONFIG_FROM_PROJECT", "-Dshared"]
                extra_build_args = ["--build-from-project", "-Dshared"]
                extra_install_args = ["--install-from-project", "--shared"]
                toolchain = "clang"

                [git]
                url = "https://example.com/split.git"
                main_branch = "main"

                [presets.extra]
                extra_config_args = ["-DCONFIG_FROM_PRESET", "-Dshared"]
                extra_build_args = ["--build-from-preset", "-Dshared"]
                extra_install_args = ["--install-from-preset", "--shared"]
                """
            )
        )

        store = ConfigurationStore.from_directory(self.workspace)
        engine = BuildEngine(
            store=store, command_runner=self.runner, workspace=self.workspace
        )
        options = BuildOptions(
            project_name="split-args",
            presets=["extra"],
            extra_config_args=["-DCONFIG_FROM_CLI", "-Dshared"],
            extra_build_args=["--build-from-cli", "-Dshared"],
            extra_install_args=["--install-from-cli", "--shared"],
            install=True,
            install_dir=str(self.workspace / "install-root"),
        )
        with patch("builder.build.shutil.which", return_value=None):
            plan = engine.plan(options)

        self.assertIn("split-args", plan.project.name)
        self.assertEqual(
            set(plan.extra_config_args),
            {
                "-DCONFIG_FROM_PRESET",
                "-Dshared",
                "-DCONFIG_FROM_PROJECT",
                "-DCONFIG_FROM_CLI",
            },
        )
        self.assertEqual(
            set(plan.extra_build_args),
            {
                "--build-from-preset",
                "-Dshared",
                "--build-from-project",
                "--build-from-cli",
            },
        )
        self.assertEqual(
            set(plan.extra_install_args),
            {
                "--install-from-preset",
                "--shared",
                "--install-from-project",
                "--install-from-cli",
            },
        )

        install_steps = [
            step for step in plan.steps if step.description == "Install project"
        ]
        self.assertTrue(install_steps)
        install_cmd = install_steps[0].command
        for expected in [
            "--install-from-preset",
            "--install-from-project",
            "--install-from-cli",
            "--shared",
        ]:
            self.assertIn(expected, install_cmd)
