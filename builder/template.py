"""Template and expression resolution utilities."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import ast
import operator
import re


_PLACEHOLDER_PATTERN = re.compile(r"\{\{([^{}]+)\}\}")
_EXPRESSION_PATTERN = re.compile(r"^\s*\[\[(?P<expr>.*)\]\]\s*$", re.DOTALL)

_ALLOWED_BIN_OPS: dict[type[ast.AST], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}

_ALLOWED_BOOL_OPS: dict[type[ast.AST], Any] = {
    ast.And: all,
    ast.Or: any,
}

_ALLOWED_UNARY_OPS: dict[type[ast.AST], Any] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
    ast.Invert: operator.invert,
}

_ALLOWED_COMPARISONS: dict[type[ast.AST], Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


class TemplateError(ValueError):
    """Raised when template resolution fails."""


@dataclass(slots=True)
class TemplateResolver:
    """Resolves templates and expressions using a nested mapping context."""

    context: Mapping[str, Any]

    def resolve(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._resolve_string(value)
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.resolve(list(value)))
        if isinstance(value, dict):
            return {key: self.resolve(val) for key, val in value.items()}
        return value

    def _resolve_string(self, value: str) -> Any:
        expression_match = _EXPRESSION_PATTERN.match(value)
        if expression_match:
            expr = expression_match.group("expr")
            expr = self._substitute(expr, for_expression=True)
            expr = expr.strip()
            return self._evaluate_expression(expr)
        substituted = self._substitute(value, for_expression=False)
        return substituted

    def _substitute(self, text: str, *, for_expression: bool) -> str:
        def replacement(match: re.Match[str]) -> str:
            path = match.group(1).strip()
            result = self._resolve_path(path)
            if for_expression:
                return _to_expression_literal(result)
            if isinstance(result, (dict, list, tuple)):
                return _to_expression_literal(result)
            return str(result)

        return _PLACEHOLDER_PATTERN.sub(replacement, text)

    def _resolve_path(self, path: str) -> Any:
        current: Any = self.context
        for part in path.split("."):
            if isinstance(current, Mapping) and part in current:
                current = current[part]
                continue
            if isinstance(current, (list, tuple)):
                try:
                    index = int(part)
                except ValueError as exc:  # pragma: no cover - defensive guard
                    raise TemplateError(f"List index must be an integer for path '{path}'") from exc
                try:
                    current = current[index]
                except IndexError as exc:  # pragma: no cover - defensive guard
                    raise TemplateError(f"Index {index} out of range for path '{path}'") from exc
                continue
            raise TemplateError(f"Cannot resolve path '{path}' in template context")
        return current

    def _evaluate_expression(self, expression: str) -> Any:
        try:
            node = ast.parse(expression, mode="eval")
        except SyntaxError as exc:  # pragma: no cover - invalid syntax guard
            raise TemplateError(f"Invalid expression syntax: {expression}") from exc
        return _ExpressionEvaluator().visit(node)


class _ExpressionEvaluator(ast.NodeVisitor):
    def visit(self, node: ast.AST) -> Any:  # type: ignore[override]
        if isinstance(node, ast.Expression):
            return self.visit(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in {"True", "False", "None"}:
                return {"True": True, "False": False, "None": None}[node.id]
            raise TemplateError(f"Name '{node.id}' is not allowed in expressions")
        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in _ALLOWED_BIN_OPS:
                raise TemplateError(f"Operator '{op_type.__name__}' is not allowed")
            left = self.visit(node.left)
            right = self.visit(node.right)
            return _ALLOWED_BIN_OPS[op_type](left, right)
        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in _ALLOWED_UNARY_OPS:
                raise TemplateError(f"Unary operator '{op_type.__name__}' is not allowed")
            operand = self.visit(node.operand)
            return _ALLOWED_UNARY_OPS[op_type](operand)
        if isinstance(node, ast.BoolOp):
            op_type = type(node.op)
            if op_type not in _ALLOWED_BOOL_OPS:
                raise TemplateError(f"Boolean operator '{op_type.__name__}' is not allowed")
            values = [bool(self.visit(value)) for value in node.values]
            if op_type is ast.And:
                return all(values)
            if op_type is ast.Or:
                return any(values)
        if isinstance(node, ast.Compare):
            left = self.visit(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                op_type = type(op)
                if op_type not in _ALLOWED_COMPARISONS:
                    raise TemplateError(f"Comparison operator '{op_type.__name__}' is not allowed")
                right = self.visit(comparator)
                if not _ALLOWED_COMPARISONS[op_type](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            condition = self.visit(node.test)
            return self.visit(node.body if condition else node.orelse)
        if isinstance(node, ast.List):
            return [self.visit(element) for element in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(self.visit(element) for element in node.elts)
        if isinstance(node, ast.Dict):
            keys = [self.visit(key) for key in node.keys]
            values = [self.visit(value) for value in node.values]
            return dict(zip(keys, values))
        raise TemplateError(f"Expression node '{type(node).__name__}' is not allowed")


def _to_expression_literal(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, (int, float, bool)):
        return repr(value)
    if value is None:
        return "None"
    return repr(value)
