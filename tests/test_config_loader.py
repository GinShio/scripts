from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import unittest

from builder.config_loader import ConfigurationStore

try:  # PyYAML is optional
        import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - optional dependency absent
        yaml = None


class ConfigurationLoaderTests(unittest.TestCase):
        def setUp(self) -> None:
                self.temp_dir = tempfile.TemporaryDirectory()
                self.root = Path(self.temp_dir.name)
                self.config_dir = self.root / "config"
                self.projects_dir = self.config_dir / "projects"
                self.projects_dir.mkdir(parents=True)

        def tearDown(self) -> None:
                self.temp_dir.cleanup()

        def test_supports_json_configs(self) -> None:
                (self.config_dir / "config.json").write_text(
                        textwrap.dedent(
                                """
                                {
                                    "global": {
                                        "default_build_type": "Release",
                                        "default_operation": "auto"
                                    }
                                }
                                """
                        ).strip()
                )
                (self.config_dir / "company-base.json").write_text(
                        textwrap.dedent(
                                """
                                {
                                    "presets": {
                                        "default": {
                                            "environment": {
                                                "CC": "gcc"
                                            }
                                        }
                                    }
                                }
                                """
                        ).strip()
                )
                (self.projects_dir / "demo.json").write_text(
                        textwrap.dedent(
                                """
                                {
                                    "project": {
                                        "name": "demo",
                                        "source_dir": "/src/demo",
                                        "build_dir": "_build/default",
                                        "build_system": "cmake"
                                    },
                                    "git": {
                                        "url": "https://example.com/demo.git",
                                        "main_branch": "main"
                                    }
                                }
                                """
                        ).strip()
                )

                store = ConfigurationStore.from_directory(self.root)
                self.assertEqual(store.global_config.default_build_type, "Release")
                self.assertIn("company-base", store.shared_configs)
                project = store.get_project("demo")
                self.assertEqual(project.name, "demo")
                self.assertEqual(project.build_system, "cmake")

        @unittest.skipUnless(yaml is not None, "PyYAML is required for YAML config tests")
        def test_supports_yaml_configs(self) -> None:
                (self.config_dir / "config.yaml").write_text(
                        textwrap.dedent(
                                """
                                global:
                                    default_build_type: Debug
                                    default_operation: auto
                                """
                        ).strip()
                )
                (self.projects_dir / "demo.yaml").write_text(
                        textwrap.dedent(
                                """
                                project:
                                    name: demo
                                    source_dir: "/src/demo"
                                    build_dir: "_build/default"
                                    build_system: cmake
                                git:
                                    url: "https://example.com/demo.git"
                                    main_branch: main
                                """
                        ).strip()
                )

                store = ConfigurationStore.from_directory(self.root)
                self.assertEqual(store.global_config.default_build_type, "Debug")
                self.assertIn("demo", store.projects)

        def test_conflicting_config_stems_raise(self) -> None:
                (self.config_dir / "config.toml").write_text("[global]\n")
                (self.config_dir / "shared.toml").write_text("[data]\nvalue = 1\n")
                (self.config_dir / "shared.json").write_text("{\n  \"data\": {\"value\": 2}\n}\n")
                (self.projects_dir / "demo.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "demo"
                                source_dir = "/src/demo"
                                build_dir = "_build"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/demo.git"
                                main_branch = "main"
                                """
                        ).strip()
                )

                with self.assertRaises(ValueError):
                        ConfigurationStore.from_directory(self.root)


if __name__ == "__main__":  # pragma: no cover
        unittest.main()
