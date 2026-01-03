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
    streamed: bool = False


class CommandError(RuntimeError):
    """Raised when a command fails."""

    def __init__(self, result: CommandResult):
        message = f"Command failed with exit code {result.returncode}: {' '.join(map(shlex.quote, result.command))}"
        if result.streamed:
            message = f"{message}\nstdout/stderr already streamed above."
        else:
            message = (
                f"{message}\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )
        super().__init__(message)
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
        stream: bool = False,
    ) -> CommandResult:
        raise NotImplementedError

    def format_command(self, command: Sequence[str]) -> str:
        return " ".join(shlex.quote(part) for part in command)


class SubprocessCommandRunner(CommandRunner):
    """Command runner that executes commands via :mod:`subprocess`."""

    @staticmethod
    def _merge_environment(env: Mapping[str, str] | None) -> Dict[str, str] | None:
        if env is None:
            return None
        merged = os.environ.copy()
        merged.update(env)
        return merged

    def _finalize(self, result: CommandResult, *, check: bool) -> CommandResult:
        if check and result.returncode != 0:
            raise CommandError(result)
        return result

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        note: str | None = None,
        stream: bool = False,
    ) -> CommandResult:
        merged_env = self._merge_environment(env)
        if not stream:
            process = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                env=merged_env,
                capture_output=True,
                text=True,
                check=False,
            )
            return self._finalize(
                CommandResult(
                    command=command,
                    returncode=process.returncode,
                    stdout=process.stdout,
                    stderr=process.stderr,
                ),
                check=check,
            )

        process = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            check=False,
        )

        return self._finalize(
            CommandResult(
                command=command,
                returncode=process.returncode,
                stdout="",
                stderr="",
                streamed=True,
            ),
            check=check,
        )


@dataclass(slots=True)
class RecordedCommand:
    command: List[str]
    cwd: str | None
    env: Dict[str, str]
    note: str | None
    stream: bool


class RecordingCommandRunner(CommandRunner):
    """Command runner that records commands instead of executing them."""

    def __init__(self) -> None:
        self.commands: List[RecordedCommand] = []

    @staticmethod
    def _record_entry(
        *,
        command: Sequence[str],
        cwd: Path | None,
        env: Mapping[str, str] | None,
        note: str | None,
        stream: bool,
    ) -> RecordedCommand:
        return RecordedCommand(
            command=list(command),
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env else {},
            note=note,
            stream=stream,
        )

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        note: str | None = None,
        stream: bool = False,
    ) -> CommandResult:
        self.commands.append(
            self._record_entry(command=command, cwd=cwd, env=env, note=note, stream=stream)
        )
        return CommandResult(command=command, returncode=0, stdout="", stderr="")

    def iter_commands(self) -> Iterable[RecordedCommand]:
        return iter(self.commands)

    def iter_formatted(self, *, workspace: Path | None = None) -> Iterable[str]:
        default_cwd = str(workspace) if workspace else None
        for record in self.commands:
            cmd = self.format_command(record.command)
            cwd = record.cwd or default_cwd
            note = record.note
            parts: List[str] = ["[dry-run]"]
            if note:
                parts.append(note)
            if cwd:
                parts.append(f"(cwd={cwd})")
            parts.append(cmd)
            yield " ".join(parts)
