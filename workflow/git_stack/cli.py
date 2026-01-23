"""CLI entry point for git-stack tools."""
import argparse
import sys

from .src.anno import annotate_stack
from .src.git import get_current_branch, resolve_base_branch
from .src.slice import (apply_slice, get_stack_commits,
                        launch_interactive_editor)
from .src.sync import sync_stack


def main():
    parser = argparse.ArgumentParser(description="Git stack workflow tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Slice command
    slice_parser = subparsers.add_parser(
        "slice", help="Slice the current stack into PRs")
    slice_parser.add_argument(
        "--base", help="Base branch (default: inferred or main/master)")

    # Sync command
    sync_parser = subparsers.add_parser(
        "sync", help="Sync local branches to remote and manage PRs")
    sync_parser.add_argument(
        "--dry-run", action="store_true", help="Do not push or create PRs")
    sync_parser.add_argument(
        "--pr", action="store_true", help="Create/Update PRs on remote")
    sync_parser.add_argument(
        "--no-push", action="store_true", help="Do not push code/branches")
    sync_parser.add_argument(
        "--all", action="store_true", help="Sync ALL stacks in .git/machete (default: current stack only)")

    # Create command (Legacy/Alias)
    create_parser = subparsers.add_parser(
        "create", help="[Deprecated] Create or update PRs for the stack (Use sync --pr --no-push)")
    create_parser.add_argument(
        "--dry-run", action="store_true", help="Do not create PRs")

    # Anno command
    anno_parser = subparsers.add_parser(
        "anno", help="Annotate PR descriptions with stack navigation")
    anno_parser.add_argument(
        "--all", action="store_true", help="Annotate ALL stacks in .git/machete (default: current stack only)")

    args = parser.parse_args()

    if args.command == "slice":
        try:
            # Use shared resolution logic (handles config, main/master fallback)
            base = resolve_base_branch(args.base)

            commits = get_stack_commits(base)
            if not commits:
                print(f"No commits found between {base} and HEAD.")
                sys.exit(0)

            mapping = launch_interactive_editor(base, commits)
            if mapping:
                apply_slice(base, mapping)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)

    elif args.command == "sync":
        push = not args.no_push
        pr = args.pr

        limit_to = None
        if not args.all:
            curr = get_current_branch()
            if curr:
                limit_to = curr
            else:
                print("Warning: Could not detect current branch. Syncing all stacks.")

        sync_stack(push=push, pr=pr, dry_run=args.dry_run,
                   limit_to_branch=limit_to)

    elif args.command == "create":
        # Equivalent to sync --pr --no-push
        sync_stack(push=False, pr=True, dry_run=args.dry_run)

    elif args.command == "anno":
        limit_to = None
        if not args.all:
            curr = get_current_branch()
            if curr:
                limit_to = curr
            else:
                print("Warning: Could not detect current branch. Annotating all stacks.")

        annotate_stack(limit_to_branch=limit_to)


if __name__ == "__main__":
    main()
