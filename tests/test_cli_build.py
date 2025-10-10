from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import io
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout

from builder import cli


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
            extra_args=[],
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
