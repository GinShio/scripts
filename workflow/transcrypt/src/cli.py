from __future__ import annotations

#
# transcrypt - https://github.com/elasticdog/transcrypt
#
# A script to configure transparent encryption of sensitive files stored in
# a Git repository. It utilizes OpenSSL's symmetric cipher routines and follows
# the gitattributes(5) man page regarding the use of filters.
#
# Copyright (c) 2019-2025 James Murty <james@murty.co>
# Copyright (c) 2014-2020 Aaron Bull Schaefer <aaron@elasticdog.com>
#
# Add PR https://github.com/elasticdog/transcrypt/pull/132 for salt and pbkdf2
# Copyright (c) 2022-2025 Jon Crall <jon.crall@kitware.com>
#
# This source code is provided under the terms of the MIT License
# that can be be found in the LICENSE file.
#
# --------------------------------------------------------------------------------
# Ported to Python by GinShio
# Copyright (c) 2026 GinShio
# --------------------------------------------------------------------------------
#
import argparse
import sys
from typing import List

from . import actions


def main(args: List[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Transparent encryption for git (Python version of transcrypt)"
    )

    # Global arguments
    parser.add_argument(
        "-c",
        "--context",
        default="default",
        help="Encryption context (default: 'default')",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )

    subparsers = parser.add_subparsers(
        dest="command", required=True, help="Command to run"
    )

    # Internal Commands (Hidden from help)
    # These are called by git, not the user.
    clean_parser = subparsers.add_parser("clean", help=argparse.SUPPRESS)
    clean_parser.add_argument("file", nargs="?", help="File path being processed")

    smudge_parser = subparsers.add_parser("smudge", help=argparse.SUPPRESS)
    smudge_parser.add_argument("file", nargs="?", help="File path being processed")

    textconv_parser = subparsers.add_parser("textconv", help=argparse.SUPPRESS)
    textconv_parser.add_argument("file", help="File to convert")

    # Configure (Deprecated/Removed, directs user to git config)
    subparsers.add_parser("configure", help=argparse.SUPPRESS)

    # User Commands

    # Install
    install_parser = subparsers.add_parser(
        "install",
        help="Install git filters locally",
        description="Registers the clean/smudge filters in local git config. Run this after setting up password/cipher.",
    )

    # Uninstall
    uninstall_parser = subparsers.add_parser("uninstall", help="Remove git filters")

    # Status
    status_parser = subparsers.add_parser(
        "status", help="Show current configuration status"
    )

    parsed_args = parser.parse_args(args)

    try:
        if parsed_args.command == "clean":
            actions.clean(context=parsed_args.context, file_path=parsed_args.file)
        elif parsed_args.command == "smudge":
            actions.smudge(context=parsed_args.context, file_path=parsed_args.file)
        elif parsed_args.command == "configure":
            actions.configure(context=parsed_args.context)
        elif parsed_args.command == "install":
            actions.install(context=parsed_args.context)
        elif parsed_args.command == "uninstall":
            actions.uninstall(context=parsed_args.context)
        elif parsed_args.command == "textconv":
            actions.textconv(file_path=parsed_args.file, context=parsed_args.context)
        elif parsed_args.command == "status":
            actions.status(context=parsed_args.context)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0
