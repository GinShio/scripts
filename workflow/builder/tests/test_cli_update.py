from __future__ import annotations

import io
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builder import cli


class UpdateCommandTests(unittest.TestCase):
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
                build_dir = "_build/demo"
                build_system = "cmake"
                toolchain = "clang"

                [git]
                url = "https://example.com/demo.git"
                main_branch = "main"
                auto_stash = true
                """
            )
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_update_short_flags_parsed(self) -> None:
        args = cli._parse_arguments(["update", "-b", "feature", "-s", "skip", "-n"])
        self.assertEqual(args.command, "update")
        self.assertEqual(args.branch, "feature")
        self.assertEqual(args.submodule, "skip")
        self.assertTrue(args.dry_run)

    def test_update_dry_run_records_commands(self) -> None:
        args = SimpleNamespace(
            project=None,
            branch=None,
            submodule="default",
            dry_run=True,
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._handle_update(args, self.workspace)
        output = buffer.getvalue()
        self.assertIn("[dry-run]", output)
        self.assertIn("git init", output)
        self.assertIn("git fetch", output)
        self.assertIn(str(self.workspace / "repos" / "demo"), output)

    def test_update_dry_run_existing_repo_pulls_main_branch(self) -> None:
        repo_path = self.workspace / "repos" / "demo"
        (repo_path / ".git").mkdir(parents=True)
        args = SimpleNamespace(
            project="demo",
            branch=None,
            submodule="default",
            dry_run=True,
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._handle_update(args, self.workspace)
        output = buffer.getvalue()
        self.assertIn("git merge --ff-only origin/main", output)
        self.assertNotIn("git clone", output)

    def test_update_clone_script_template_is_resolved(self) -> None:
        config_path = self.workspace / "config" / "projects" / "demo.toml"
        config_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "demo"
                source_dir = "{{builder.path}}/repos/demo"
                build_dir = "_build/demo"
                build_system = "cmake"
                toolchain = "clang"

                [git]
                url = "https://example.com/demo.git"
                main_branch = "main"
                clone_script = "echo clone {{project.source_dir}}"
                """
            )
        )

        args = SimpleNamespace(
            project="demo",
            branch=None,
            submodule="default",
            dry_run=True,
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli._handle_update(args, self.workspace)
        output = buffer.getvalue()
        expected_path = self.workspace / "repos" / "demo"
        self.assertIn(f"echo clone {expected_path.as_posix()}", output)
        self.assertNotIn(f"(cwd={expected_path.as_posix()})", output)

    def test_update_passes_component_dir_to_git_manager(self) -> None:
        config_path = self.workspace / "config" / "projects" / "demo.toml"
        config_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "demo"
                source_dir = "{{builder.path}}/repos/demo"
                component_dir = "libs/core"
                build_dir = "_build/demo"
                build_system = "cmake"
                toolchain = "clang"

                [git]
                url = "https://example.com/demo.git"
                main_branch = "main"
                component_branch = "comp/main"
                """
            )
        )

        repo_path = self.workspace / "repos" / "demo"
        (repo_path / ".git").mkdir(parents=True, exist_ok=True)

        class RecordingGitManager:
            last_component_dir: Path | None = None
            last_main_branch: str | None = None
            last_component_branch: str | None = None

            def __init__(self, runner) -> None:
                self.runner = runner

            def update_repository(
                self,
                *,
                repo_path: Path,
                url: str,
                main_branch: str,
                component_branch=None,
                clone_script=None,
                update_script=None,
                auto_stash=False,
                environment=None,
                dry_run=False,
                component_dir=None,
            ) -> None:
                RecordingGitManager.last_component_dir = component_dir
                RecordingGitManager.last_main_branch = main_branch
                RecordingGitManager.last_component_branch = component_branch

        args = SimpleNamespace(
            project="demo",
            branch=None,
            submodule="default",
            dry_run=True,
        )
        buffer = io.StringIO()
        with patch("builder.cli.GitManager", RecordingGitManager):
            with redirect_stdout(buffer):
                cli._handle_update(args, self.workspace)

        self.assertEqual(RecordingGitManager.last_component_dir, Path("libs/core"))
        self.assertEqual(RecordingGitManager.last_main_branch, "main")
        self.assertEqual(RecordingGitManager.last_component_branch, "comp/main")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
