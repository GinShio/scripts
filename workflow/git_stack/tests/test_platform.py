import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from core.git_remotes import GitService, RemoteInfo
from git_stack.src import platform


class TestPlatform(unittest.TestCase):

    @patch('git_stack.src.platform.get_remote_url')
    def test_get_platform_github(self, mock_url):
        mock_url.return_value = "https://github.com/owner/repo.git"
        with patch.object(platform.GitHubPlatform, 'check_auth', return_value=True):
            plat = platform.get_platform()
            self.assertIsInstance(plat, platform.GitHubPlatform)
            self.assertEqual(plat.repo, "owner/repo")

    @patch('git_stack.src.platform.get_remote_url')
    def test_get_platform_gitlab_detect(self, mock_url):
        mock_url.return_value = "https://gitlab.com/owner/repo.git"
        # Since GitLab is implemented
        with patch.object(platform.GitLabPlatform, 'check_auth', return_value=True):
            plat = platform.get_platform()
            self.assertIsInstance(plat, platform.GitLabPlatform)

    @patch('git_stack.src.platform.GitHubPlatform._request')
    @patch('git_stack.src.platform.GitHubPlatform.check_auth', return_value=True)
    def test_sync_mr_create(self, mock_check, mock_request):
        # Setup: No existing PR
        mock_request.side_effect = [
            [],  # get_mr (open)
            [],  # get_mr (all/merged) -> None (safe to create)
            {'number': 123, 'html_url': 'http://url'}  # create_mr response
        ]

        info = RemoteInfo(
            host="github.com",
            owner="u",
            repo="r",
            service=GitService.GITHUB
        )
        plat = platform.GitHubPlatform(info)
        # Mock token to skip logic
        plat.token = 'abc'

        plat.sync_mr('feat', 'main')

        # Verify calls
        # 1. GET requests pulls (open)
        # 2. GET requests pulls (all) -- NEW SAFETY CHECK
        # 3. POST create
        self.assertEqual(mock_request.call_count, 3)
        create_call = mock_request.call_args_list[2]
        self.assertEqual(create_call[0][0], "POST")
        self.assertEqual(create_call[0][2]['base'], 'main')

    @patch('git_stack.src.platform.GitHubPlatform._request')
    @patch('git_stack.src.platform.GitHubPlatform.check_auth', return_value=True)
    def test_sync_mr_update(self, mock_check, mock_request):
        # Setup: Existing PR with wrong base
        mock_request.side_effect = [
            [{'number': 123, 'base': {'ref': 'old_base'}}],  # get_mr
            {}  # patch response
        ]

        info = RemoteInfo(
            host="github.com",
            owner="u",
            repo="r",
            service=GitService.GITHUB
        )
        plat = platform.GitHubPlatform(info)
        plat.token = 'abc'

        plat.sync_mr('feat', 'new_base')

        # Verify calls
        update_call = mock_request.call_args_list[1]
        self.assertEqual(update_call[0][0], "PATCH")
        self.assertEqual(update_call[0][1], "pulls/123")
        self.assertEqual(update_call[0][2]['base'], 'new_base')

    @patch('git_stack.src.platform.GitHubPlatform._request')
    @patch('git_stack.src.platform.GitHubPlatform.check_auth', return_value=True)
    def test_platform_reuse_logic_ancient(self, mock_check, mock_request):
        """Test that ancient merged PRs allow new PR creation."""
        # Setup specific platform instance
        info = platform.RemoteInfo(
            host="github.com", owner="u", repo="r", service=GitService.GITHUB)
        plat = platform.GitHubPlatform(info)
        plat.token = 'tok'

        ancient_date = (datetime.now(timezone.utc) -
                        timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")

        mock_request.side_effect = [
            [],  # get_mr(open)
            # get_mr(all)
            [{'number': 99, 'state': 'merged', 'closed_at': ancient_date}],
            {'number': 100, 'html_url': 'new_url'}  # create result
        ]

        plat.sync_mr('feat', 'main')

        # We expect 3 calls: Open check, All check, Create
        self.assertEqual(mock_request.call_count, 3)
        self.assertEqual(mock_request.call_args_list[2][0][0], "POST")

    @patch('git_stack.src.platform.GitHubPlatform._request')
    @patch('git_stack.src.platform.GitHubPlatform.check_auth', return_value=True)
    def test_platform_reuse_logic_recent(self, mock_check, mock_request):
        """Test that recent merged PRs block creation."""
        info = platform.RemoteInfo(
            host="github.com", owner="u", repo="r", service=GitService.GITHUB)
        plat = platform.GitHubPlatform(info)
        plat.token = 'tok'

        recent_date = (datetime.now(timezone.utc) -
                       timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

        mock_request.side_effect = [
            [],  # get_mr(open)
            # get_mr(all)
            [{'number': 99, 'state': 'merged', 'closed_at': recent_date}],
        ]

        plat.sync_mr('feat', 'main')

        # We expect 2 calls: Open check, All check
        # NO Create
        self.assertEqual(mock_request.call_count, 2)
