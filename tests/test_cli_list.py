from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
import shutil

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
        (projects_dir / "demo.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "demo"
                source_dir = "{{builder.path}}/repos/demo"

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

        args = SimpleNamespace(project=None, branch=None, no_switch_branch=False, url=False)
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
        self.assertNotIn("URL", header_line)

        fake_manager = FakeGitManager.last_instance
        self.assertIsNotNone(fake_manager)
        self.assertEqual(len(fake_manager.prepare_calls), 1)
        self.assertEqual(fake_manager.prepare_calls[0][1], "main")
        self.assertEqual(len(fake_manager.restore_calls), 1)

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

        args = SimpleNamespace(project=None, branch=None, no_switch_branch=False, url=True)
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        header_line = output.splitlines()[0]
        self.assertIn("URL", header_line)
        self.assertIn("https://example.com/demo.git", output)

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

        args = SimpleNamespace(project="demo", branch=None, no_switch_branch=False, url=False)
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

        args = SimpleNamespace(project=None, branch=None, no_switch_branch=False, url=True)
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
        self.assertEqual(parts, ["fedcba98765", "external/vulkan", "https://example.com/vulkan.git"])

    def test_list_with_presets_and_dependencies(self) -> None:
        project_path = self.workspace / "config" / "projects" / "complex.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "complex"
                source_dir = "{{builder.path}}/repos/complex"

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

        args = SimpleNamespace(
            project="complex",
            branch=None,
            no_switch_branch=False,
            url=False,
            presets=True,
            dependencies=True,
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
