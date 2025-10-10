from __future__ import annotations

import unittest

from builder.presets import PresetRepository
from builder.template import TemplateResolver


class PresetRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = {
            "user": {"branch": "main", "build_type": "Debug"},
            "project": {"name": "demo", "source_dir": "/src/demo", "build_dir": "/src/demo/_build"},
            "system": {"os": "linux", "architecture": "x86_64"},
            "env": {},
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
