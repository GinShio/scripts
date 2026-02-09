import unittest
from unittest.mock import MagicMock, patch

from git_stack.src import git


class TestGit(unittest.TestCase):
    @patch("git_stack.src.git._get_repo")
    def test_run_git_success(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "git output\n"
        mock_repo.run_git_cmd.return_value = mock_result
        mock_get_repo.return_value = mock_repo

        output = git.run_git(["status"])

        self.assertEqual(output, "git output")
        mock_repo.run_git_cmd.assert_called_with(["status"], check=True)

    @patch("git_stack.src.git._get_repo")
    def test_run_git_fail_check_false(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"
        mock_repo.run_git_cmd.return_value = mock_result
        mock_get_repo.return_value = mock_repo

        output = git.run_git(["status"], check=False)
        self.assertEqual(output, "")

    def test_resolve_base_branch_provided(self):
        self.assertEqual(git.resolve_base_branch("feature"), "feature")

    @patch("git_stack.src.git._get_repo")
    def test_resolve_base_branch_auto_main(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_repo.get_config.return_value = None  # no workflow.base-branch
        mock_repo.list_remotes.return_value = ["origin"]
        mock_repo.resolve_default_branch.return_value = "main"
        mock_get_repo.return_value = mock_repo

        self.assertEqual(git.resolve_base_branch(None), "main")

    @patch("git_stack.src.git._get_repo")
    def test_resolve_base_branch_auto_master(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_repo.get_config.return_value = None
        mock_repo.list_remotes.return_value = ["origin"]
        mock_repo.resolve_default_branch.return_value = "master"
        mock_get_repo.return_value = mock_repo

        self.assertEqual(git.resolve_base_branch(None), "master")

    @patch("git_stack.src.git._get_repo")
    def test_get_refs_map(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_repo.get_branches.return_value = {"main": "12345", "feature": "67890"}
        mock_get_repo.return_value = mock_repo

        refs = git.get_refs_map()
        self.assertEqual(refs, {"main": "12345", "feature": "67890"})

    @patch("git_stack.src.git._get_repo")
    def test_resolve_base_branch_check_upstream(self, mock_get_repo):
        """Scenario: upstream exists, has main."""
        mock_repo = MagicMock()
        mock_repo.get_config.return_value = None
        mock_repo.list_remotes.return_value = ["origin", "upstream"]
        mock_repo.resolve_default_branch.return_value = "main"
        mock_get_repo.return_value = mock_repo

        base = git.resolve_base_branch()
        self.assertEqual(base, "main")
        mock_repo.resolve_default_branch.assert_called_with(remote="upstream")

    @patch("git_stack.src.git._get_repo")
    def test_resolve_base_branch_fallback_origin(self, mock_get_repo):
        """Scenario: upstream remote name returned as 'origin' (default)."""
        mock_repo = MagicMock()
        mock_repo.get_config.return_value = None
        mock_repo.list_remotes.return_value = ["origin"]
        mock_repo.resolve_default_branch.return_value = "master"
        mock_get_repo.return_value = mock_repo

        base = git.resolve_base_branch()
        self.assertEqual(base, "master")
        mock_repo.resolve_default_branch.assert_called_with(remote="origin")

    @patch("git_stack.src.git._get_repo")
    def test_get_config(self, mock_get_repo):
        mock_repo = MagicMock()
        mock_repo.get_config.return_value = "value"
        mock_get_repo.return_value = mock_repo

        self.assertEqual(git.get_config("key"), "value")
        mock_repo.get_config.assert_called_with("key")
