"""
Context and Console classes for gputest.
"""
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from core.command_runner import SubprocessCommandRunner, CommandResult
from core.archive import ArchiveConsole


class Console(ArchiveConsole):
    """Simple console output handler with configurable log level.

    Levels: none < error < info < debug
    Default: 'none' (no output)
    """

    LEVELS = {
        "none": 0,
        "error": 1,
        "info": 2,
        "debug": 3,
    }

    def __init__(self, level: str = "none", dry_run: bool = False):
        self.level_name = level
        self.level = self.LEVELS.get(level, 0)
        self.dry_run = dry_run

    def info(self, message: str) -> None:
        if self.level >= self.LEVELS["info"]:
            print(f"[INFO] {message}")

    def error(self, message: str) -> None:
        if self.level >= self.LEVELS["error"]:
            print(f"[ERROR] {message}", file=sys.stderr)

    def dry(self, message: str) -> None:
        if self.dry_run:
            print(f"[DRY] {message}")

    def debug(self, message: str) -> None:
        if self.level >= self.LEVELS["debug"]:
            print(f"[DEBUG] {message}")


@dataclass
class Context:
    config: Dict[str, Any]
    console: Console
    runner: SubprocessCommandRunner
    project_root: Path
    runner_root: Path
    result_dir: Path


class DryRunCommandRunner(SubprocessCommandRunner):
    """Command runner that prints commands instead of executing them."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        note: str | None = None,
        stream: bool = False,
    ) -> "CommandResult":
        cmd_str = self.format_command(command)
        print(f"[DRY] {cmd_str}")
        if cwd:
            print(f"      (cwd: {cwd})")
        if env:
            print(f"      (env: {env})")

        return CommandResult(
            command=command,
            returncode=0,
            stdout="",
            stderr="",
            streamed=stream
        )
