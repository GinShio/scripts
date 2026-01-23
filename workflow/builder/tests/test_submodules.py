"""Test submodule listing functionality."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.command_runner import CommandResult

from builder.git_manager import GitManager


class TestSubmodulesList(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name) / "repo"
        self.repo_path.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_runner(self) -> Mock:
        runner = Mock()

        def run_side_effect(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
                return CommandResult(
                    command=command, returncode=0, stdout="true\n", stderr=""
                )
            if command[:3] == ["git", "config", "--bool"]:
                return CommandResult(
                    command=command, returncode=0, stdout="false\n", stderr=""
                )
            raise AssertionError(f"Unexpected command: {command}")

        runner.run.side_effect = run_side_effect
        return runner

    def test_list_submodules_empty_repo(self) -> None:
        runner = self._make_runner()

        def run_side_effect(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
                return CommandResult(
                    command=command, returncode=0, stdout="true\n", stderr=""
                )
            if command[:3] == ["git", "config", "--bool"]:
                return CommandResult(
                    command=command, returncode=0, stdout="false\n", stderr=""
                )
            if command[:3] == ["git", "submodule", "status"]:
                return CommandResult(
                    command=command, returncode=0, stdout="", stderr=""
                )
            raise AssertionError(f"Unexpected command: {command}")

        runner.run.side_effect = run_side_effect

        git_manager = GitManager(runner)
        result = git_manager.list_submodules(self.repo_path)
        self.assertEqual(result, [])

    def test_list_submodules_with_submodules(self) -> None:
        lib1 = self.repo_path / "external" / "lib1"
        lib2 = self.repo_path / "external" / "lib2"
        lib1.mkdir(parents=True)
        lib2.mkdir(parents=True)

        runner = self._make_runner()

        status_output = " aaaaaaa external/lib1 (main)\n bbbbbbb external/lib2 (dev)\n"

        def run_side_effect(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
                return CommandResult(
                    command=command, returncode=0, stdout="true\n", stderr=""
                )
            if command[:3] == ["git", "config", "--bool"]:
                return CommandResult(
                    command=command, returncode=0, stdout="false\n", stderr=""
                )
            if command[:3] == ["git", "submodule", "status"]:
                return CommandResult(
                    command=command, returncode=0, stdout=status_output, stderr=""
                )
            if command[:3] == ["git", "config", "--file"]:
                key = command[-1]
                mapping = {
                    'submodule."external/lib1".url': "https://example.com/lib1.git\n",
                    'submodule."external/lib2".url': "\n",
                    "submodule.external/lib2.url": "https://example.com/lib2.git\n",
                }
                value = mapping.get(key, "")
                if value:
                    return CommandResult(
                        command=command, returncode=0, stdout=value, stderr=""
                    )
                return CommandResult(
                    command=command, returncode=1, stdout="", stderr=""
                )
            if command[:3] == ["git", "config", "--get"]:
                key = command[-1]
                mapping = {
                    'submodule."external/lib2".url': "https://example.com/lib2.git\n",
                }
                value = mapping.get(key, "")
                if value:
                    return CommandResult(
                        command=command, returncode=0, stdout=value, stderr=""
                    )
                return CommandResult(
                    command=command, returncode=1, stdout="", stderr=""
                )
            raise AssertionError(f"Unexpected command: {command}")

        runner.run.side_effect = run_side_effect

        git_manager = GitManager(runner)
        result = git_manager.list_submodules(self.repo_path)

        expected = [
            {
                "path": "external/lib1",
                "hash": "aaaaaaa",
                "url": "https://example.com/lib1.git",
            },
            {
                "path": "external/lib2",
                "hash": "bbbbbbb",
                "url": "https://example.com/lib2.git",
            },
        ]
        self.assertEqual(result, expected)

    def test_list_submodules_url_fallback_to_config(self) -> None:
        lib_path = self.repo_path / "external" / "lib"
        lib_path.mkdir(parents=True)

        runner = self._make_runner()

        status_output = " cccccc external/lib\n"

        def run_side_effect(command, **kwargs):
            if command[:3] == ["git", "rev-parse", "--is-inside-work-tree"]:
                return CommandResult(
                    command=command, returncode=0, stdout="true\n", stderr=""
                )
            if command[:3] == ["git", "config", "--bool"]:
                return CommandResult(
                    command=command, returncode=0, stdout="false\n", stderr=""
                )
            if command[:3] == ["git", "submodule", "status"]:
                return CommandResult(
                    command=command, returncode=0, stdout=status_output, stderr=""
                )
            if command[:3] == ["git", "config", "--file"]:
                key = command[-1]
                if key == 'submodule."external/lib".url':
                    return CommandResult(
                        command=command, returncode=0, stdout="\n", stderr=""
                    )
                return CommandResult(
                    command=command, returncode=1, stdout="", stderr=""
                )
            if command[:3] == ["git", "config", "--get"]:
                key = command[-1]
                if key == "submodule.external/lib.url":
                    return CommandResult(
                        command=command,
                        returncode=0,
                        stdout="https://example.com/lib.git\n",
                        stderr="",
                    )
                return CommandResult(
                    command=command, returncode=1, stdout="", stderr=""
                )
            raise AssertionError(f"Unexpected command: {command}")

        runner.run.side_effect = run_side_effect

        git_manager = GitManager(runner)
        result = git_manager.list_submodules(self.repo_path)

        expected = [
            {
                "path": "external/lib",
                "hash": "cccccc",
                "url": "https://example.com/lib.git",
            }
        ]
        self.assertEqual(result, expected)

    def test_list_submodules_nonexistent_repo(self) -> None:
        runner = Mock()
        git_manager = GitManager(runner)
        missing_path = self.repo_path / "missing"

        result = git_manager.list_submodules(missing_path)
        self.assertEqual(result, [])
