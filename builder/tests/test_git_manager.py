from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from core.command_runner import CommandResult, CommandRunner
from builder.git_manager import GitManager


class FakeGitRunner(CommandRunner):
    def __init__(
        self,
        *,
        initial_branch: str = "feature",
        dirty: bool = False,
        commits: dict[str, str] | None = None,
    ) -> None:
        base_commits = commits or {initial_branch: "abc123"}
        self.history: list[dict] = []
        self.repo_states: dict[Path | None, dict[str, object]] = {
            None: {
                "branch": initial_branch,
                "dirty": dirty,
                "commits": dict(base_commits),
            }
        }
        self.submodule_paths: set[str] = set()
        self.root_path: Path | None = None

    @property
    def branch(self) -> str:
        return self.repo_states[None]["branch"]  # type: ignore[index]

    @branch.setter
    def branch(self, value: str) -> None:
        self.repo_states[None]["branch"] = value

    def run(self, command, *, cwd=None, env=None, check=True, note=None, stream=False):  # type: ignore[override]
        record = {
            "command": list(command),
            "cwd": cwd,
            "env": env,
            "note": note,
        }
        self.history.append(record)

        cmd_list = list(command)
        key_path = cwd.resolve() if isinstance(cwd, Path) else None
        if key_path is not None and self.root_path is None:
            self.root_path = key_path
        state = self._state(key_path)
        branch = state["branch"]  # type: ignore[index]
        commits_map: dict[str, str] = state["commits"]  # type: ignore[assignment]

        if cmd_list[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return CommandResult(command=cmd_list, returncode=0, stdout=f"{branch}\n", stderr="")
        if cmd_list[:2] == ["git", "rev-parse"] and len(cmd_list) == 3 and cmd_list[2] == "HEAD":
            commit = commits_map.get(branch, "")
            return CommandResult(command=cmd_list, returncode=0, stdout=f"{commit}\n", stderr="")
        if cmd_list[:2] == ["git", "rev-parse"] and len(cmd_list) == 3 and cmd_list[2] != "HEAD":
            target_branch = cmd_list[2]
            if target_branch in commits_map:
                return CommandResult(command=cmd_list, returncode=0, stdout=f"{commits_map[target_branch]}\n", stderr="")
            return CommandResult(command=cmd_list, returncode=1, stdout="", stderr="unknown branch")
        if cmd_list[:2] == ["git", "status"]:
            dirty = bool(state["dirty"])
            stdout = "?? file\n" if dirty else ""
            return CommandResult(command=cmd_list, returncode=0, stdout=stdout, stderr="")
        if cmd_list[:3] == ["git", "stash", "push"]:
            state["dirty"] = False
            return CommandResult(command=cmd_list, returncode=0, stdout="", stderr="")
        if cmd_list[:3] == ["git", "stash", "pop"]:
            return CommandResult(command=cmd_list, returncode=0, stdout="", stderr="")
        if cmd_list[:2] == ["git", "switch"]:
            if "-c" in cmd_list:
                idx = cmd_list.index("-c")
                new_branch = cmd_list[idx + 1]
                origin_branch = cmd_list[idx + 2] if idx + 2 < len(cmd_list) else None
                if origin_branch and origin_branch.startswith("origin/"):
                    source = origin_branch.split("/", 1)[1]
                    commits_map[new_branch] = commits_map.get(source, "newcommit")
                else:
                    commits_map.setdefault(new_branch, "newcommit")
                state["branch"] = new_branch
                if key_path is None or key_path == self.root_path:
                    self.branch = new_branch
            else:
                new_branch = cmd_list[2]
                commits_map.setdefault(new_branch, "switched")
                state["branch"] = new_branch
                if key_path is None or key_path == self.root_path:
                    self.branch = new_branch
            return CommandResult(command=cmd_list, returncode=0, stdout="", stderr="")
        if cmd_list[:2] == ["git", "checkout"]:
            if len(cmd_list) >= 3:
                target_branch = cmd_list[2]
                commits_map.setdefault(target_branch, "checkedout")
                state["branch"] = target_branch
                if key_path is None or key_path == self.root_path:
                    self.branch = target_branch
            return CommandResult(command=cmd_list, returncode=0, stdout="", stderr="")
        if cmd_list[:3] == ["git", "config", "--file"] and len(cmd_list) >= 5 and cmd_list[3] == ".gitmodules":
            path_key = self._extract_submodule_path(cmd_list[4])
            if path_key and path_key in self.submodule_paths:
                return CommandResult(command=cmd_list, returncode=0, stdout=f"{path_key}\n", stderr="")
            return CommandResult(command=cmd_list, returncode=1, stdout="", stderr="not found")
        return CommandResult(command=cmd_list, returncode=0, stdout="", stderr="")

    def _state(self, key: Path | None) -> dict[str, object]:
        if key not in self.repo_states:
            base = self.repo_states[None]
            self.repo_states[key] = {
                "branch": base["branch"],
                "dirty": base["dirty"],
                "commits": dict(base["commits"]),
            }
        return self.repo_states[key]

    def set_repo_state(
        self,
        *,
        path: Path,
        branch: str,
        commits: dict[str, str],
        dirty: bool = False,
    ) -> None:
        resolved = path.resolve()
        self.repo_states[resolved] = {
            "branch": branch,
            "dirty": dirty,
            "commits": dict(commits),
        }

    def add_submodule_path(self, path: str) -> None:
        self.submodule_paths.add(path)

    @staticmethod
    def _extract_submodule_path(key: str) -> str | None:
        if key.startswith('submodule."') and key.endswith('".path'):
            return key[len('submodule."') : -len('".path')]
        if key.startswith("submodule.") and key.endswith(".path"):
            return key[len("submodule.") : -len(".path")]
        return None


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

    def test_prepare_checkout_updates_submodules_for_normal_repo(self) -> None:
        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2"})
        manager = GitManager(runner)
        state = manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=False,
            no_switch_branch=False,
        )
        history_commands = [entry["command"] for entry in runner.history]
        self.assertIn(["git", "switch", "main"], history_commands)
        self.assertIn(["git", "submodule", "update", "--recursive"], history_commands)
        self.assertNotIn(["git", "fetch", "--all"], history_commands)
        self.assertIsNone(state.component_branch)
        self.assertIsNone(state.component_path)

    def test_prepare_checkout_component_submodule_switches_component(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2"})
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="comp-old",
            commits={"comp-old": "c1", "comp-target": "c2"},
        )

        manager = GitManager(runner)
        state = manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=False,
            no_switch_branch=False,
            component_dir=component_rel,
            component_branch="comp-target",
        )

        history_commands = runner.history
        root_updates = [entry for entry in history_commands if entry["command"] == ["git", "submodule", "update", "--recursive"] and entry["cwd"] == self.repo_path]
        self.assertFalse(root_updates)
        self.assertNotIn(["git", "fetch", "--all"], [entry["command"] for entry in history_commands])
        component_switch = [
            entry
            for entry in history_commands
            if entry["cwd"] == component_path and entry["command"][:2] == ["git", "switch"]
        ]
        self.assertTrue(component_switch)
        component_updates = [
            entry
            for entry in history_commands
            if entry["cwd"] == component_path and entry["command"] == ["git", "submodule", "update", "--recursive"]
        ]
        self.assertTrue(component_updates)
        self.assertEqual(state.component_branch, "comp-old")
        self.assertEqual(state.component_path, component_path)

        history_length_before_restore = len(runner.history)
        manager.restore_checkout(self.repo_path, state)
        restoration_entries = runner.history[history_length_before_restore:]
        restoration_commands = [entry["command"] for entry in restoration_entries]
        restoration_switch = [
            entry
            for entry in restoration_entries
            if entry["cwd"] == component_path and entry["command"] == ["git", "switch", "comp-old"]
        ]
        self.assertTrue(restoration_switch)
        restoration_updates = [
            entry
            for entry in restoration_entries
            if entry["cwd"] == component_path and entry["command"] == ["git", "submodule", "update", "--recursive"]
        ]
        self.assertTrue(restoration_updates)
        root_restoration_updates = [
            entry
            for entry in restoration_entries
            if entry["cwd"] == self.repo_path and entry["command"] == ["git", "submodule", "update", "--recursive"]
        ]
        self.assertTrue(root_restoration_updates)

        self.assertIn(["git", "checkout", "feature"], restoration_commands)
        self.assertEqual(runner.branch, "feature")

    def test_restore_checkout_updates_submodules_for_root(self) -> None:
        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2"})
        manager = GitManager(runner)
        state = manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=False,
            no_switch_branch=False,
        )
        initial_history_length = len(runner.history)

        manager.restore_checkout(self.repo_path, state)

        new_entries = runner.history[initial_history_length:]
        commands = [entry["command"] for entry in new_entries]
        self.assertIn(["git", "checkout", "feature"], commands)
        self.assertIn(["git", "submodule", "update", "--recursive"], commands)
        self.assertEqual(runner.branch, "feature")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
