from __future__ import annotations

import unittest

from builder.template import TemplateResolver


class TemplateResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = {
            "user": {"branch": "main", "build_type": "Release"},
            "project": {"name": "demo", "source_dir": "/src/demo", "build_dir": "_build"},
            "system": {"os": "linux", "architecture": "x86_64", "memory": {"total_gb": 16}},
            "env": {"CC": "clang"},
        }
        self.resolver = TemplateResolver(self.context)

    def test_resolve_placeholder(self) -> None:
        result = self.resolver.resolve("{{project.name}}-{{user.branch}}")
        self.assertEqual(result, "demo-main")

    def test_resolve_expression(self) -> None:
        result = self.resolver.resolve("[[ {{system.memory.total_gb}} // 2 ]]")
        self.assertEqual(result, 8)

    def test_resolve_condition_expression(self) -> None:
        result = self.resolver.resolve("[[ {{system.os}} == 'linux' ]]")
        self.assertTrue(result)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
