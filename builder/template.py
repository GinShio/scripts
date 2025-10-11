"""Template and expression resolution utilities."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Sequence, List
import ast
import operator
import re


_PLACEHOLDER_PATTERN = re.compile(r"\{\{([^{}]+)\}\}")
_EXPRESSION_PATTERN = re.compile(r"^\s*\[\[(?P<expr>.*)\]\]\s*$", re.DOTALL)
_SINGLE_PLACEHOLDER_PATTERN = re.compile(r"^\s*\{\{([^{}]+)\}\}\s*$")

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
    _cache: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def resolve(self, value: Any) -> Any:
        return self._resolve_value(value, stack=[])

    def _resolve_value(self, value: Any, *, stack: list[str]) -> Any:
        if isinstance(value, str):
            return self._resolve_string(value, stack=stack)
        if isinstance(value, list):
            return [self._resolve_value(item, stack=list(stack)) for item in value]
        if isinstance(value, tuple):
            resolved_list = [self._resolve_value(item, stack=list(stack)) for item in value]
            return tuple(resolved_list)
        if isinstance(value, dict):
            return {key: self._resolve_value(val, stack=list(stack)) for key, val in value.items()}
        return value

    def _resolve_string(self, value: str, *, stack: list[str]) -> Any:
        expression_match = _EXPRESSION_PATTERN.match(value)
        if expression_match:
            expr = expression_match.group("expr")
            expr = self._substitute(expr, stack=stack, for_expression=True)
            expr = expr.strip()
            return self._evaluate_expression(expr)
        placeholder_match = _SINGLE_PLACEHOLDER_PATTERN.match(value)
        if placeholder_match:
            path = placeholder_match.group(1).strip()
            return self._resolve_path(path, stack=stack)
        substituted = self._substitute(value, stack=stack, for_expression=False)
        return substituted

    def _substitute(self, text: str, *, stack: list[str], for_expression: bool) -> str:
        def replacement(match: re.Match[str]) -> str:
            path = match.group(1).strip()
            result = self._resolve_path(path, stack=stack)
            if for_expression:
                return _to_expression_literal(result)
            if isinstance(result, (dict, list, tuple)):
                return _to_expression_literal(result)
            return str(result)

        if not _PLACEHOLDER_PATTERN.search(text):
            return text
        return _PLACEHOLDER_PATTERN.sub(replacement, text)

    def _resolve_path(self, path: str, *, stack: list[str]) -> Any:
        if path in self._cache:
            return self._cache[path]
        if path in stack:
            cycle = " -> ".join(stack + [path])
            raise TemplateError(f"Circular dependency detected: {cycle}")

        raw_value = self._lookup_raw(path)
        stack.append(path)
        resolved = self._resolve_value(raw_value, stack=stack)
        stack.pop()
        self._cache[path] = resolved
        return resolved

    def _lookup_raw(self, path: str) -> Any:
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


def extract_placeholders(value: Any) -> set[str]:
    """Collect all template placeholder paths referenced within *value*."""

    placeholders: set[str] = set()

    def _collect(obj: Any) -> None:
        if isinstance(obj, str):
            for match in _PLACEHOLDER_PATTERN.finditer(obj):
                path = match.group(1).strip()
                if path:
                    placeholders.add(path)
            return
        if isinstance(obj, Mapping):
            for item in obj.values():
                _collect(item)
            return
        if isinstance(obj, (list, tuple)):
            for item in obj:
                _collect(item)

    _collect(value)
    return placeholders


def build_dependency_map(
    mapping: Mapping[str, Any],
    *,
    prefixes: Sequence[str],
    pre_resolved: Iterable[str] | None = None,
) -> Dict[str, List[str]]:
    """Construct a dependency graph for placeholder resolution."""

    dependency_map: Dict[str, List[str]] = {str(key): [] for key in mapping.keys()}
    keys_in_scope = set(dependency_map.keys())
    pre_resolved_set = {str(key) for key in pre_resolved} if pre_resolved else set()

    for raw_key, value in mapping.items():
        key = str(raw_key)
        deps: set[str] = set()
        for placeholder in extract_placeholders(value):
            for prefix in prefixes:
                if not placeholder.startswith(prefix):
                    continue
                dep_token = placeholder[len(prefix):].strip()
                if not dep_token:
                    continue
                dep_name = dep_token.split(".", 1)[0]
                if dep_name in keys_in_scope and dep_name not in pre_resolved_set and dep_name != key:
                    deps.add(dep_name)
        dependency_map[key] = sorted(deps)
    return dependency_map


def _find_cycle(dependency_map: Mapping[str, Sequence[str]]) -> list[str]:
    visited: set[str] = set()
    active: set[str] = set()
    path: list[str] = []

    def _dfs(node: str) -> list[str] | None:
        visited.add(node)
        active.add(node)
        path.append(node)
        for dep in dependency_map.get(node, ()):  # type: ignore[arg-type]
            if dep not in dependency_map:
                continue
            if dep in active:
                try:
                    start_index = path.index(dep)
                except ValueError:
                    start_index = 0
                return path[start_index:] + [dep]
            if dep not in visited:
                result = _dfs(dep)
                if result:
                    return result
        active.remove(node)
        path.pop()
        return None

    for node in dependency_map:
        if node not in visited:
            cycle = _dfs(node)
            if cycle:
                return cycle
    return []


def topological_order(dependency_map: Mapping[str, Sequence[str]]) -> list[str]:
    """Return a topological ordering or raise TemplateError on cycles."""

    nodes = list(dependency_map.keys())
    dependents: Dict[str, set[str]] = {node: set() for node in nodes}
    indegree: Dict[str, int] = {node: 0 for node in nodes}

    for node, deps in dependency_map.items():
        filtered_deps = [dep for dep in deps if dep in dependency_map]
        indegree[node] = len(filtered_deps)
        for dep in filtered_deps:
            dependents.setdefault(dep, set()).add(node)

    ready = [node for node, degree in indegree.items() if degree == 0]
    ready.sort()
    order: list[str] = []

    while ready:
        node = ready.pop(0)
        order.append(node)
        for dependent in sorted(dependents.get(node, ())):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()

    if len(order) != len(nodes):
        cycle = _find_cycle(dependency_map)
        if cycle:
            raise TemplateError(f"Circular dependency detected: {' -> '.join(cycle)}")
        raise TemplateError("Circular dependency detected")

    return order


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
