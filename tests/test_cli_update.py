from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout

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
        (projects_dir / "demo.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "demo"
                source_dir = "{{builder.path}}/repos/demo"
                build_dir = "_build/demo"
                build_system = "cmake"

                [git]
                url = "https://example.com/demo.git"
                main_branch = "main"
                auto_stash = true
                """
            )
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

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
        self.assertIn("git clone", output)
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
        self.assertIn("git pull --ff-only origin main", output)
        self.assertNotIn("git clone", output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
