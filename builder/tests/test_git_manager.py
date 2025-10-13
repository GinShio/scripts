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
        self.submodule_urls: dict[str, str] = {}
        self.submodule_status_entries: list[tuple[str, str, str | None]] = []
        self.sparse_checkout: bool = False
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

        if cmd_list[:4] == ["git", "config", "--bool", "core.sparseCheckout"]:
            value = "true" if self.sparse_checkout else "false"
            return CommandResult(command=cmd_list, returncode=0, stdout=f"{value}\n", stderr="")
        if cmd_list[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return CommandResult(command=cmd_list, returncode=0, stdout=f"{branch}\n", stderr="")
        if cmd_list[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
            return CommandResult(command=cmd_list, returncode=0, stdout="true\n", stderr="")
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
            key_entry = cmd_list[4]
            path_key = self._extract_submodule_path(key_entry)
            if path_key and path_key in self.submodule_paths:
                if key_entry.endswith(".url") or key_entry.endswith('".url'):
                    url = self.submodule_urls.get(path_key, "")
                    return CommandResult(command=cmd_list, returncode=0, stdout=f"{url}\n", stderr="")
                return CommandResult(command=cmd_list, returncode=0, stdout=f"{path_key}\n", stderr="")
            return CommandResult(command=cmd_list, returncode=1, stdout="", stderr="not found")
        if cmd_list[:4] == ["git", "submodule", "status", "--recursive"]:
            lines: list[str] = []
            for commit_hash, submodule_path, branch_name in self.submodule_status_entries:
                suffix = f" ({branch_name})" if branch_name else ""
                lines.append(f" {commit_hash} {submodule_path}{suffix}")
            stdout = "\n".join(lines)
            if stdout:
                stdout += "\n"
            return CommandResult(command=cmd_list, returncode=0, stdout=stdout, stderr="")
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

    def add_submodule_status(self, path: str, commit: str, url: str | None = None, branch: str | None = None) -> None:
        self.submodule_status_entries.append((commit, path, branch))
        self.submodule_paths.add(path)
        if url is not None:
            self.submodule_urls[path] = url

    @staticmethod
    def _extract_submodule_path(key: str) -> str | None:
        suffixes = ('".path', '".url')
        for suffix in suffixes:
            if key.startswith('submodule."') and key.endswith(suffix):
                return key[len('submodule."') : -len(suffix)]
        plain_suffixes = ('.path', '.url')
        for suffix in plain_suffixes:
            if key.startswith("submodule.") and key.endswith(suffix):
                return key[len("submodule.") : -len(suffix)]
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
        self.assertIn(["git", "switch", "feature"], history_commands)
        self.assertIn(["git", "stash", "pop"], history_commands)
        self.assertLess(
            history_commands.index(["git", "switch", "feature"]),
            history_commands.index(["git", "stash", "pop"]),
        )

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

    def test_prepare_and_restore_same_branch_skips_submodule_update(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="main", commits={"main": "m1"})
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="component/main",
            commits={"component/main": "c1"},
            dirty=False,
        )

        manager = GitManager(runner)
        state = manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=False,
            no_switch_branch=False,
            component_dir=component_rel,
            component_branch="component/main",
        )

        prepare_updates = [
            entry
            for entry in runner.history
            if entry["command"] == ["git", "submodule", "update", "--recursive"]
        ]
        self.assertFalse(prepare_updates)

        history_length_before_restore = len(runner.history)
        manager.restore_checkout(self.repo_path, state)
        restore_updates = [
            entry
            for entry in runner.history[history_length_before_restore:]
            if entry["command"] == ["git", "submodule", "update", "--recursive"]
        ]
        self.assertFalse(restore_updates)

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
        root_switches = [
            entry
            for entry in history_commands
            if entry["cwd"] == self.repo_path and entry["command"][:2] == ["git", "switch"]
        ]
        self.assertFalse(root_switches)
        self.assertEqual(runner.branch, "feature")
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
        self.assertFalse(root_restoration_updates)

        self.assertNotIn(["git", "checkout", "feature"], restoration_commands)
        self.assertEqual(runner.branch, "feature")

    def test_prepare_checkout_component_switches_when_commits_match(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2"})
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="comp-old",
            commits={"comp-old": "same", "comp-target": "same"},
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

        component_switches = [
            entry["command"]
            for entry in runner.history
            if entry["cwd"] == component_path and entry["command"][:2] == ["git", "switch"]
        ]
        self.assertIn(["git", "switch", "comp-target"], component_switches)
        self.assertEqual(state.component_branch, "comp-old")

        history_length_before_restore = len(runner.history)
        manager.restore_checkout(self.repo_path, state)
        restore_component_commands = [
            entry["command"]
            for entry in runner.history[history_length_before_restore:]
            if entry["cwd"] == component_path
        ]
        self.assertIn(["git", "switch", "comp-old"], restore_component_commands)

    def test_prepare_checkout_component_absolute_path_detected(self) -> None:
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
            component_dir=component_path,
            component_branch="comp-target",
        )

        component_switches = [
            entry["command"]
            for entry in runner.history
            if entry["cwd"] == component_path and entry["command"][:2] == ["git", "switch"]
        ]
        self.assertIn(["git", "switch", "comp-target"], component_switches)
        self.assertEqual(state.component_path, component_path)
        self.assertEqual(state.component_branch, "comp-old")

    def test_prepare_checkout_component_external_worktree_detected(self) -> None:
        component_path = (Path(self.temp_dir.name) / "worktrees" / "component-main").resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2"})
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
            component_dir=component_path,
            component_branch="comp-target",
        )

        component_switches = [
            entry["command"]
            for entry in runner.history
            if entry["cwd"] == component_path and entry["command"][:2] == ["git", "switch"]
        ]
        self.assertIn(["git", "switch", "comp-target"], component_switches)
        self.assertEqual(state.component_path, component_path)
        self.assertEqual(state.component_branch, "comp-old")

    def test_update_repository_component_submodule_switches_component(self) -> None:
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
        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
            component_branch="comp-target",
            component_dir=component_rel,
        )

        component_switches = [
            entry
            for entry in runner.history
            if entry["cwd"] == component_path and entry["command"][:2] == ["git", "switch"]
        ]
        self.assertTrue(component_switches)
        component_pulls = [
            entry
            for entry in runner.history
            if entry["cwd"] == component_path and entry["command"] == ["git", "pull", "--ff-only", "origin", "comp-target"]
        ]
        self.assertTrue(component_pulls)
        component_switch_commands = [entry["command"] for entry in component_switches]
        self.assertIn(["git", "switch", "comp-target"], component_switch_commands)
        self.assertIn(["git", "switch", "comp-old"], component_switch_commands)
        self.assertLess(
            component_switch_commands.index(["git", "switch", "comp-target"]),
            component_switch_commands.index(["git", "switch", "comp-old"]),
        )

        state = runner.repo_states.get(component_path)
        self.assertIsNotNone(state)
        if state:
            self.assertEqual(state.get("branch"), "comp-old")

    def test_update_repository_component_switches_when_commits_match(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2", "comp-target": "same"})
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="comp-old",
            commits={"comp-old": "same", "comp-target": "same"},
        )

        manager = GitManager(runner)
        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
            component_branch="comp-target",
            component_dir=component_rel,
        )

        component_switches = [
            entry["command"]
            for entry in runner.history
            if entry["cwd"] == component_path and entry["command"][:2] == ["git", "switch"]
        ]
        self.assertIn(["git", "switch", "comp-target"], component_switches)
        self.assertIn(["git", "switch", "comp-old"], component_switches)

    def test_update_repository_component_branch_only_updates_component_repo(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2", "comp-target": "c2"})
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="comp-old",
            commits={"comp-old": "c1", "comp-target": "c2"},
        )

        manager = GitManager(runner)
        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
            component_branch="comp-target",
            component_dir=component_rel,
        )

        root_switches = [
            entry
            for entry in runner.history
            if entry["cwd"] == self.repo_path and entry["command"][:2] == ["git", "switch"]
        ]
        self.assertTrue(any(entry["command"] == ["git", "switch", "main"] for entry in root_switches))
        component_switches = [
            entry
            for entry in runner.history
            if entry["cwd"] == component_path and entry["command"][:2] == ["git", "switch"]
        ]
        self.assertEqual(
            [entry["command"] for entry in component_switches],
            [["git", "switch", "comp-target"], ["git", "switch", "comp-old"]],
        )

        state = runner.repo_states.get(component_path)
        self.assertIsNotNone(state)
        if state:
            self.assertEqual(state.get("branch"), "comp-old")

    def test_update_repository_component_auto_stash_when_switching(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(
            initial_branch="feature",
            commits={"feature": "f1", "main": "m2", "comp-target": "c2"},
        )
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="comp-old",
            commits={"comp-old": "c1", "comp-target": "c2"},
            dirty=True,
        )

        manager = GitManager(runner)
        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
            component_branch="comp-target",
            component_dir=component_rel,
            auto_stash=True,
        )

        component_commands = [
            entry["command"]
            for entry in runner.history
            if entry["cwd"] == component_path
        ]
        self.assertIn(["git", "stash", "push", "-m", "builder auto-stash"], component_commands)
        self.assertIn(["git", "stash", "pop"], component_commands)

        stash_push_index = component_commands.index(["git", "stash", "push", "-m", "builder auto-stash"])
        switch_index = component_commands.index(["git", "switch", "comp-target"])
        self.assertLess(stash_push_index, switch_index)

        restore_index = component_commands.index(["git", "switch", "comp-old"])
        stash_pop_index = component_commands.index(["git", "stash", "pop"])
        self.assertLess(restore_index, stash_pop_index)

        state = runner.repo_states.get(component_path)
        self.assertIsNotNone(state)
        if state:
            self.assertEqual(state.get("branch"), "comp-old")

    def test_update_repository_component_dirty_without_auto_stash_raises(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(
            initial_branch="feature",
            commits={"feature": "f1", "main": "m2", "comp-target": "c2"},
        )
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="comp-old",
            commits={"comp-old": "c1", "comp-target": "c2"},
            dirty=True,
        )

        manager = GitManager(runner)
        with self.assertRaisesRegex(
            RuntimeError,
            r"Component working tree at '.*components/library' has uncommitted changes; enable auto_stash to proceed",
        ):
            manager.update_repository(
                repo_path=self.repo_path,
                url="https://example.com/demo.git",
                main_branch="main",
                component_branch="comp-target",
                component_dir=component_rel,
            )

    def test_update_repository_handles_detached_head(self) -> None:
        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2"})
        manager = GitManager(runner)

        runner.branch = "HEAD"
        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
        )

        commands = [entry["command"] for entry in runner.history]
        self.assertIn(["git", "switch", "main"], commands)
        self.assertNotIn(["git", "switch", "HEAD"], commands)

    def test_update_repository_update_script_switches_branch(self) -> None:
        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2"})
        manager = GitManager(runner)

        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
            update_script="echo update",
        )

        commands = [entry["command"] for entry in runner.history]
        self.assertIn(["git", "switch", "main"], commands)
        self.assertIn(["sh", "-c", "echo update"], commands)
        self.assertIn(["git", "switch", "feature"], commands)

        switch_main_index = commands.index(["git", "switch", "main"])
        script_index = commands.index(["sh", "-c", "echo update"])
        restore_index = commands.index(["git", "switch", "feature"])
        self.assertLess(switch_main_index, script_index)
        self.assertLess(script_index, restore_index)
        self.assertNotIn(["git", "fetch", "--all"], commands)

    def test_update_repository_update_script_stash_handling(self) -> None:
        runner = FakeGitRunner(initial_branch="feature", dirty=True, commits={"feature": "f1", "main": "m2"})
        manager = GitManager(runner)

        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
            update_script="echo update",
            auto_stash=True,
        )

        commands = [entry["command"] for entry in runner.history]
        self.assertIn(["git", "stash", "push", "-m", "builder auto-stash"], commands)
        self.assertIn(["git", "stash", "pop"], commands)
        self.assertIn(["sh", "-c", "echo update"], commands)

        stash_push_index = commands.index(["git", "stash", "push", "-m", "builder auto-stash"])
        script_index = commands.index(["sh", "-c", "echo update"])
        stash_pop_index = commands.index(["git", "stash", "pop"])
        restore_index = commands.index(["git", "switch", "feature"])

        self.assertLess(stash_push_index, script_index)
        self.assertLess(script_index, restore_index)
        self.assertLess(restore_index, stash_pop_index)

    def test_update_repository_update_script_restores_component_branch(self) -> None:
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
        manager.update_repository(
            repo_path=self.repo_path,
            url="https://example.com/demo.git",
            main_branch="main",
            component_branch="comp-target",
            component_dir=component_rel,
            update_script="echo update",
        )

        commands = [entry["command"] for entry in runner.history]
        self.assertIn(["git", "switch", "main"], commands)
        self.assertIn(["sh", "-c", "echo update"], commands)
        self.assertIn(["git", "switch", "feature"], commands)

        component_switch_commands = [
            entry["command"]
            for entry in runner.history
            if entry["cwd"] == component_path and entry["command"][:2] == ["git", "switch"]
        ]
        self.assertEqual(
            component_switch_commands,
            [["git", "switch", "comp-target"], ["git", "switch", "comp-old"]],
        )

        state = runner.repo_states.get(component_path)
        self.assertIsNotNone(state)
        if state:
            self.assertEqual(state.get("branch"), "comp-old")

    def test_prepare_checkout_component_submodule_skips_root_stash(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="feature", dirty=True, commits={"feature": "f1", "main": "m2"})
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="comp-old",
            commits={"comp-old": "c1", "comp-target": "c2"},
        )

        manager = GitManager(runner)
        manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=True,
            no_switch_branch=False,
            component_dir=component_rel,
            component_branch="comp-target",
        )

        stash_commands = [entry for entry in runner.history if entry["command"][:3] == ["git", "stash", "push"]]
        self.assertFalse(stash_commands)
        self.assertEqual(runner.branch, "feature")

    def test_prepare_checkout_component_auto_stash_when_switching(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2"})
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="comp-old",
            commits={"comp-old": "c1", "comp-target": "c2"},
            dirty=True,
        )

        manager = GitManager(runner)
        state = manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=True,
            no_switch_branch=False,
            component_dir=component_rel,
            component_branch="comp-target",
        )

        component_commands = [
            entry["command"]
            for entry in runner.history
            if entry["cwd"] == component_path
        ]
        self.assertIn(["git", "stash", "push", "-m", "builder auto-stash"], component_commands)
        self.assertIn(["git", "switch", "comp-target"], component_commands)

        history_length_before_restore = len(runner.history)
        manager.restore_checkout(self.repo_path, state)
        restore_component_commands = [
            entry["command"]
            for entry in runner.history[history_length_before_restore:]
            if entry["cwd"] == component_path
        ]
        self.assertIn(["git", "stash", "pop"], restore_component_commands)

    def test_prepare_checkout_component_dirty_without_switch_skips_stash(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="main", dirty=False, commits={"main": "m1"})
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="component/main",
            commits={"component/main": "c1"},
            dirty=True,
        )

        manager = GitManager(runner)
        state = manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=True,
            no_switch_branch=False,
            component_dir=component_rel,
            component_branch="component/main",
        )

        component_stashes = [
            entry
            for entry in runner.history
            if entry["cwd"] == component_path and entry["command"][:3] == ["git", "stash", "push"]
        ]
        self.assertFalse(component_stashes)
        self.assertIsNone(state.component_path)
        self.assertIsNone(state.component_branch)
        component_updates = [
            entry
            for entry in runner.history
            if entry["cwd"] == component_path and entry["command"] == ["git", "submodule", "update", "--recursive"]
        ]
        self.assertFalse(component_updates)

    def test_prepare_checkout_component_dirty_without_auto_stash_raises(self) -> None:
        component_rel = Path("components/library")
        component_path = (self.repo_path / component_rel).resolve()
        (component_path / ".git").mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="feature", commits={"feature": "f1", "main": "m2"})
        runner.add_submodule_path(component_rel.as_posix())
        runner.set_repo_state(
            path=component_path,
            branch="comp-old",
            commits={"comp-old": "c1", "comp-target": "c2"},
            dirty=True,
        )

        manager = GitManager(runner)
        with self.assertRaisesRegex(
            RuntimeError,
            r"Component working tree at '.*components/library' has uncommitted changes; enable auto_stash to proceed",
        ):
            manager.prepare_checkout(
                repo_path=self.repo_path,
                target_branch="main",
                auto_stash=False,
                no_switch_branch=False,
                component_dir=component_rel,
                component_branch="comp-target",
            )

    def test_restore_checkout_pops_stash_after_branch_restored(self) -> None:
        runner = FakeGitRunner(initial_branch="feature", dirty=True, commits={"feature": "f1", "main": "m2"})
        manager = GitManager(runner)
        state = manager.prepare_checkout(
            repo_path=self.repo_path,
            target_branch="main",
            auto_stash=True,
            no_switch_branch=False,
        )
        history_length_before_restore = len(runner.history)
        manager.restore_checkout(self.repo_path, state)
        restoration_entries = runner.history[history_length_before_restore:]
        commands = [entry["command"] for entry in restoration_entries]
        stash_pop_index = commands.index(["git", "stash", "pop"])
        checkout_indices = [idx for idx, cmd in enumerate(commands) if cmd == ["git", "checkout", "feature"]]
        self.assertTrue(checkout_indices)
        self.assertLess(max(checkout_indices), stash_pop_index)

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

    def test_get_repository_state_supports_subdirectories(self) -> None:
        sub_path = self.repo_path / "components" / "alpha"
        sub_path.mkdir(parents=True)

        runner = FakeGitRunner(initial_branch="main", commits={"main": "abcdef0"})
        manager = GitManager(runner)

        branch, commit = manager.get_repository_state(sub_path)

        self.assertEqual(branch, "main")
        self.assertEqual(commit, "abcdef0")

    def test_list_submodules_sparse_checkout_skips_missing(self) -> None:
        runner = FakeGitRunner(initial_branch="main", commits={"main": "c0ffee"})
        runner.sparse_checkout = True
        runner.add_submodule_status("present/module", "1111111", url="https://example.com/present.git")
        runner.add_submodule_status("missing/module", "2222222", url="https://example.com/missing.git")

        manager = GitManager(runner)

        present_dir = self.repo_path / "present" / "module"
        present_dir.mkdir(parents=True, exist_ok=True)

        submodules = manager.list_submodules(self.repo_path)

        paths = [entry["path"] for entry in submodules]
        self.assertIn("present/module", paths)
        self.assertNotIn("missing/module", paths)
        urls = {entry["path"]: entry.get("url") for entry in submodules}
        self.assertEqual(urls.get("present/module"), "https://example.com/present.git")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
