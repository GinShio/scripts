#!/usr/bin/env python3
"""
gputest - GPU Test Automation Tool

Refactored implementation of the GPU testing scripts.
"""
import argparse
import datetime
import glob
import os
import platform
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Add project root to path to allow importing core
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    from core.command_runner import SubprocessCommandRunner
    from core.config_loader import load_config_file
    from gputest.src.context import Context, Console, DryRunCommandRunner
    from gputest.src.toolbox import run_toolbox
    from gputest.src.runner import run_tests
    from gputest.src.restore import run_restore
    from gputest.src.cleanup import run_cleanup
    from gputest.src.list_cmd import run_list
    from gputest.src.utils import deep_merge
except ImportError:
    # Fallback for when running standalone or during development if paths
    # aren't set
    print(
        "Warning: Could not import core modules. Ensure PYTHONPATH is set correctly.",
        file=sys.stderr)
    sys.exit(1)

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="GPU Test Automation Tool")
    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        help="Path to configuration file or directory")
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show what would be done without doing it")
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output (legacy, maps to debug)")
    parser.add_argument(
        "--log",
        "-l",
        choices=["none", "error", "info", "debug"],
        default="none",
        help="Set log level (default: none)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Install (Toolbox)
    p_install = subparsers.add_parser(
        "install", help="Install test suites (Toolbox)")
    p_install.add_argument(
        "targets",
        nargs="*",
        help="Specific suites to install (default: all)")

    # Run (Test Runner)
    p_run = subparsers.add_parser("run", help="Run tests")
    p_run.add_argument(
        "tests",
        nargs="+",
        help="Names of tests to run (from [tests] in config)")

    # Restore
    p_restore = subparsers.add_parser(
        "restore", help="Restore baseline results")
    p_restore.add_argument(
        "--days",
        type=int,
        default=10,
        help="Number of days to look back")

    # Cleanup
    p_cleanup = subparsers.add_parser("cleanup", help="Cleanup old results")

    # List
    p_list = subparsers.add_parser("list", help="List drivers or suites")
    p_list.add_argument(
        "target",
        choices=["drivers", "driver", "suites", "suite"],
        help="Target to list (drivers or suites)")
    p_list.add_argument(
        "name",
        nargs="?",
        help="Specific driver or suite name")

    args = parser.parse_args()

    # Determine log level: explicit --log takes precedence, otherwise --verbose maps to debug
    if args.log and args.log != "none":
        log_level = args.log
    else:
        log_level = "debug" if args.verbose else "none"

    console = Console(level=log_level, dry_run=args.dry_run)

    # Determine config path: CLI > Env > Default
    config_path = args.config
    if config_path is None:
        env_config_dir = os.environ.get("GPUTEST_CONFIG_DIR")
        if env_config_dir:
            config_path = Path(env_config_dir)
            console.info(
                f"Using configuration from GPUTEST_CONFIG_DIR: {config_path}")
        else:
            # Default to the config directory so multiple small TOML files
            # can be loaded and merged (easier maintenance).
            config_path = Path("gputest/config")

    if not config_path.exists():
        console.error(f"Configuration path not found: {config_path}")
        sys.exit(1)

    config = {}
    try:
        if config_path.is_dir():
            # Load all .toml files in directory
            config_files = sorted(config_path.glob("*.toml"))
            if not config_files:
                console.error(
                    f"No .toml configuration files found in: {config_path}")
                sys.exit(1)

            for cf in config_files:
                console.debug(f"Loading config file: {cf}")
                file_config = load_config_file(cf)
                deep_merge(config, file_config)
        else:
            config = load_config_file(config_path)

    except Exception as e:
        console.error(f"Failed to load config: {e}")
        sys.exit(1)

    # Global settings
    global_cfg = config.get("global", {})
    console.debug(global_cfg)
    project_root = Path(
        os.path.expanduser(
            global_cfg.get(
                "project_root",
                "~/Projects"))).resolve()
    runner_root = Path(
        os.path.expanduser(
            global_cfg.get(
                "runner_root",
                "/run/user/1000/runner"))).resolve()
    result_dir = Path(
        os.path.expanduser(
            global_cfg.get(
                "result_dir",
                "~/Public/result"))).resolve()

    if args.dry_run:
        runner = DryRunCommandRunner()
    else:
        runner = SubprocessCommandRunner()

    ctx = Context(
        config=config,
        console=console,
        runner=runner,
        project_root=project_root,
        runner_root=runner_root,
        result_dir=result_dir
    )

    # Ensure directories exist (unless dry run)
    if not args.dry_run:
        runner_root.mkdir(parents=True, exist_ok=True)
        result_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "install":
        run_toolbox(ctx, args.targets)
    elif args.command == "run":
        run_tests(ctx, args.tests)
    elif args.command == "restore":
        run_restore(ctx, args.days)
    elif args.command == "cleanup":
        run_cleanup(ctx)
    elif args.command == "list":
        run_list(ctx, args.target, args.name)


if __name__ == "__main__":
    main()
