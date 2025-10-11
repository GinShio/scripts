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
            "values": {"string_number": "42", "float_value": "3.25", "zero": 0},
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

    def test_expression_supports_basic_type_conversions(self) -> None:
        result_int = self.resolver.resolve("[[ int({{values.string_number}}) + 1 ]]")
        self.assertEqual(result_int, 43)

        result_float = self.resolver.resolve("[[ float({{values.string_number}}) / 2 ]]")
        self.assertEqual(result_float, 21.0)

        result_str = self.resolver.resolve("[[ str({{system.memory.total_gb}}) ]]")
        self.assertEqual(result_str, "16")

        result_bool = self.resolver.resolve("[[ bool({{values.zero}}) ]]")
        self.assertFalse(result_bool)

    def test_expression_disallows_unknown_functions(self) -> None:
        with self.assertRaises(TemplateError):
            self.resolver.resolve("[[ len({{project.name}}) ]]")

    def test_expression_supports_min_function(self) -> None:
        result = self.resolver.resolve("[[ min({{system.memory.total_gb}}, 32, 8) ]]")
        self.assertEqual(result, 8)

        list_context = TemplateResolver({"numbers": [5, 3, 7]})
        list_result = list_context.resolve("[[ min({{numbers}}) ]]")
        self.assertEqual(list_result, 3)

    def test_expression_supports_additional_math_functions(self) -> None:
        max_result = self.resolver.resolve("[[ max({{system.memory.total_gb}}, 4) ]]")
        self.assertEqual(max_result, 16)

        abs_context = TemplateResolver({"values": {"negative": -12}})
        abs_result = abs_context.resolve("[[ abs({{values.negative}}) ]]")
        self.assertEqual(abs_result, 12)

        round_context = TemplateResolver({"values": {"pi": 3.14159}})
        round_result = round_context.resolve("[[ round({{values.pi}}, 2) ]]")
        self.assertEqual(round_result, 3.14)

        sum_context = TemplateResolver({"values": {"items": [1, 2, 3]}})
        sum_result = sum_context.resolve("[[ sum({{values.items}}) ]]")
        self.assertEqual(sum_result, 6)
        sum_with_start = sum_context.resolve("[[ sum({{values.items}}, 5) ]]")
        self.assertEqual(sum_with_start, 11)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
