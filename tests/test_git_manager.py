from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from builder.command_runner import CommandResult, CommandRunner
from builder.git_manager import GitManager


class FakeGitRunner(CommandRunner):
    def __init__(
        self,
        *,
        initial_branch: str = "feature",
        dirty: bool = False,
        commits: dict[str, str] | None = None,
    ) -> None:
        self.branch = initial_branch
        self.history: list[dict] = []
        self.dirty = dirty
        self.commits = commits or {initial_branch: "abc123"}

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
        if cmd_list[:2] == ["git", "rev-parse"] and len(cmd_list) == 3 and cmd_list[2] == "HEAD":
            commit = self.commits.get(self.branch, "")
            return CommandResult(command=cmd_list, returncode=0, stdout=f"{commit}\n", stderr="")
        if cmd_list[0:2] == ["git", "rev-parse"] and len(cmd_list) == 3 and cmd_list[2] != "HEAD":
            branch = cmd_list[2]
            if branch in self.commits:
                return CommandResult(command=cmd_list, returncode=0, stdout=f"{self.commits[branch]}\n", stderr="")
            return CommandResult(command=cmd_list, returncode=1, stdout="", stderr="unknown branch")
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
                origin_branch = cmd_list[idx + 2] if idx + 2 < len(cmd_list) else None
                if origin_branch and origin_branch.startswith("origin/"):
                    source = origin_branch.split("/", 1)[1]
                    self.commits[self.branch] = self.commits.get(source, "newcommit")
                else:
                    self.commits[self.branch] = self.commits.get(self.branch, "newcommit")
            else:
                self.branch = cmd_list[2]
            self.commits.setdefault(self.branch, "switched")
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
        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m1"})
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
        runner = FakeGitRunner(initial_branch="feature", dirty=True, commits={"feature": "f1", "main": "m1"})
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

    def test_prepare_checkout_same_branch_skips_stash(self) -> None:
        runner = FakeGitRunner(initial_branch="main", dirty=True, commits={"main": "abcd"})
        manager = GitManager(runner)
        state = manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=True,
            no_switch_branch=False,
        )
        history_commands = [entry["command"] for entry in runner.history]
        self.assertNotIn(["git", "stash", "push", "-m", "builder auto-stash"], history_commands)
        self.assertFalse(state.stash_applied)
        self.assertEqual(runner.branch, "main")

    def test_prepare_checkout_other_branch_stashes_when_needed(self) -> None:
        runner = FakeGitRunner(initial_branch="feature", dirty=True, commits={"feature": "f1", "main": "m2"})
        manager = GitManager(runner)
        state = manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=True,
            no_switch_branch=False,
        )
        history_commands = [entry["command"] for entry in runner.history]
        self.assertIn(["git", "stash", "push", "-m", "builder auto-stash"], history_commands)
        self.assertTrue(state.stash_applied)
        self.assertEqual(runner.branch, "main")

    def test_prepare_checkout_same_commit_skips_switch(self) -> None:
        runner = FakeGitRunner(initial_branch="feature", dirty=True, commits={"feature": "abc", "main": "abc"})
        manager = GitManager(runner)
        state = manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=True,
            no_switch_branch=False,
            environment={"CUSTOM": "VALUE"},
        )
        history_commands = [entry["command"] for entry in runner.history]
        self.assertNotIn(["git", "switch", "main"], history_commands)
        self.assertNotIn(["git", "stash", "push", "-m", "builder auto-stash"], history_commands)
        self.assertFalse(state.stash_applied)
        self.assertEqual(runner.branch, "feature")
        env_records = [entry["env"] for entry in runner.history if entry["command"][0] == "git"]
        self.assertTrue(all(record.get("CUSTOM") == "VALUE" for record in env_records))

    def test_update_repository_passes_environment(self) -> None:
        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m1"})
        manager = GitManager(runner)
        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
            auto_stash=False,
            environment={"GIT_TRACE": "1"},
            dry_run=False,
        )
        env_records = [entry["env"] for entry in runner.history if entry["command"][0] == "git"]
        self.assertTrue(env_records)
        self.assertTrue(all(record.get("GIT_TRACE") == "1" for record in env_records))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
