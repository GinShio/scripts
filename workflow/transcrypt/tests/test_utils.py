import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys
import os

# Add the workflow directory to sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), '../../..'))
sys.path.append(os.path.join(os.path.dirname(__file__), '../../'))

from workflow.transcrypt.src import utils
from workflow.core.command_runner import CommandResult

class TestUtils(unittest.TestCase):

    @patch('workflow.transcrypt.src.utils.runner')
    def test_get_git_root_success(self, mock_runner):
        mock_runner.run.return_value = CommandResult([], 0, "/path/to/repo\n", "")
        root = utils.get_git_root()
        self.assertEqual(root, Path("/path/to/repo"))
        mock_runner.run.assert_called_with(["git", "rev-parse", "--show-toplevel"], check=True)

    @patch('workflow.transcrypt.src.utils.runner')
    def test_get_git_root_failure(self, mock_runner):
        # Mock run to raise Exception
        mock_runner.run.side_effect = Exception("git error")
        with self.assertRaises(SystemExit) as cm:
            utils.get_git_root()
        self.assertEqual(cm.exception.code, 1)

    @patch('workflow.transcrypt.src.utils.runner')
    def test_get_git_config_found(self, mock_runner):
        mock_runner.run.return_value = CommandResult([], 0, "some_value\n", "")
        val = utils.get_git_config("some.key")
        self.assertEqual(val, "some_value")
        mock_runner.run.assert_called_with(["git", "config", "--get", "some.key"], check=False)

    @patch('workflow.transcrypt.src.utils.runner')
    def test_get_git_config_not_found(self, mock_runner):
        mock_runner.run.return_value = CommandResult([], 1, "", "")
        val = utils.get_git_config("some.key")
        self.assertIsNone(val)

    @patch('workflow.transcrypt.src.utils.runner')
    def test_set_git_config(self, mock_runner):
        mock_runner.run.return_value = CommandResult([], 0, "", "")
        utils.set_git_config("some.key", "value")
        mock_runner.run.assert_called_with(["git", "config", "--local", "some.key", "value"], check=True)

    @patch('workflow.transcrypt.src.utils.runner')
    def test_unset_git_config(self, mock_runner):
        mock_runner.run.return_value = CommandResult([], 0, "", "")
        utils.unset_git_config("some.key")
        mock_runner.run.assert_called_with(["git", "config", "--local", "--unset", "some.key"], check=False)

    @patch('workflow.transcrypt.src.utils.runner')
    def test_get_git_dir(self, mock_runner):
        mock_runner.run.return_value = CommandResult([], 0, "/path/to/repo/.git\n", "")
        res = utils.get_git_dir()
        self.assertEqual(res, Path("/path/to/repo/.git"))

    @patch('workflow.transcrypt.src.utils.runner')
    def test_is_git_repo(self, mock_runner):
        mock_runner.run.return_value = CommandResult([], 0, "true\n", "")
        self.assertTrue(utils.is_git_repo())

        mock_runner.run.return_value = CommandResult([], 128, "", "")
        self.assertFalse(utils.is_git_repo())

    @patch('workflow.transcrypt.src.utils.get_git_root')
    def test_get_relative_path(self, mock_get_root):
        mock_get_root.return_value = Path("/repo")
        # Case 1: Inside repo
        p = Path("/repo/subdir/file.txt")
        rel = utils.get_relative_path(p)
        self.assertEqual(rel, Path("subdir/file.txt"))

        # Case 2: Outside repo (or not relative)
        p2 = Path("/other/file.txt")
        rel2 = utils.get_relative_path(p2)
        # Should return absolute
        self.assertEqual(rel2, Path("/other/file.txt"))

if __name__ == '__main__':
    unittest.main()
