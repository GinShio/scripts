"""Test submodule listing functionality."""

import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from builder.git_manager import GitManager
from builder.command_runner import CommandResult


class TestSubmodulesList(unittest.TestCase):
    @patch('pathlib.Path.exists')
    def test_list_submodules_empty_repo(self, mock_exists):
        """Test listing submodules in a repository with no submodules."""
        mock_exists.return_value = True

        runner = Mock()
        runner.run.return_value = CommandResult(command=[], returncode=1, stdout="", stderr="")

        git_manager = GitManager(runner)
        repo_path = Path("/fake/repo")

        with patch.object(Path, '__truediv__') as mock_truediv:
            mock_truediv.return_value = Mock(exists=Mock(return_value=True))
            result = git_manager.list_submodules(repo_path)
            self.assertEqual(result, [])

    @patch('pathlib.Path.exists')
    def test_list_submodules_with_submodules(self, mock_exists):
        """Test listing submodules in a repository with submodules."""
        mock_exists.return_value = True

        runner = Mock()

        # Mock submodule status output
        status_output = " abc123def external/lib1 (master)\n def456abc external/lib2 (main)\n"

        def run_side_effect(command, **kwargs):
            if command[:3] == ["git", "submodule", "status"]:
                return CommandResult(command=command, returncode=0, stdout=status_output, stderr="")

            if command[:3] == ["git", "config", "--file"]:
                key = command[-1]
                mapping = {
                    'submodule."external/lib1".url': "https://github.com/example/lib1.git\n",
                    'submodule."external/lib2".url': "\n",
                    'submodule.external/lib2.url': "https://github.com/example/lib2.git\n",
                }
                value = mapping.get(key, "")
                if value:
                    return CommandResult(command=command, returncode=0, stdout=value, stderr="")
                return CommandResult(command=command, returncode=1, stdout="", stderr="")

            if command[:3] == ["git", "config", "--get"]:
                key = command[-1]
                mapping = {
                    'submodule."external/lib2".url': "https://github.com/example/lib2.git\n",
                }
                value = mapping.get(key, "")
                if value:
                    return CommandResult(command=command, returncode=0, stdout=value, stderr="")
                return CommandResult(command=command, returncode=1, stdout="", stderr="")

            raise AssertionError(f"Unexpected command: {command}")

        runner.run.side_effect = run_side_effect

        git_manager = GitManager(runner)
        repo_path = Path("/fake/repo")

        with patch.object(Path, '__truediv__') as mock_truediv:
            mock_truediv.return_value = Mock(exists=Mock(return_value=True))
            result = git_manager.list_submodules(repo_path)

            expected = [
                {
                    "path": "external/lib1",
                    "hash": "abc123def",
                    "url": "https://github.com/example/lib1.git"
                },
                {
                    "path": "external/lib2",
                    "hash": "def456abc",
                    "url": "https://github.com/example/lib2.git"
                }
            ]
            self.assertEqual(result, expected)

    @patch('pathlib.Path.exists')
    def test_list_submodules_url_fallback_to_config(self, mock_exists):
        """Ensure URL lookup falls back to git config when .gitmodules entry is empty."""
        mock_exists.return_value = True

        runner = Mock()

        status_output = " abc123def external/lib1\n"

        def run_side_effect(command, **kwargs):
            if command[:3] == ["git", "submodule", "status"]:
                return CommandResult(command=command, returncode=0, stdout=status_output, stderr="")

            if command[:3] == ["git", "config", "--file"]:
                key = command[-1]
                if key == 'submodule."external/lib1".url':
                    return CommandResult(command=command, returncode=0, stdout="\n", stderr="")
                return CommandResult(command=command, returncode=1, stdout="", stderr="")

            if command[:3] == ["git", "config", "--get"]:
                key = command[-1]
                if key == 'submodule.external/lib1.url':
                    return CommandResult(command=command, returncode=0, stdout="https://github.com/example/lib1.git\n", stderr="")
                return CommandResult(command=command, returncode=1, stdout="", stderr="")

            raise AssertionError(f"Unexpected command: {command}")

        runner.run.side_effect = run_side_effect

        git_manager = GitManager(runner)
        repo_path = Path("/fake/repo")

        with patch.object(Path, '__truediv__') as mock_truediv:
            mock_truediv.return_value = Mock(exists=Mock(return_value=True))
            result = git_manager.list_submodules(repo_path)

            expected = [
                {
                    "path": "external/lib1",
                    "hash": "abc123def",
                    "url": "https://github.com/example/lib1.git",
                }
            ]
            self.assertEqual(result, expected)

    def test_list_submodules_nonexistent_repo(self):
        """Test listing submodules in a non-existent repository."""
        runner = Mock()
        git_manager = GitManager(runner)
        repo_path = Path("/nonexistent/repo")

        result = git_manager.list_submodules(repo_path)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
