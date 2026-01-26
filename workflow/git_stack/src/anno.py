"""Annotation command logic."""

from __future__ import annotations

import re
import sys
from typing import Any, Dict, List, Optional

from .git import resolve_base_branch
from .machete import (
    STACK_FOOTER,
    STACK_HEADER,
    MacheteNode,
    format_stack_markdown,
    get_linear_stack,
    parse_machete,
    strip_existing_stack_block,
    write_machete,
)
from .platform import PlatformInterface, get_platform


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
    label_char = platform.get_item_char()  # "#" or "!"

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
        print(f"Limiting annotation to linear stack: {[n.name for n in targets]}")

    # 2. Collect PR info and update local annotations
    # We iterate all nodes that are not roots (roots usually don't have PRs in this context)
    all_nodes = targets  # Was list(nodes.values())
    pr_cache: Dict[str, Any] = {}
    updates_made = False

    print("Fetching PR/MR status...")

    # Parallelize get_mr calls since they are independent network IO.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    get_mr_futures = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        for node in all_nodes:
            if not node.parent:
                continue
            # Submit network call
            get_mr_futures[executor.submit(platform.get_mr, node.name)] = node

        for future in as_completed(get_mr_futures):
            node = get_mr_futures[future]
            try:
                data = future.result()
                if data:
                    pr_cache[node.name] = data
                    # Update machete annotation
                    num = str(data.get("number") or data.get("iid"))
                    new_anno = f"{label_type} {label_char}{num}"
                    if node.annotation != new_anno:
                        node.annotation = new_anno
                        updates_made = True
            except Exception as e:
                print(f"  Failed to fetch {node.name}: {e}")

    if updates_made:
        print("Updating .git/machete with new numbers...")
        write_machete(nodes)

    # 3. Update Remote PR Descriptions
    print("Updating remote descriptions...")

    # 3a. Parallel fetch of current descriptions for all PRs we found.
    desc_futures = {}
    curr_desc_map: Dict[str, str] = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=8) as executor:
        for node_name, data in pr_cache.items():
            pr_num = str(data.get("number") or data.get("iid"))
            desc_futures[executor.submit(platform.get_mr_description, pr_num)] = (
                node_name,
                pr_num,
            )

        for future in as_completed(desc_futures):
            node_name, pr_num = desc_futures[future]
            try:
                curr = future.result()
                if curr is None:
                    curr = ""
                curr_desc_map[pr_num] = curr
            except Exception as e:
                print(
                    f"  Failed to fetch description for {node_name} ({label_char}{pr_num}): {e}"
                )
                curr_desc_map[pr_num] = ""

    # 3b. Build new descriptions for each PR (using cached pr info and fetched descriptions)
    updates: Dict[str, str] = {}
    for node in all_nodes:
        if node.name not in pr_cache:
            continue

        pr_data = pr_cache[node.name]
        pr_num = str(pr_data.get("number") or pr_data.get("iid"))

        # Build stack context
        stack_nodes = get_linear_stack(node.name, nodes)

        # Convert to StackItem list
        stack_items = []
        for sn in stack_nodes:
            p_num = "?"
            if sn.name in pr_cache:
                p_data = pr_cache[sn.name]
                p_num = str(p_data.get("number") or p_data.get("iid"))
            elif sn.parent is None:
                p_num = "-"
            else:
                p_num = "?"

            stack_items.append({"node": sn, "pr_num": p_num})

        # Generate Table
        table = format_stack_markdown(stack_items, node.name, label_type, label_char)

        # Use previously fetched current description
        curr_desc = curr_desc_map.get(pr_num, "") or ""

        # If there is an existing generated stack block, inspect it.
        existing_block_match = re.search(
            rf"{re.escape(STACK_HEADER)}.*?{re.escape(STACK_FOOTER)}",
            curr_desc,
            flags=re.DOTALL,
        )
        if existing_block_match:
            block = existing_block_match.group(0)
            old_names = re.findall(r"`([A-Za-z0-9._/-]+)`", block)
            current_names = [sn.name for sn in stack_nodes]
            missing = [n for n in old_names if n not in current_names]
            added = [n for n in current_names if n not in old_names]
            if missing and not added:
                print(
                    f"  Skipping update for {label_type} {label_char}{pr_num} ({node.name}) - existing stack contains removed branches: {missing}"
                )
                continue

        clean_desc = strip_existing_stack_block(curr_desc)

        # Ensure spacing
        if clean_desc and not clean_desc.endswith("\n"):
            clean_desc += "\n"
        if clean_desc and not clean_desc.endswith("\n\n"):
            clean_desc += "\n"

        new_desc = clean_desc + table

        if new_desc.strip() != curr_desc.strip():
            updates[pr_num] = new_desc

    # 3c. Perform updates in parallel
    if updates:
        with ThreadPoolExecutor(max_workers=8) as executor:
            fut_map = {}
            for pr_num, new_desc in updates.items():
                # Find node/branch for logging context (best-effort)
                branch = next(
                    (
                        n
                        for n, d in pr_cache.items()
                        if str(d.get("number") or d.get("iid")) == pr_num
                    ),
                    "",
                )
                print(f"  Updating {label_type} {label_char}{pr_num} ({branch})...")
                fut_map[
                    executor.submit(platform.update_mr_description, pr_num, new_desc)
                ] = pr_num

            for future in as_completed(fut_map):
                pr_num = fut_map[future]
                try:
                    future.result()
                except Exception as e:
                    print(
                        f"  Failed to update description for {label_type} {label_char}{pr_num}: {e}"
                    )

    print("Stack annotation complete! 🥞")
