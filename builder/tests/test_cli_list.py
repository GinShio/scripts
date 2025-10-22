from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import re
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import shutil
import textwrap

from builder import cli
from builder.git_manager import GitWorkState


class ListCommandTests(unittest.TestCase):
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
                source_dir = "{{builder.path}}/repos/demo"
                build_system = "cmake"
                build_dir = "_build"
                install_dir = "_install"
                toolchain = "clang"

                [git]
                url = "https://example.com/demo.git"
                main_branch = "main"
                """
            )
        )
        # Ensure the repo directory exists for nicer output
        repo_dir = self.workspace / "repos" / "demo"
        (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_all_projects_displays_commit_information(self) -> None:
        class FakeGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                self.prepare_calls: list[tuple[Path, str]] = []
                self.restore_calls: list[Path] = []
                FakeGitManager.last_instance = self

            def get_repository_state(self, repo_path: Path, *, environment=None):
                self.repo_path = repo_path
                return ("main", "abcdef1234567890")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return []

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
            ):
                self.prepare_calls.append((repo_path, target_branch))
                return GitWorkState(branch="main", stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                self.restore_calls.append(repo_path)

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

        build_dir = self.workspace / "repos" / "demo" / "_build"
        build_dir.mkdir(parents=True, exist_ok=True)
        cache_path = build_dir / "CMakeCache.txt"
        cache_path.write_text(
            textwrap.dedent(
                """
                # This is the CMake cache.
                CMAKE_INSTALL_PREFIX:PATH=/opt/custom/install
                """
            ).strip()
        )

        args = SimpleNamespace(
            projects=[],
            branch=None,
            no_switch_branch=False,
            path=False,
            url=False,
            presets=False,
            dependencies=False,
            submodules=None,
            show_build_dir=False,
            show_install_dir=False,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)
        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("Project", output)
        self.assertIn("demo", output)
        self.assertIn("abcdef12345", output)
        self.assertNotIn("https://example.com/demo.git", output)
        header_line = output.splitlines()[0]
        self.assertNotIn("Path", header_line)
        self.assertNotIn("Build Dir", header_line)
        self.assertNotIn("Install Dir", header_line)
        self.assertNotIn("URL", header_line)

        fake_manager = FakeGitManager.last_instance
        self.assertIsNotNone(fake_manager)
        self.assertEqual(len(fake_manager.prepare_calls), 1)
        self.assertEqual(fake_manager.prepare_calls[0][1], "main")
        self.assertEqual(len(fake_manager.restore_calls), 1)

    def test_list_can_show_build_and_install_dirs(self) -> None:
        class FakeGitManager:
            def __init__(self, runner) -> None:
                self.runner = runner

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return ("dev", "0123456789abcdef")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return []

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
            ):
                return GitWorkState(branch="dev", stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                pass

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

        args = SimpleNamespace(
            projects=[],
            branch=None,
            no_switch_branch=False,
            path=False,
            url=False,
            presets=False,
            dependencies=False,
            submodules=None,
            show_build_dir=True,
            show_install_dir=True,
        )
        # Pre-create the build directory to simulate an existing build with resolved paths
        plan_build_dir = self.workspace / "repos" / "demo" / "_build"
        plan_build_dir.mkdir(parents=True, exist_ok=True)
        # Write a minimal CMakeCache to reflect CLI overrides during list
        (plan_build_dir / "CMakeCache.txt").write_text(
            textwrap.dedent(
                """
                CMAKE_INSTALL_PREFIX:PATH=/opt/custom/install
                """
            ).strip()
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        header_line = output.splitlines()[0]
        self.assertIn("Build Dir", header_line)
        self.assertIn("Install Dir", header_line)
        self.assertIn("/opt/custom/install", output)

    def test_list_with_url_flag_displays_repository_url(self) -> None:
        class FakeGitManager:
            def __init__(self, runner) -> None:
                self.runner = runner

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return ("dev", "0123456789abcdef")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return []

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
            ):
                return GitWorkState(branch="dev", stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                pass

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

        args = SimpleNamespace(
            projects=[],
            branch=None,
            no_switch_branch=False,
            path=True,
            url=True,
            presets=False,
            dependencies=False,
            submodules=None,
            show_build_dir=False,
            show_install_dir=False,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        header_line = output.splitlines()[0]
        self.assertIn("URL", header_line)
        self.assertIn("https://example.com/demo.git", output)

    def test_list_with_path_flag_displays_repository_path(self) -> None:
        class FakeGitManager:
            def __init__(self, runner) -> None:
                self.runner = runner

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return ("dev", "0123456789abcdef")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return []

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
            ):
                return GitWorkState(branch="dev", stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                pass

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

        args = SimpleNamespace(
            projects=[],
            branch=None,
            no_switch_branch=False,
            path=True,
            url=False,
            presets=False,
            dependencies=False,
            submodules=None,
            show_build_dir=False,
            show_install_dir=False,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        header_line = output.splitlines()[0]
        headers = re.split(r"\s{2,}", header_line.strip())
        self.assertEqual(headers, ["Project", "Branch", "Commit", "Path"])
        repo_path = self.workspace / "repos" / "demo"
        self.assertIn(str(repo_path), output)

    def test_list_column_order_matches_specification(self) -> None:
        class FakeGitManager:
            def __init__(self, runner) -> None:
                self.runner = runner

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return ("dev", "0123456789abcdef")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return []

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
            ):
                return GitWorkState(branch="dev", stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                pass

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

        plan_build_dir = self.workspace / "repos" / "demo" / "_build"
        plan_build_dir.mkdir(parents=True, exist_ok=True)
        (plan_build_dir / "CMakeCache.txt").write_text(
            textwrap.dedent(
                """
                CMAKE_INSTALL_PREFIX:PATH=/opt/custom/install
                """
            ).strip()
        )

        args = SimpleNamespace(
            projects=[],
            branch=None,
            no_switch_branch=False,
            path=True,
            url=True,
            presets=True,
            dependencies=True,
            submodules=None,
            show_build_dir=True,
            show_install_dir=True,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        header_line = output.splitlines()[0]
        headers = re.split(r"\s{2,}", header_line.strip())
        self.assertEqual(
            headers,
            [
                "Project",
                "Branch",
                "Commit",
                "Path",
                "URL",
                "Build Dir",
                "Install Dir",
                "Presets",
                "Dependencies",
            ],
        )

    def test_list_handles_missing_repository(self) -> None:
        class FakeGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                self.prepare_calls: list[tuple[Path, str]] = []
                FakeGitManager.last_instance = self

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return (None, None)

            def list_submodules(self, repo_path: Path, *, environment=None):
                return []

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
            ):
                self.prepare_calls.append((repo_path, target_branch))
                return GitWorkState(branch="main", stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                pass

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return (repo_path / ".git").exists()

        args = SimpleNamespace(
            projects=["demo"],
            branch=None,
            no_switch_branch=False,
            path=False,
            url=False,
            presets=False,
            dependencies=False,
            submodules=None,
            show_build_dir=False,
            show_install_dir=False,
        )
        repo_dir = self.workspace / "repos" / "demo"
        # Remove the .git directory to simulate missing repo
        if (repo_dir / ".git").exists():
            shutil.rmtree(repo_dir / ".git")

        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)
        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("demo", output)
        self.assertIn("<missing>", output)

        fake_manager = FakeGitManager.last_instance
        self.assertIsNotNone(fake_manager)
        self.assertEqual(len(fake_manager.prepare_calls), 0)

    def test_list_displays_submodules_section(self) -> None:
        class FakeGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                FakeGitManager.last_instance = self

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return ("main", "1234567890abcdef")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return [
                    {
                        "path": "external/vulkan",
                        "hash": "fedcba9876543210",
                        "url": "https://example.com/vulkan.git",
                    }
                ]

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
            ):
                return GitWorkState(branch="main", stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                pass

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

        args = SimpleNamespace(
            projects=[],
            branch=None,
            no_switch_branch=False,
            path=True,
            url=True,
            presets=False,
            dependencies=False,
            submodules=None,
            show_build_dir=False,
            show_install_dir=False,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("Project", output)
        self.assertIn("URL", output.splitlines()[0])
        lines = [line for line in output.splitlines() if "external/vulkan" in line]
        self.assertEqual(len(lines), 1)
        submodule_line = lines[0]
        self.assertIn("fedcba98765", submodule_line)
        self.assertIn("external/vulkan", submodule_line)
        self.assertIn("https://example.com/vulkan.git", submodule_line)
        parts = [part.strip() for part in submodule_line.split("  ") if part.strip()]
        self.assertIn("fedcba98765", parts)
        self.assertIn("external/vulkan", parts)
        self.assertIn("https://example.com/vulkan.git", parts)

    def test_list_accepts_multiple_projects_and_toggles_submodules(self) -> None:
        other_project_path = self.workspace / "config" / "projects" / "second.toml"
        other_project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "second"
                source_dir = "{{builder.path}}/repos/second"
                toolchain = "clang"

                [git]
                url = "https://example.com/second.git"
                main_branch = "main"
                """
            )
        )
        (self.workspace / "repos" / "second" / ".git").mkdir(parents=True, exist_ok=True)

        class FakeGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                self.prepare_calls: list[Path] = []
                FakeGitManager.last_instance = self

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

            def get_repository_state(self, repo_path: Path, *, environment=None):
                commit_map = {
                    "demo": "11111111111aaaaa",
                    "second": "22222222222bbbbb",
                }
                name = repo_path.name
                return ("main", commit_map.get(name, "deadbeefdeadbeef"))

            def list_submodules(self, repo_path: Path, *, environment=None):
                return [
                    {
                        "path": f"modules/{repo_path.name}",
                        "hash": "9999999999999999",
                        "url": "https://example.com/modules.git",
                    }
                ]

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
            ):
                self.prepare_calls.append(repo_path)
                return GitWorkState(branch=target_branch, stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                pass

        args = SimpleNamespace(
            projects=["demo", "second"],
            branch=None,
            no_switch_branch=False,
            path=False,
            url=False,
            presets=False,
            dependencies=False,
            submodules=False,
            show_build_dir=False,
            show_install_dir=False,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        data_lines = [line for line in output.splitlines()[2:] if line.strip()]
        self.assertEqual(len(data_lines), 2)
        self.assertIn("demo", data_lines[0])
        self.assertIn("second", data_lines[1])
        self.assertNotIn("modules/demo", output)
        self.assertNotIn("modules/second", output)

        fake_manager = FakeGitManager.last_instance
        self.assertIsNotNone(fake_manager)
        assert fake_manager is not None
        self.assertEqual([path.name for path in fake_manager.prepare_calls], ["demo", "second"])

    def test_list_with_presets_and_dependencies(self) -> None:
        project_path = self.workspace / "config" / "projects" / "complex.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "complex"
                source_dir = "{{builder.path}}/repos/complex"
                toolchain = "clang"

                [git]
                url = "https://example.com/complex.git"
                main_branch = "main"

                [presets.default]
                description = "Default preset"

                [presets.optimized]
                description = "Optimized preset"

                [[dependencies]]
                name = "dep-one"
                presets = ["fast"]

                [[dependencies]]
                name = "dep-two"
                """
            )
        )
        repo_dir = self.workspace / "repos" / "complex"
        (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

        class FakeGitManager:
            def __init__(self, runner) -> None:
                self.runner = runner

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return ("feature", "1234567890abcdef")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return [
                    {
                        "path": "external/lib",
                        "hash": "abcdefabcdefabcd",
                        "url": "https://example.com/lib.git",
                    }
                ]

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
            ):
                return GitWorkState(branch="feature", stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                pass

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

        args = SimpleNamespace(
            projects=["complex"],
            branch=None,
            no_switch_branch=False,
            path=False,
            url=False,
            presets=True,
            dependencies=True,
            submodules=None,
            show_build_dir=False,
            show_install_dir=False,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        header_line = output.splitlines()[0]
        self.assertIn("Presets", header_line)
        self.assertIn("Dependencies", header_line)
        self.assertIn("default, optimized", output)
        self.assertIn("dep-one (fast)", output)
        self.assertIn("dep-two", output)
        self.assertNotIn("external/lib", output)
        self.assertNotIn("abcdefabcdefa", output)

    def test_list_accepts_nested_repository_paths(self) -> None:
        component_project_path = self.workspace / "config" / "projects" / "component.toml"
        component_project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "component"
                source_dir = "{{builder.path}}/repos/demo/component"
                toolchain = "clang"

                [git]
                url = "https://example.com/demo.git"
                main_branch = "main"
                """
            )
        )

        component_dir = self.workspace / "repos" / "demo" / "component"
        component_dir.mkdir(parents=True, exist_ok=True)

        class FakeGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                self.repo_paths: list[Path] = []
                FakeGitManager.last_instance = self

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                self.repo_paths.append(repo_path)
                return True

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return ("main", "feedfacecafebeef")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return []

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
            ):
                return GitWorkState(branch="main", stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                pass

        args = SimpleNamespace(
            projects=["component"],
            branch=None,
            no_switch_branch=False,
            path=False,
            url=False,
            presets=False,
            dependencies=False,
            submodules=None,
            show_build_dir=False,
            show_install_dir=False,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("component", output)
        self.assertIn("feedfacecaf", output)

        fake_manager = FakeGitManager.last_instance
        self.assertIsNotNone(fake_manager)
        self.assertIn(component_dir, fake_manager.repo_paths)

    def test_list_component_branch_selection(self) -> None:
        project_path = self.workspace / "config" / "projects" / "component.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "component"
                source_dir = "{{builder.path}}/repos/component"
                component_dir = "libs/core"
                toolchain = "clang"

                [git]
                url = "https://example.com/component.git"
                main_branch = "main"
                component_branch = "component/main"
                """
            )
        )

        repo_root = self.workspace / "repos" / "component"
        component_path = repo_root / "libs" / "core"
        (component_path / ".git").mkdir(parents=True, exist_ok=True)

        class RecordingGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                self.prepare_calls: list[dict[str, object | None]] = []
                self.restore_calls: list[GitWorkState] = []
                RecordingGitManager.last_instance = self

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

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
            ) -> GitWorkState:
                self.prepare_calls.append(
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
            ) -> None:
                self.restore_calls.append(state)

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return ("component/main", "abc123def4567890")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return []

        args = SimpleNamespace(
            projects=["component"],
            branch=None,
            no_switch_branch=False,
            path=False,
            url=False,
            presets=False,
            dependencies=False,
            submodules=None,
            show_build_dir=False,
            show_install_dir=False,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", RecordingGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        self.assertEqual(exit_code, 0)
        manager = RecordingGitManager.last_instance
        self.assertIsNotNone(manager)
        assert manager is not None
        self.assertTrue(manager.prepare_calls)
        call = manager.prepare_calls[0]
        self.assertEqual(call["target_branch"], "main")
        self.assertEqual(call["component_dir"], Path("libs/core"))
        self.assertEqual(call["component_branch"], "component/main")
        self.assertTrue(manager.restore_calls)

    def test_list_branch_override_targets_component_repo(self) -> None:
        project_path = self.workspace / "config" / "projects" / "component.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "component"
                source_dir = "{{builder.path}}/repos/component"
                component_dir = "libs/core"
                toolchain = "clang"

                [git]
                url = "https://example.com/component.git"
                main_branch = "main"
                component_branch = "component/main"
                """
            )
        )

        repo_root = self.workspace / "repos" / "component"
        component_path = repo_root / "libs" / "core"
        (component_path / ".git").mkdir(parents=True, exist_ok=True)

        class RecordingGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                self.prepare_calls: list[dict[str, object | None]] = []
                self.last_target_branch: str | None = None
                RecordingGitManager.last_instance = self

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

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
            ) -> GitWorkState:
                self.prepare_calls.append(
                    {
                        "repo_path": repo_path,
                        "target_branch": target_branch,
                        "component_dir": component_dir,
                        "component_branch": component_branch,
                    }
                )
                return GitWorkState(
                    branch=target_branch,
                    stash_applied=False,
                    component_branch=component_branch,
                    component_path=component_path,
                    component_stash_applied=False,
                )

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                return None

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return ("component/dev", "0123456789abcdef")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return []

        args = SimpleNamespace(
            projects=["component"],
            branch="component/dev",
            no_switch_branch=False,
            path=False,
            url=False,
            presets=False,
            dependencies=False,
            submodules=None,
            show_build_dir=False,
            show_install_dir=False,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", RecordingGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        self.assertEqual(exit_code, 0)
        manager = RecordingGitManager.last_instance
        self.assertIsNotNone(manager)
        assert manager is not None
        self.assertTrue(manager.prepare_calls)
        call = manager.prepare_calls[0]
        self.assertEqual(call["target_branch"], "main")
        self.assertEqual(call["component_branch"], "component/dev")

    def test_list_branch_override_single_repository(self) -> None:
        project_path = self.workspace / "config" / "projects" / "single.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "single"
                source_dir = "{{builder.path}}/repos/single"
                toolchain = "clang"

                [git]
                url = "https://example.com/single.git"
                main_branch = "main"
                """
            )
        )

        repo_root = self.workspace / "repos" / "single"
        (repo_root / ".git").mkdir(parents=True, exist_ok=True)

        class RecordingGitManager:
            last_instance = None

            def __init__(self, runner) -> None:
                self.runner = runner
                self.prepare_calls: list[dict[str, object | None]] = []
                RecordingGitManager.last_instance = self

            def is_repository(self, repo_path: Path, *, environment=None) -> bool:
                return True

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
            ) -> GitWorkState:
                self.prepare_calls.append(
                    {
                        "repo_path": repo_path,
                        "target_branch": target_branch,
                        "component_dir": component_dir,
                        "component_branch": component_branch,
                    }
                )
                self.last_target_branch = target_branch
                return GitWorkState(branch=target_branch, stash_applied=False)

            def restore_checkout(self, repo_path: Path, state: GitWorkState, *, environment=None) -> None:
                return None

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return (self.last_target_branch or "main", "deadbeefcafebabe")

            def list_submodules(self, repo_path: Path, *, environment=None):
                return []

        args = SimpleNamespace(
            projects=["single"],
            branch="release/2.0",
            no_switch_branch=False,
            path=False,
            url=False,
            presets=False,
            dependencies=False,
            submodules=None,
            show_build_dir=False,
            show_install_dir=False,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", RecordingGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        self.assertEqual(exit_code, 0)
        manager = RecordingGitManager.last_instance
        self.assertIsNotNone(manager)
        assert manager is not None
        self.assertTrue(manager.prepare_calls)
        call = manager.prepare_calls[0]
        self.assertEqual(call["target_branch"], "release/2.0")
        self.assertIsNone(call["component_branch"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
