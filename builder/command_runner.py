"""Utilities for executing shell commands with optional dry-run support."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence
import os
import shlex
import subprocess


@dataclass
class CommandResult:
    """Represents the outcome of an executed command."""

    command: Sequence[str]
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    """Raised when a command fails."""

    def __init__(self, result: CommandResult):
        super().__init__(
            f"Command failed with exit code {result.returncode}: {' '.join(map(shlex.quote, result.command))}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        self.result = result


class CommandRunner:
    """Abstract command runner interface."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        note: str | None = None,
    ) -> CommandResult:
        raise NotImplementedError

    def format_command(self, command: Sequence[str]) -> str:
        return " ".join(shlex.quote(part) for part in command)


class SubprocessCommandRunner(CommandRunner):
    """Command runner that executes commands via :mod:`subprocess`."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        note: str | None = None,
    ) -> CommandResult:
        merged_env: Dict[str, str] | None = None
        if env is not None:
            merged_env = os.environ.copy()
            merged_env.update(env)

        process = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            capture_output=True,
            text=True,
            check=False,
        )
        result = CommandResult(
            command=command,
            returncode=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
        )
        if check and process.returncode != 0:
            raise CommandError(result)
        return result


class RecordingCommandRunner(CommandRunner):
    """Command runner that records commands instead of executing them."""

    def __init__(self) -> None:
        self.commands: List[dict] = []

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        note: str | None = None,
    ) -> CommandResult:
        record = {
            "command": list(command),
            "cwd": str(cwd) if cwd else None,
            "env": dict(env) if env else {},
            "note": note,
        }
        self.commands.append(record)
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def iter_commands(self) -> Iterable[dict]:
        return iter(self.commands)

    def iter_formatted(self, *, workspace: Path | None = None) -> Iterable[str]:
        default_cwd = str(workspace) if workspace else None
        for record in self.commands:
            cmd = self.format_command(record["command"])
            cwd = record["cwd"] or default_cwd
            note = record.get("note")
            parts: List[str] = ["[dry-run]"]
            if note:
                parts.append(note)
            if cwd:
                parts.append(f"(cwd={cwd})")
            parts.append(cmd)
            yield " ".join(parts)
