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
            def __init__(self, runner) -> None:
                self.runner = runner

            def get_repository_state(self, repo_path: Path, *, environment=None):
                self.repo_path = repo_path
                return ("main", "abcdef1234567890")

        args = SimpleNamespace(project=None)
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", FakeGitManager):
            with redirect_stdout(buffer):
                exit_code = cli._handle_list(args, self.workspace)
        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("Project", output)
        self.assertIn("demo", output)
        self.assertIn("abcdef1234567890", output)

    def test_list_handles_missing_repository(self) -> None:
        class FakeGitManager:
            def __init__(self, runner) -> None:
                self.runner = runner

            def get_repository_state(self, repo_path: Path, *, environment=None):
                return (None, None)

        args = SimpleNamespace(project="demo")
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
