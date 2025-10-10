from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from builder.command_runner import CommandResult, CommandRunner
from builder.git_manager import GitManager


class FakeGitRunner(CommandRunner):
    def __init__(self, *, initial_branch: str = "feature", dirty: bool = False) -> None:
        self.branch = initial_branch
        self.history: list[dict] = []
        self.dirty = dirty

    def run(self, command, *, cwd=None, env=None, check=True, note=None):  # type: ignore[override]
        record = {
            "command": list(command),
            "cwd": cwd,
            "env": env,
            "note": note,
        }
        self.history.append(record)
        cmd_list = list(command)
        if cmd_list[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return CommandResult(command=cmd_list, returncode=0, stdout=f"{self.branch}\n", stderr="")
        if cmd_list[:2] == ["git", "status"]:
            stdout = "?? file\n" if self.dirty else ""
            return CommandResult(command=cmd_list, returncode=0, stdout=stdout, stderr="")
        if cmd_list[:3] == ["git", "stash", "push"]:
            self.dirty = False
            return CommandResult(command=cmd_list, returncode=0, stdout="", stderr="")
        if cmd_list[:3] == ["git", "stash", "pop"]:
            return CommandResult(command=cmd_list, returncode=0, stdout="", stderr="")
        if cmd_list[:2] == ["git", "switch"]:
            if "-c" in cmd_list:
                idx = cmd_list.index("-c")
                self.branch = cmd_list[idx + 1]
            else:
                self.branch = cmd_list[2]
            return CommandResult(command=cmd_list, returncode=0, stdout="", stderr="")
        return CommandResult(command=cmd_list, returncode=0, stdout="", stderr="")


class GitManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name) / "demo"
        (self.repo_path / ".git").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_update_restores_original_branch(self) -> None:
        runner = FakeGitRunner(initial_branch="feature")
        manager = GitManager(runner)
        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
            auto_stash=False,
            dry_run=False,
        )
        self.assertEqual(runner.branch, "feature")
        self.assertIn(["git", "switch", "feature"], [entry["command"] for entry in runner.history])

    def test_update_restores_branch_when_auto_stash(self) -> None:
        runner = FakeGitRunner(initial_branch="feature", dirty=True)
        manager = GitManager(runner)
        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
            auto_stash=True,
            dry_run=False,
        )
        self.assertEqual(runner.branch, "feature")
        history_commands = [entry["command"] for entry in runner.history]
        self.assertIn(["git", "stash", "push", "-m", "builder auto-stash"], history_commands)
        self.assertEqual(history_commands[-1], ["git", "switch", "feature"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
