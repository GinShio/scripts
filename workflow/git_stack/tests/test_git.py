import unittest
from unittest.mock import MagicMock, patch

from git_stack.src import git


class TestGit(unittest.TestCase):
    @patch("git_stack.src.git._RUNNER")
    def test_run_git_success(self, mock_runner):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "git output\n"
        mock_runner.run.return_value = mock_result

        output = git.run_git(["status"])

        self.assertEqual(output, "git output")
        mock_runner.run.assert_called_with(["git", "status"], check=True)

    @patch("git_stack.src.git._RUNNER")
    def test_run_git_fail_check_false(self, mock_runner):
        # Simulate a command error expectation when check=False
        # SubprocessCommandRunner.run typically returns result if check=False in run_git logic?
        # Wait, git.py implementation:
        # result = _RUNNER.run(['git'] + args, check=check)
        # If check=False, CommandRunner (wrapper) might still return result with non-zero code.

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"
        mock_runner.run.return_value = mock_result  # If check=False, it returns result

        # run_git catches CommandError if check=True.
        # But if check=False, _RUNNER.run might raise if the mocked run implementation raises?
        # The mock just returns value.

        output = git.run_git(["status"], check=False)
        self.assertEqual(output, "")

    @patch("git_stack.src.git.run_git")
    def test_resolve_base_branch_provided(self, mock_run_git):
        self.assertEqual(git.resolve_base_branch("feature"), "feature")

    @patch("git_stack.src.git.run_git")
    def test_resolve_base_branch_auto_main(self, mock_run_git):
        # Mock run_git to return sha for main
        mock_run_git.side_effect = (
            lambda args, check=True: "sha" if any("main" in a for a in args) else ""
        )
        self.assertEqual(git.resolve_base_branch(None), "main")

    @patch("git_stack.src.git.run_git")
    def test_resolve_base_branch_auto_master(self, mock_run_git):
        # Mock run_git to fail for main, succeed for master
        def side_effect(args, check=True, **kwargs):
            if "main" in args:
                return ""
            if "master" in args:
                return "sha"
            return ""

        mock_run_git.side_effect = side_effect
        self.assertEqual(git.resolve_base_branch(None), "master")

    @patch("git_stack.src.git.run_git")
    def test_get_refs_map(self, mock_run_git):
        mock_run_git.return_value = "main 12345\nfeature 67890"
        refs = git.get_refs_map()
        self.assertEqual(refs, {"main": "12345", "feature": "67890"})

    @patch("git_stack.src.git.get_upstream_remote_name")
    @patch("git_stack.src.git.run_git")
    def test_resolve_base_branch_check_upstream(self, mock_run_git, mock_get_remote):
        # Scenario: upstream exists, has main
        mock_get_remote.return_value = "upstream"

        # Mock run command chain
        # 1. symbolic-ref refs/remotes/upstream/HEAD
        # 2. show-ref ... upstream/main

        def side_effect(args, check=True):
            cmd = " ".join(args)
            if "symbolic-ref" in cmd and "upstream/HEAD" in cmd:
                return ""
            if "show-ref" in cmd and "upstream/main" in cmd:
                return "sha"
            return ""

        mock_run_git.side_effect = side_effect

        base = git.resolve_base_branch()
        self.assertEqual(base, "main")

    @patch("git_stack.src.git.get_upstream_remote_name")
    @patch("git_stack.src.git.run_git")
    def test_resolve_base_branch_fallback_origin(self, mock_run_git, mock_get_remote):
        # Scenario: upstream remote name returned as 'origin' (default)
        mock_get_remote.return_value = "origin"

        def side_effect(args, check=True):
            cmd = " ".join(args)
            if "symbolic-ref" in cmd and "origin/HEAD" in cmd:
                return "refs/remotes/origin/master"
            return ""

        mock_run_git.side_effect = side_effect

        base = git.resolve_base_branch()
        self.assertEqual(base, "master")

    @patch("git_stack.src.git.run_git")
    def test_get_config(self, mock_run_git):
        mock_run_git.return_value = "value"
        self.assertEqual(git.get_config("key"), "value")
        mock_run_git.assert_called_with(["config", "--get", "key"], check=False)
