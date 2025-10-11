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

        def test_parses_dependency_array_of_tables(self) -> None:
                (self.config_dir / "config.toml").write_text(
                        textwrap.dedent(
                                """
                                [global]
                                default_build_type = "Debug"
                                """
                        ).strip()
                )
                (self.projects_dir / "lib.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "lib"
                                source_dir = "{{builder.path}}/lib"
                                build_dir = "_build/lib"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/lib.git"
                                main_branch = "main"
                                """
                        ).strip()
                )
                (self.projects_dir / "app.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "app"
                                source_dir = "{{builder.path}}/app"
                                build_dir = "_build/app"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/app.git"
                                main_branch = "main"

                                [[dependencies]]
                                name = "lib"
                                presets = ["debug"]

                                [[dependencies]]
                                name = "tools"
                                """
                        ).strip()
                )

                store = ConfigurationStore.from_directory(self.root)
                project = store.get_project("app")
                self.assertEqual(len(project.dependencies), 2)
                self.assertEqual(project.dependencies[0].name, "lib")
                self.assertEqual(project.dependencies[0].presets, ["debug"])
                self.assertEqual(project.dependencies[1].name, "tools")
                self.assertEqual(project.dependencies[1].presets, [])

        def test_from_directories_merges_overrides_in_priority_order(self) -> None:
                base_shared = self.config_dir / "company-base.toml"
                base_shared.write_text(
                        textwrap.dedent(
                                """
                                [presets.default.environment]
                                CC = "gcc"
                                """
                        ).strip()
                )
                base_project = self.projects_dir / "demo.toml"
                base_project.write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "demo"
                                source_dir = "{{builder.path}}/demo"
                                build_dir = "_build/base"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/demo.git"
                                main_branch = "main"
                                """
                        ).strip()
                )

                override_dir = self.root / "override"
                override_projects = override_dir / "projects"
                override_projects.mkdir(parents=True)
                (override_dir / "config.toml").write_text(
                        textwrap.dedent(
                                """
                                [global]
                                default_build_type = "Release"
                                """
                        ).strip()
                )
                (override_dir / "company-base.toml").write_text(
                        textwrap.dedent(
                                """
                                [presets.default.environment]
                                CC = "clang"
                                """
                        ).strip()
                )
                (override_projects / "demo.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "demo"
                                source_dir = "{{builder.path}}/demo"
                                build_dir = "_build/override"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/demo.git"
                                main_branch = "main"
                                """
                        ).strip()
                )
                (override_projects / "tools.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "tools"
                                source_dir = "{{builder.path}}/tools"

                                [git]
                                url = "https://example.com/tools.git"
                                main_branch = "main"
                                """
                        ).strip()
                )

                store = ConfigurationStore.from_directories(self.root, [self.config_dir, override_dir])

                self.assertEqual(store.config_dirs, (self.config_dir.resolve(), override_dir.resolve()))
                self.assertEqual(store.global_config.default_build_type, "Release")

                shared = store.shared_configs["company-base"]
                env = shared.get("presets", {}).get("default", {}).get("environment", {})
                self.assertEqual(env.get("CC"), "clang")

                demo = store.get_project("demo")
                self.assertEqual(demo.build_dir, "_build/override")

                self.assertIn("tools", store.projects)

        def test_resolve_dependency_chain_orders_and_merges(self) -> None:
                (self.config_dir / "config.toml").write_text("[global]\n")
                (self.projects_dir / "core.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "core"
                                source_dir = "{{builder.path}}/core"
                                build_dir = "_build/core"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/core.git"
                                main_branch = "main"
                                """
                        ).strip()
                )
                (self.projects_dir / "lib.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "lib"
                                source_dir = "{{builder.path}}/lib"
                                build_dir = "_build/lib"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/lib.git"
                                main_branch = "main"

                                [[dependencies]]
                                name = "core"
                                """
                        ).strip()
                )
                (self.projects_dir / "tools.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "tools"
                                source_dir = "{{builder.path}}/tools"
                                build_dir = "_build/tools"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/tools.git"
                                main_branch = "main"
                                """
                        ).strip()
                )
                (self.projects_dir / "app.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "app"
                                source_dir = "{{builder.path}}/app"
                                build_dir = "_build/app"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/app.git"
                                main_branch = "main"

                                [[dependencies]]
                                name = "lib"
                                presets = ["release"]
                                """
                        ).strip()
                )

                store = ConfigurationStore.from_directory(self.root)
                chain = store.resolve_dependency_chain("app")
                self.assertEqual([dep.project.name for dep in chain], ["core", "lib"])
                self.assertEqual(chain[1].presets, ["release"])

        def test_resolve_dependency_chain_detects_cycles(self) -> None:
                (self.config_dir / "config.toml").write_text("[global]\n")
                (self.projects_dir / "lib.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "lib"
                                source_dir = "{{builder.path}}/lib"
                                build_dir = "_build/lib"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/lib.git"
                                main_branch = "main"

                                [[dependencies]]
                                name = "app"
                                """
                        ).strip()
                )
                (self.projects_dir / "app.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "app"
                                source_dir = "{{builder.path}}/app"
                                build_dir = "_build/app"
                                build_system = "cmake"

                                [git]
                                url = "https://example.com/app.git"
                                main_branch = "main"

                                [[dependencies]]
                                name = "lib"
                                """
                        ).strip()
                )

                store = ConfigurationStore.from_directory(self.root)
                with self.assertRaises(ValueError):
                        store.resolve_dependency_chain("app")

        def test_project_without_build_dir_is_allowed(self) -> None:
                (self.config_dir / "config.toml").write_text("[global]\n")
                (self.projects_dir / "meta.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "meta"
                                source_dir = "/src/meta"

                                [git]
                                url = "https://example.com/meta.git"
                                main_branch = "main"
                                """
                        ).strip()
                )

                store = ConfigurationStore.from_directory(self.root)
                project = store.get_project("meta")
                self.assertIsNone(project.build_dir)
                self.assertIsNone(project.build_system)

        def test_project_extra_args_are_kept_separate(self) -> None:
                (self.config_dir / "config.toml").write_text("[global]\n")
                (self.projects_dir / "demo.toml").write_text(
                        textwrap.dedent(
                                """
                                [project]
                                name = "demo"
                                source_dir = "/src/demo"
                                build_dir = "_build"
                                build_system = "cmake"
                                extra_config_args = ["-DCONFIG_FROM_PROJECT", "-Dshared"]
                                extra_build_args = ["--build-from-project", "--target", "install"]

                                [git]
                                url = "https://example.com/demo.git"
                                main_branch = "main"
                                """
                        ).strip()
                )

                store = ConfigurationStore.from_directory(self.root)
                project = store.get_project("demo")
                self.assertEqual(
                        project.extra_config_args,
                        ["-DCONFIG_FROM_PROJECT", "-Dshared"],
                )
                self.assertEqual(
                        project.extra_build_args,
                        ["--build-from-project", "--target", "install"],
                )


if __name__ == "__main__":  # pragma: no cover
        unittest.main()
