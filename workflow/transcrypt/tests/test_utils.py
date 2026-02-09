import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add the workflow directory to sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.append(os.path.join(os.path.dirname(__file__), "../../"))

from workflow.transcrypt.src import utils


class TestUtils(unittest.TestCase):
    @patch("workflow.transcrypt.src.utils._get_repo")
    def test_get_git_root_success(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_repo.root_dir = Path("/path/to/repo")
        mock_get_repo.return_value = mock_repo
        root = utils.get_git_root()
        self.assertEqual(root, Path("/path/to/repo"))

    @patch("workflow.transcrypt.src.utils._get_repo")
    def test_get_git_root_failure(self, mock_get_repo):
        mock_get_repo.side_effect = Exception("git error")
        with self.assertRaises(SystemExit) as cm:
            utils.get_git_root()
        self.assertEqual(cm.exception.code, 1)

    @patch("workflow.transcrypt.src.utils._get_repo")
    def test_get_git_config_found(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_repo.get_config.return_value = "some_value"
        mock_get_repo.return_value = mock_repo
        val = utils.get_git_config("some.key")
        self.assertEqual(val, "some_value")
        mock_repo.get_config.assert_called_with("some.key")

    @patch("workflow.transcrypt.src.utils._get_repo")
    def test_get_git_config_not_found(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_repo.get_config.return_value = None
        mock_get_repo.return_value = mock_repo
        val = utils.get_git_config("some.key")
        self.assertIsNone(val)

    @patch("workflow.transcrypt.src.utils._get_repo")
    def test_set_git_config(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_get_repo.return_value = mock_repo
        utils.set_git_config("some.key", "value")
        mock_repo.set_config.assert_called_with("some.key", "value", scope="local")

    @patch("workflow.transcrypt.src.utils._get_repo")
    def test_unset_git_config(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_get_repo.return_value = mock_repo
        utils.unset_git_config("some.key")
        mock_repo.unset_config.assert_called_with("some.key", scope="local")

    @patch("workflow.transcrypt.src.utils._get_repo")
    def test_get_git_dir(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_repo.git_dir = Path("/path/to/repo/.git")
        mock_get_repo.return_value = mock_repo
        res = utils.get_git_dir()
        self.assertEqual(res, Path("/path/to/repo/.git"))

    @patch("workflow.transcrypt.src.utils._get_repo")
    def test_is_git_repo(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_repo.is_valid = True
        mock_get_repo.return_value = mock_repo
        self.assertTrue(utils.is_git_repo())

        mock_repo.is_valid = False
        self.assertFalse(utils.is_git_repo())

    @patch("workflow.transcrypt.src.utils._get_repo")
    def test_get_relative_path(self, mock_get_repo):
        mock_repo = MagicMock()
        # Case 1: Inside repo
        mock_repo.relpath.return_value = Path("subdir/file.txt")
        mock_get_repo.return_value = mock_repo
        p = Path("/repo/subdir/file.txt")
        rel = utils.get_relative_path(p)
        self.assertEqual(rel, Path("subdir/file.txt"))

        # Case 2: Outside repo (relpath returns absolute)
        mock_repo.relpath.return_value = Path("/other/file.txt")
        p2 = Path("/other/file.txt")
        rel2 = utils.get_relative_path(p2)
        self.assertEqual(rel2, Path("/other/file.txt"))


if __name__ == "__main__":
    unittest.main()
