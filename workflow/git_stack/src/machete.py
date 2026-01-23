"""Machete file parser and utilities."""

from __future__ import annotations

import os
from typing import Dict, Iterable, List, Optional, Union

from .git import get_git_dir


def get_machete_file_path() -> str:
    return os.path.join(get_git_dir(), "machete")


class MacheteNode:
    def __init__(self, name: str, indent: int = 0, annotation: str = ""):
        self.name = name
        self.indent = indent
        self.annotation = annotation
        self.parent: Optional[MacheteNode] = None
        self.children: List[MacheteNode] = []

    def __repr__(self) -> str:
        return f"<MacheteNode {self.name} (indent={self.indent})>"


def parse_machete() -> Dict[str, MacheteNode]:
    """
    Parse .git/machete file into a dictionary of branch_name -> Node.
    The nodes are linked (parent/children).
    """
    path = get_machete_file_path()
    if not os.path.exists(path):
        return {}

    with open(path, "r") as f:
        lines = [line.rstrip() for line in f.readlines()]

    nodes: Dict[str, MacheteNode] = {}
    last_node_at_indent: Dict[int, MacheteNode] = {}

    # We assume standard 4-space indentation or tabs, but the logic
    # should essentially respect relative indentation increase.
    # To be robust, let's just use raw whitespace length logic.

    for line in lines:
        if not line.strip():
            continue

        lstripped = line.lstrip()
        raw_indent = len(line) - len(lstripped)

        # Split name and annotation
        parts = lstripped.split(maxsplit=1)
        name = parts[0]
        annotation = parts[1] if len(parts) > 1 else ""

        node = MacheteNode(name, raw_indent, annotation)
        nodes[name] = node

        # Find parent: closest previous node with strictly less indent
        parent: Optional[MacheteNode] = None

        # Look backwards in valid indents
        possible_indents = sorted(
            [i for i in last_node_at_indent.keys() if i < raw_indent], reverse=True
        )
        if possible_indents:
            parent = last_node_at_indent[possible_indents[0]]

        if parent:
            node.parent = parent
            parent.children.append(node)

        last_node_at_indent[raw_indent] = node

        # Clear deeper indents as they are no longer candidates for parents of future lines
        keys_to_remove = [k for k in last_node_at_indent.keys() if k > raw_indent]
        for k in keys_to_remove:
            del last_node_at_indent[k]

    return nodes


def get_roots(nodes: Dict[str, MacheteNode]) -> List[MacheteNode]:
    """Return all root nodes (nodes with no parent)."""
    return [n for n in nodes.values() if n.parent is None]


def write_machete(nodes: Union[Iterable[MacheteNode], Dict[str, MacheteNode]]) -> None:
    """
    Write a list of proper MacheteNodes (roots) to .git/machete.
    It performs a traverse to print children accordingly.
    """
    lines = []

    roots = []
    if isinstance(nodes, dict):
        roots = get_roots(nodes)
    else:
        # Avoid consuming an iterator twice or incorrectly
        node_list = list(nodes)
        # If the input list contains children, we should only iterate roots to avoid duplication
        # assuming the nodes structure is linked correctly.
        # But if it's a flat list of *unlinked* nodes, this logic fails.
        # We assume linked structure.
        roots = [n for n in node_list if n.parent is None]

    def _traverse(node: MacheteNode, indent_level: int = 0):
        indent_str = "    " * indent_level
        suffix = f" {node.annotation}" if node.annotation else ""
        lines.append(f"{indent_str}{node.name}{suffix}")
        for child in node.children:
            _traverse(child, indent_level + 1)

    for root in roots:
        _traverse(root)

    path = get_machete_file_path()
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def get_linear_stack(
    current_branch: str, nodes: Dict[str, MacheteNode]
) -> List[MacheteNode]:
    """
    Get the linear stack for the current branch.
    This includes:
    1. All ancestors (Root -> ... -> Parent)
    2. The current branch
    3. The primary downstream chain (First child -> First child's first child -> ...)
    """
    if current_branch not in nodes:
        return []

    current = nodes[current_branch]

    # Trace specific lineage (Ancestors)
    ancestors = []
    p = current.parent
    while p:
        ancestors.append(p)
        p = p.parent
    ancestors.reverse()

    # Trace primary descendants (First child only)
    descendants = []
    c = current
    while c.children:
        # Heuristic: Pick the first child as the "main" continuation
        c = c.children[0]
        descendants.append(c)

    return ancestors + [current] + descendants


# ----------------------------------------------------------------------
# Visualization / Annotation Helpers
# ----------------------------------------------------------------------

# {'node': MacheteNode, 'pr_num': str}
StackItem = Dict[str, Union[MacheteNode, str]]

STACK_HEADER = "<!-- start git-stack-sync generated -->"
STACK_FOOTER = "<!-- end git-stack-sync generated -->"


def generate_nested_list(
    stack: List[Dict[str, Any]], current_focused_branch: str, item_label: str = "PR"
) -> str:
    """
    Generate ASCII nested list for the stack.
    stack items: {'node': MacheteNode, 'pr_num': str}
    """
    lines = [STACK_HEADER, "", "### 🥞 Stack", ""]

    if not stack:
        return ""

    total_items = len(stack)

    for i, item in enumerate(stack):
        node = item["node"]
        pr_num = item["pr_num"]

        indent = "  " * i

        is_current = node.name == current_focused_branch
        highlight = f" 👈 **(THIS {item_label})**" if is_current else ""

        index_str = f"[{i + 1}/{total_items}]"

        line_pr = f"{indent}* **{index_str} {item_label} #{pr_num}**{highlight}"

        parent_name = node.parent.name if node.parent else "?"
        line_flow = f"{indent}  `{parent_name}` ← `{node.name}`"

        lines.append(line_pr)
        lines.append(line_flow)
        lines.append("")

    lines.append(STACK_FOOTER)
    return "\n".join(lines)


def strip_existing_stack_block(body: str) -> str:
    """Remove existing git-stack block."""
    import re

    if STACK_HEADER in body:
        body = re.sub(
            rf"{re.escape(STACK_HEADER)}.*?{re.escape(STACK_FOOTER)}",
            "",
            body,
            flags=re.DOTALL,
        )
    return body.strip()


def extract_pr_number(annotation: str) -> Optional[str]:
    """Assuming format 'PR #123 (author) ...', extract '123'."""
    import re

    match = re.search(r"PR #(\d+)", annotation)
    if match:
        return match.group(1)
    return None
