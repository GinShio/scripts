from __future__ import annotations

import unittest

from builder import TemplateError, TemplateResolver
from builder.presets import PresetRepository


class PresetRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = {
            "user": {"branch": "main", "build_type": "Debug"},
            "project": {
                "name": "demo",
                "source_dir": "/src/demo",
                "build_dir": "/src/demo/_build",
            },
            "system": {"os": "linux", "architecture": "x86_64"},
            "env": {"PATH": "/usr/bin"},
        }
        self.resolver = TemplateResolver(self.context)

    def test_inheritance_and_overrides(self) -> None:
        repo = PresetRepository(
            project_presets={
                "base": {
                    "environment": {"CC": "clang"},
                    "definitions": {"BUILD_SHARED_LIBS": False},
                },
                "dev": {
                    "extends": ["base"],
                    "environment": {"CFLAGS": "-O0"},
                    "definitions": {"BUILD_SHARED_LIBS": True},
                },
            },
            project_name="demo",
            project_org=None,
        )
        resolved = repo.resolve(["dev"], template_resolver=self.resolver)
        self.assertEqual(resolved.environment["CC"], "clang")
        self.assertEqual(resolved.environment["CFLAGS"], "-O0")
        self.assertTrue(resolved.definitions["BUILD_SHARED_LIBS"])

    def test_condition_prevents_application(self) -> None:
        repo = PresetRepository(
            project_presets={
                "linux": {
                    "condition": "[[ {{system.os}} == 'linux' ]]",
                    "environment": {"CC": "clang"},
                },
                "windows": {
                    "condition": "[[ {{system.os}} == 'windows' ]]",
                    "environment": {"CC": "cl"},
                },
            },
            project_name="demo",
            project_org=None,
        )
        resolved = repo.resolve(["linux", "windows"], template_resolver=self.resolver)
        self.assertEqual(resolved.environment["CC"], "clang")
        self.assertNotIn("cl", resolved.environment.values())

    def test_collects_extra_args_without_duplicates(self) -> None:
        repo = PresetRepository(
            project_presets={
                "base": {
                    "extra_config_args": ["-DCONFIG_FROM_BASE", "-Dshared"],
                    "extra_build_args": ["--build-from-base", "-Dshared"],
                    "extra_install_args": ["--install-from-base", "--shared"],
                },
                "child": {
                    "extends": ["base"],
                    "extra_config_args": ["-DCONFIG_FROM_CHILD"],
                    "extra_build_args": ["--build-from-child"],
                    "extra_install_args": ["--install-from-child"],
                },
            },
            project_name="demo",
            project_org=None,
        )
        resolved = repo.resolve(["child"], template_resolver=self.resolver)
        self.assertEqual(
            set(resolved.extra_config_args),
            {
                "-DCONFIG_FROM_BASE",
                "-Dshared",
                "-DCONFIG_FROM_CHILD",
            },
        )
        self.assertEqual(
            set(resolved.extra_build_args),
            {
                "--build-from-base",
                "-Dshared",
                "--build-from-child",
            },
        )
        self.assertEqual(
            set(resolved.extra_install_args),
            {
                "--install-from-base",
                "--shared",
                "--install-from-child",
            },
        )

    def test_environment_supports_nested_references(self) -> None:
        repo = PresetRepository(
            project_presets={
                "tooling": {
                    "environment": {
                        "SDK_ROOT": "/opt/sdk",
                        "BIN_DIR": "{{env.SDK_ROOT}}/bin",
                        "PATH": "{{env.PATH}}:{{env.BIN_DIR}}",
                    }
                }
            },
            project_name="demo",
            project_org=None,
        )
        resolved = repo.resolve(["tooling"], template_resolver=self.resolver)
        self.assertEqual(resolved.environment["SDK_ROOT"], "/opt/sdk")
        self.assertEqual(resolved.environment["BIN_DIR"], "/opt/sdk/bin")
        self.assertEqual(resolved.environment["PATH"], "/usr/bin:/opt/sdk/bin")

    def test_environment_cycle_raises_error(self) -> None:
        repo = PresetRepository(
            project_presets={
                "loop": {
                    "environment": {
                        "A": "{{env.B}}",
                        "B": "{{env.A}}",
                    }
                }
            },
            project_name="demo",
            project_org=None,
        )
        with self.assertRaises(TemplateError) as exc_info:
            repo.resolve(["loop"], template_resolver=self.resolver)
        self.assertIn("Circular dependency detected", str(exc_info.exception))

    def test_definition_cycle_raises_error(self) -> None:
        repo = PresetRepository(
            project_presets={
                "loop": {
                    "definitions": {
                        "A": "{{preset.definitions.B}}",
                        "B": "{{preset.definitions.A}}",
                    }
                }
            },
            project_name="demo",
            project_org=None,
        )
        with self.assertRaises(TemplateError) as exc_info:
            repo.resolve(["loop"], template_resolver=self.resolver)
        self.assertIn("Circular dependency detected", str(exc_info.exception))

    def test_org_scoped_preset_prefers_matching_org(self) -> None:
        repo = PresetRepository(
            project_presets={
                "scoped": {
                    "org": "vendor",
                    "environment": {"CC": "clang"},
                },
                "local": {
                    "environment": {"CC": "gcc"},
                },
            },
            shared_presets=[
                {
                    "global": {"environment": {"CC": "icc"}},
                    "other": {"org": "other", "environment": {"CC": "msvc"}},
                }
            ],
            project_name="demo",
            project_org="vendor",
        )

        resolved = repo.resolve(["scoped"], template_resolver=self.resolver)
        self.assertEqual(resolved.environment["CC"], "clang")

        resolved_local = repo.resolve(["local"], template_resolver=self.resolver)
        self.assertEqual(resolved_local.environment["CC"], "gcc")

        resolved_global = repo.resolve(["global"], template_resolver=self.resolver)
        self.assertEqual(resolved_global.environment["CC"], "icc")

        fully_qualified = repo.resolve(
            ["vendor/scoped"], template_resolver=self.resolver
        )
        self.assertEqual(fully_qualified.environment["CC"], "clang")

    def test_org_scoped_requires_explicit_name_for_different_org(self) -> None:
        repo = PresetRepository(
            project_presets={},
            shared_presets=[
                {
                    "scoped": {
                        "org": "vendor",
                        "environment": {"OPT_LEVEL": "3"},
                    }
                }
            ],
            project_name="demo",
            project_org="other",
        )

        with self.assertRaises(KeyError):
            repo.resolve(["scoped"], template_resolver=self.resolver)

        resolved = repo.resolve(["vendor/scoped"], template_resolver=self.resolver)
        self.assertEqual(resolved.environment["OPT_LEVEL"], "3")
