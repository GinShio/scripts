from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from builder import TemplateError, validation
from builder.cli import ConfigurationStore


class ValidationModuleTests(unittest.TestCase):
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
                default_build_type = "Debug"
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

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_validate_store_structure_flags_non_string_extends(self) -> None:
        shared_path = self.workspace / "config" / "shared.toml"
        shared_path.write_text(
            textwrap.dedent(
                """
                [presets."bad"]
                extends = 1
                """
            )
        )
        store = ConfigurationStore.from_directory(self.workspace)
        errors = validation.validate_store_structure(store)
        self.assertTrue(any("extends" in message for message in errors))

    def test_validate_project_flags_unknown_extends(self) -> None:
        project_path = self.workspace / "config" / "projects" / "alpha.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "alpha"
                source_dir = "{{builder.path}}/alpha"
                build_dir = "_build"
                build_system = "cmake"

                [presets.default]
                extends = ["missing"]

                [git]
                url = "https://example.com/alpha.git"
                main_branch = "main"
                """
            )
        )
        store = ConfigurationStore.from_directory(self.workspace)
        with self.assertRaisesRegex(ValueError, "unknown preset 'missing'"):
            validation.validate_project(store, "alpha", workspace=self.workspace)

    def test_validate_project_condition_requires_expression_format(self) -> None:
        project_path = self.workspace / "config" / "projects" / "alpha.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "alpha"
                source_dir = "{{builder.path}}/alpha"
                build_dir = "_build"
                build_system = "cmake"

                [presets.default]
                condition = "invalid"

                [git]
                url = "https://example.com/alpha.git"
                main_branch = "main"
                """
            )
        )
        store = ConfigurationStore.from_directory(self.workspace)
        with self.assertRaisesRegex(
            ValueError, r"must use the form \[\[ expression \]\]"
        ):
            validation.validate_project(store, "alpha", workspace=self.workspace)

    def test_validate_project_condition_expression_syntax(self) -> None:
        project_path = self.workspace / "config" / "projects" / "alpha.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "alpha"
                source_dir = "{{builder.path}}/alpha"
                build_dir = "_build"
                build_system = "cmake"

                [presets.default]
                condition = "[[ 1 + ]]"

                [git]
                url = "https://example.com/alpha.git"
                main_branch = "main"
                """
            )
        )
        store = ConfigurationStore.from_directory(self.workspace)
        with self.assertRaisesRegex(ValueError, "Invalid expression syntax"):
            validation.validate_project(store, "alpha", workspace=self.workspace)

    def test_validate_project_environment_expression_syntax(self) -> None:
        project_path = self.workspace / "config" / "projects" / "alpha.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "alpha"
                source_dir = "{{builder.path}}/alpha"
                build_dir = "_build"
                build_system = "cmake"

                [presets.default.environment]
                BROKEN = "[[ 1 + ]]"

                [git]
                url = "https://example.com/alpha.git"
                main_branch = "main"
                """
            )
        )
        store = ConfigurationStore.from_directory(self.workspace)
        with self.assertRaisesRegex(ValueError, "Invalid expression syntax"):
            validation.validate_project(store, "alpha", workspace=self.workspace)

    def test_validate_project_allows_template_extends(self) -> None:
        project_path = self.workspace / "config" / "projects" / "alpha.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "alpha"
                source_dir = "{{builder.path}}/alpha"
                build_dir = "_build"
                build_system = "cmake"

                [presets.default]
                extends = "{{user.branch}}"

                [git]
                url = "https://example.com/alpha.git"
                main_branch = "main"
                """
            )
        )
        store = ConfigurationStore.from_directory(self.workspace)
        # Should not raise due to template extends
        validation.validate_project(store, "alpha", workspace=self.workspace)

    def test_validate_project_catches_template_errors(self) -> None:
        project_path = self.workspace / "config" / "projects" / "epsilon.toml"
        project_path.write_text(
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
        store = ConfigurationStore.from_directory(self.workspace)
        with self.assertRaises(TemplateError):
            validation.validate_project_templates(
                store,
                "epsilon",
                workspace=self.workspace,
            )

    def test_validate_project_detects_environment_cycles(self) -> None:
        project_path = self.workspace / "config" / "projects" / "alpha.toml"
        project_path.write_text(
            textwrap.dedent(
                """
                [project]
                name = "alpha"
                source_dir = "{{builder.path}}/alpha"
                build_dir = "_build"
                build_system = "cmake"

                [project.environment]
                FOO = "{{project.environment.BAR}}"
                BAR = "{{project.environment.FOO}}"

                [git]
                url = "https://example.com/alpha.git"
                main_branch = "main"
                """
            )
        )
        store = ConfigurationStore.from_directory(self.workspace)
        with self.assertRaises(TemplateError):
            validation.validate_project_templates(
                store,
                "alpha",
                workspace=self.workspace,
            )
