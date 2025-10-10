from __future__ import annotations

import unittest

from unittest.mock import patch

from builder.template import TemplateError, TemplateResolver


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

    def test_nested_variable_resolution(self) -> None:
        context = {
            "user": {"branch": "dev", "build_type": "Debug"},
            "project": {
                "name": "demo",
                "build_dir": "_build/{{user.branch}}_{{user.build_type}}",
                "install_dir": "{{project.build_dir}}",
            },
        }
        resolver = TemplateResolver(context)
        self.assertEqual(resolver.resolve("{{project.install_dir}}"), "_build/dev_Debug")

    def test_expression_dependencies_and_caching(self) -> None:
        context = {
            "variables": {"base": 5},
            "expressions": {
                "double": "[[ {{variables.base}} * 2 ]]",
                "quad": "[[ {{expressions.double}} * 2 ]]",
            },
        }
        resolver = TemplateResolver(context)
        self.assertEqual(resolver.resolve("{{expressions.quad}}"), 20)
        with patch.object(TemplateResolver, "_evaluate_expression", wraps=TemplateResolver._evaluate_expression) as spy:
            first = resolver.resolve("{{expressions.double}}")
            second = resolver.resolve("{{expressions.double}}")
        self.assertEqual(first, 10)
        self.assertEqual(second, 10)
        self.assertEqual(spy.call_count, 0)

    def test_cycle_detection(self) -> None:
        context = {
            "variables": {
                "alpha": "{{variables.beta}}",
                "beta": "{{variables.alpha}}",
            }
        }
        resolver = TemplateResolver(context)
        with self.assertRaises(TemplateError):
            resolver.resolve("{{variables.alpha}}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
