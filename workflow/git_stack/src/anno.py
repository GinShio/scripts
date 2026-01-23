"""Annotation command logic."""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from core.template import TemplateResolver

from .git import resolve_base_branch
from .machete import (STACK_FOOTER, STACK_HEADER, MacheteNode,
                      generate_nested_list, get_linear_stack, parse_machete,
                      strip_existing_stack_block, write_machete)
from .platform import PlatformInterface, get_platform


def resolve_template_str(template: str, context: Dict[str, Any]) -> str:
    """Helper to resolve a single template string using core.TemplateResolver."""
    resolver = TemplateResolver(context)
    return str(resolver.resolve(template))


def annotate_stack(limit_to_branch: Optional[str] = None) -> None:
    """
    Main entry point for stack annotation.
    1. Detect Platform
    2. Traverse Machete Tree (get valid branches)
    3. Fetch PR info for ALL branches in stack
    4. Construct Table
    5. Update PR descriptions

    Args:
        limit_to_branch: Only annotate branches in the same stack as this branch.
    """
    platform = get_platform()

    if not platform:
        print("Error: Could not detect platform or authorization failed.")
        sys.exit(1)

    label_type = platform.get_item_label()  # "PR" or "MR"

    # 1. Parse full tree
    nodes = parse_machete()
    if not nodes:
        print("No .git/machete definition found.")
        return

    # Filter targets if limiting
    targets = list(nodes.values())

    if limit_to_branch:
        if limit_to_branch not in nodes:
            print(f"Branch '{limit_to_branch}' not found. Cannot limit scope.")
            sys.exit(1)

        targets = get_linear_stack(limit_to_branch, nodes)
        print(
            f"Limiting annotation to linear stack: {[n.name for n in targets]}")

    # 2. Collect PR info and update local annotations
    # We iterate all nodes that are not roots (roots usually don't have PRs in this context)
    all_nodes = targets  # Was list(nodes.values())
    pr_cache: Dict[str, Any] = {}
    updates_made = False

    print("Fetching PR/MR status...")

    for node in all_nodes:
        # Skip roots if they are typically 'main' without PRs
        if not node.parent:
            continue

        try:
            data = None
            data = platform.get_mr(node.name)

            if data:
                pr_cache[node.name] = data
                # Update machete annotation
                num = str(data.get('number') or data.get('iid'))

                # Use resolve_template for annotation format
                # e.g. "PR #123"
                context = {"type": label_type, "number": num}
                new_anno = resolve_template_str(
                    "{{type}} #{{number}}", context)

                if node.annotation != new_anno:
                    node.annotation = new_anno
                    updates_made = True
                    # Optimization: Don't print every single update if many
                    # print(f"  Updated {node.name} -> {new_anno}")
        except Exception as e:
            print(f"  Failed to fetch {node.name}: {e}")

    if updates_made:
        print("Updating .git/machete with new numbers...")
        write_machete(nodes)

    # 3. Update Remote PR Descriptions
    print("Updating remote descriptions...")

    for node in all_nodes:
        if node.name not in pr_cache:
            continue

        pr_data = pr_cache[node.name]
        pr_num = str(pr_data.get('number') or pr_data.get('iid'))

        # Build stack context
        stack_nodes = get_linear_stack(node.name, nodes)

        # Convert to StackItem list
        stack_items = []
        for sn in stack_nodes:
            p_num = "?"
            if sn.name in pr_cache:
                p_data = pr_cache[sn.name]
                p_num = str(p_data.get('number') or p_data.get('iid'))
            elif sn.parent is None:
                # Root
                p_num = "-"
            else:
                # No PR found
                p_num = "?"

            stack_items.append({'node': sn, 'pr_num': p_num})

        # Generate Table
        table = generate_nested_list(stack_items, node.name, label_type)

        # Fetch current description
        curr_desc = platform.get_mr_description(pr_num)
        if curr_desc is None:
            curr_desc = ""

        # Strip old block
        clean_desc = strip_existing_stack_block(curr_desc)

        # Append new block
        # Ensure spacing
        if clean_desc and not clean_desc.endswith('\n'):
            clean_desc += "\n"
        if clean_desc and not clean_desc.endswith('\n\n'):
            clean_desc += "\n"

        new_desc = clean_desc + table

        if new_desc.strip() != curr_desc.strip():
            # Use template for log message
            log_ctx = {"type": label_type,
                       "number": pr_num, "branch": node.name}
            msg = resolve_template_str(
                "  Updating {{type}} #{{number}} ({{branch}})...", log_ctx)
            print(msg)
            platform.update_mr_description(pr_num, new_desc)

    print("Stack annotation complete! 🥞")
