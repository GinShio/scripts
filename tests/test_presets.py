from __future__ import annotations

import unittest

from builder.presets import PresetRepository
from builder.template import TemplateResolver, TemplateError


class PresetRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = {
            "user": {"branch": "main", "build_type": "Debug"},
            "project": {"name": "demo", "source_dir": "/src/demo", "build_dir": "/src/demo/_build"},
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
            }
        )
        resolved = repo.resolve(["dev"], template_resolver=self.resolver)
        self.assertEqual(resolved.environment["CC"], "clang")
        self.assertEqual(resolved.environment["CFLAGS"], "-O0")
        self.assertTrue(resolved.definitions["BUILD_SHARED_LIBS"])

    def test_condition_prevents_application(self) -> None:
        repo = PresetRepository(
            project_presets={
                "linux": {"condition": "[[ {{system.os}} == 'linux' ]]", "environment": {"CC": "clang"}},
                "windows": {"condition": "[[ {{system.os}} == 'windows' ]]", "environment": {"CC": "cl"}},
            }
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
                },
                "child": {
                    "extends": ["base"],
                    "extra_config_args": ["-DCONFIG_FROM_CHILD"],
                    "extra_build_args": ["--build-from-child"],
                },
            }
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
            }
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
            }
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
            }
        )
        with self.assertRaises(TemplateError) as exc_info:
            repo.resolve(["loop"], template_resolver=self.resolver)
        self.assertIn("Circular dependency detected", str(exc_info.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
