from __future__ import annotations

import io
import os
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builder import TemplateError, cli, validation


class ValidateCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self._orig_config_dir = os.environ.pop("BUILDER_CONFIG_DIR", None)
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
        (projects_dir / "alpha.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "alpha"
                source_dir = "{{builder.path}}/alpha"
                build_dir = "_build"
                build_system = "cmake"

                [git]
                url = "https://example.com/alpha.git"
                main_branch = "main"
                """
            )
        )
        (projects_dir / "beta.toml").write_text(
            textwrap.dedent(
                """
                [project]
                name = "beta"
                source_dir = "{{builder.path}}/beta"
                build_dir = "_build"
                build_system = "meson"

                [git]
                url = "https://example.com/beta.git"
                main_branch = "main"
                """
            )
        )

    def tearDown(self) -> None:
        if self._orig_config_dir is not None:
            os.environ["BUILDER_CONFIG_DIR"] = self._orig_config_dir
        self.temp_dir.cleanup()

    def test_parse_arguments_accepts_positional_project(self) -> None:
        args = cli._parse_arguments(["validate", "alpha"])
        self.assertEqual(args.command, "validate")
        self.assertEqual(args.project, "alpha")

    def test_validate_all_projects_runs_for_each(self) -> None:
        args = SimpleNamespace(project=None)
        with patch("builder.cli.validate_store_structure", return_value=[]):
            with (
                patch("builder.cli.validate_project") as mock_validate,
                patch("builder.cli.validate_project_templates") as mock_templates,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    cli._handle_validate(args, self.workspace)
        called_projects = {call.args[1] for call in mock_validate.call_args_list}
        template_projects = {call.args[1] for call in mock_templates.call_args_list}
        self.assertSetEqual(called_projects, {"alpha", "beta"})
        self.assertSetEqual(template_projects, {"alpha", "beta"})
        self.assertIn("Validation successful", buffer.getvalue())

    def test_validate_single_project_runs_only_requested(self) -> None:
        args = SimpleNamespace(project="beta")
        with patch("builder.cli.validate_store_structure", return_value=[]):
            with (
                patch("builder.cli.validate_project") as mock_validate,
                patch("builder.cli.validate_project_templates") as mock_templates,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    cli._handle_validate(args, self.workspace)
        mock_validate.assert_called_once()
        mock_templates.assert_called_once()
        self.assertEqual(mock_validate.call_args.args[1], "beta")
        self.assertIn("Validation successful", buffer.getvalue())

    def test_validate_reports_failures_and_returns_nonzero(self) -> None:
        args = SimpleNamespace(project=None)
        with patch("builder.cli.validate_store_structure", return_value=[]):
            with (
                patch(
                    "builder.cli.validate_project",
                    side_effect=[None, ValueError("broken config")],
                ) as mock_validate,
                patch("builder.cli.validate_project_templates") as mock_templates,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    status = cli._handle_validate(args, self.workspace)
        output = buffer.getvalue()
        self.assertEqual(status, 1)
        self.assertIn("Validation failed:", output)
        self.assertIn("[beta] broken config", output)
        self.assertEqual(mock_templates.call_count, 1)

    def test_validate_reports_global_errors(self) -> None:
        args = SimpleNamespace(project="alpha")
        with patch(
            "builder.cli.validate_store_structure", return_value=["bad shared preset"]
        ):
            with (
                patch(
                    "builder.cli.validate_project", side_effect=[None]
                ) as mock_validate,
                patch("builder.cli.validate_project_templates") as mock_templates,
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    status = cli._handle_validate(args, self.workspace)
        output = buffer.getvalue()
        self.assertEqual(status, 1)
        self.assertIn("[config] bad shared preset", output)
        mock_validate.assert_called_once()
        mock_templates.assert_called_once()

    def test_validate_project_rejects_absolute_build_dir(self) -> None:
        config_path = self.workspace / "config" / "projects" / "gamma.toml"
        config_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "gamma"
                source_dir = "{{builder.path}}/gamma"
                build_dir = "/absolute/build"
                build_system = "cmake"

                [git]
                url = "https://example.com/gamma.git"
                main_branch = "main"
                """
            )
        )
        store = cli.ConfigurationStore.from_directory(self.workspace)
        with self.assertRaisesRegex(ValueError, "build_dir must be a relative path"):
            validation.validate_project(store, "gamma", workspace=self.workspace)

    def test_validate_project_requires_build_dir_for_cargo(self) -> None:
        config_path = self.workspace / "config" / "projects" / "delta.toml"
        config_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "delta"
                source_dir = "{{builder.path}}/delta"
                build_system = "cargo"

                [git]
                url = "https://example.com/delta.git"
                main_branch = "main"
                """
            )
        )
        store = cli.ConfigurationStore.from_directory(self.workspace)
        with self.assertRaisesRegex(
            ValueError, "build_dir is required for build_system 'cargo'"
        ):
            validation.validate_project(store, "delta", workspace=self.workspace)

    def test_validate_project_catches_template_errors(self) -> None:
        config_path = self.workspace / "config" / "projects" / "epsilon.toml"
        config_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "epsilon"
                source_dir = "{{builder.path}}/epsilon"
                build_dir = "_build"
                build_system = "cmake"

                [project.environment]
                BIN_DIR = "{{project.environment.MISSING}}/bin"

                [git]
                url = "https://example.com/epsilon.git"
                main_branch = "main"
                """
            )
        )
        store = cli.ConfigurationStore.from_directory(self.workspace)
        with self.assertRaises(TemplateError):
            validation.validate_project_templates(
                store,
                "epsilon",
                workspace=self.workspace,
            )
