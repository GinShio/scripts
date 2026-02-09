import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from core.git_remotes import GitService, RemoteInfo

from git_stack.src import platform


class TestPlatform(unittest.TestCase):
    @patch("git_stack.src.platform.get_fork_remote_url")
    @patch("git_stack.src.platform.get_target_remote_url")
    def test_get_platform_github(self, mock_target_url, mock_fork_url):
        mock_target_url.return_value = "https://github.com/owner/repo.git"
        mock_fork_url.return_value = "https://github.com/myuser/repo.git"
        with patch.object(platform.GitHubPlatform, "check_auth", return_value=True):
            plat = platform.get_platform()
            self.assertIsInstance(plat, platform.GitHubPlatform)
            self.assertEqual(plat.repo, "owner/repo")
            self.assertEqual(plat.fork_owner, "myuser")

    @patch("git_stack.src.platform.get_target_remote_url")
    def test_get_platform_gitlab_detect(self, mock_url):
        mock_url.return_value = "https://gitlab.com/owner/repo.git"
        # Since GitLab is implemented
        with patch.object(platform.GitLabPlatform, "check_auth", return_value=True):
            plat = platform.get_platform()
            self.assertIsInstance(plat, platform.GitLabPlatform)

    @patch("git_stack.src.platform.GitHubPlatform._request")
    @patch("git_stack.src.platform.GitHubPlatform.check_auth", return_value=True)
    def test_sync_mr_create_cross_repo(self, mock_check, mock_request):
        # Setup: No existing PR, cross repo
        mock_request.side_effect = [
            [],  # get_mr (open)
            [],  # get_mr (all/merged)
            {"number": 123, "html_url": "http://url"},  # create_mr response
        ]

        info = RemoteInfo(
            host="github.com", owner="upstream_org", repo="r", service=GitService.GITHUB
        )
        plat = platform.GitHubPlatform(info, fork_owner="myuser")
        plat.token = "abc"

        plat.sync_mr(
            "feat",
            "main",
            draft=False,
            title="feat",
            body="Stack PR managed by git-stack.",
        )

        # Verify creation uses user:branch
        create_call = mock_request.call_args_list[2]
        self.assertEqual(create_call[0][0], "POST")
        payload = create_call[0][2]
        self.assertEqual(payload["head"], "myuser:feat")
        self.assertEqual(payload["base"], "main")

    @patch("git_stack.src.platform.GitHubPlatform._request")
    @patch("git_stack.src.platform.GitHubPlatform.check_auth", return_value=True)
    def test_sync_mr_update(self, mock_check, mock_request):
        # Setup: Existing PR with wrong base
        mock_request.side_effect = [
            [{"number": 123, "base": {"ref": "old_base"}}],  # get_mr
            {},  # patch response
        ]

        info = RemoteInfo(
            host="github.com", owner="u", repo="r", service=GitService.GITHUB
        )
        plat = platform.GitHubPlatform(info)
        plat.token = "abc"

        plat.sync_mr(
            "feat",
            "new_base",
            draft=False,
            title="feat",
            body="Stack PR managed by git-stack.",
        )

        # Verify calls
        update_call = mock_request.call_args_list[1]
        self.assertEqual(update_call[0][0], "PATCH")
        self.assertEqual(update_call[0][1], "pulls/123")
        self.assertEqual(update_call[0][2]["base"], "new_base")

    @patch("git_stack.src.platform.GitHubPlatform._request")
    @patch("git_stack.src.platform.GitHubPlatform.check_auth", return_value=True)
    def test_platform_reuse_logic_ancient(self, mock_check, mock_request):
        """Test that ancient merged PRs allow new PR creation."""
        # Setup specific platform instance
        info = platform.RemoteInfo(
            host="github.com", owner="u", repo="r", service=GitService.GITHUB
        )
        plat = platform.GitHubPlatform(info)
        plat.token = "tok"

        ancient_date = (datetime.now(timezone.utc) - timedelta(days=200)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        mock_request.side_effect = [
            [],  # get_mr(open)
            # get_mr(all)
            [{"number": 99, "state": "merged", "closed_at": ancient_date}],
            {"number": 100, "html_url": "new_url"},  # create result
        ]

        plat.sync_mr(
            "feat",
            "main",
            draft=False,
            title="feat",
            body="Stack PR managed by git-stack.",
        )

        # We expect 3 calls: Open check, All check, Create
        self.assertEqual(mock_request.call_count, 3)
        self.assertEqual(mock_request.call_args_list[2][0][0], "POST")

    @patch("git_stack.src.platform.GitHubPlatform._request")
    @patch("git_stack.src.platform.GitHubPlatform.check_auth", return_value=True)
    def test_platform_reuse_logic_recent(self, mock_check, mock_request):
        """Test that recent merged PRs block creation."""
        info = platform.RemoteInfo(
            host="github.com", owner="u", repo="r", service=GitService.GITHUB
        )
        plat = platform.GitHubPlatform(info)
        plat.token = "tok"

        recent_date = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        mock_request.side_effect = [
            [],  # get_mr(open)
            # get_mr(all)
            [{"number": 99, "state": "merged", "closed_at": recent_date}],
        ]

        plat.sync_mr(
            "feat",
            "main",
            draft=False,
            title="feat",
            body="Stack PR managed by git-stack.",
        )

        # We expect 2 calls: Open check, All check
        # NO Create
        self.assertEqual(mock_request.call_count, 2)

    @patch("git_stack.src.platform.get_target_remote_url")
    def test_get_platform_gitea(self, mock_url):
        mock_url.return_value = "https://gitea.example.com/owner/repo.git"
        with patch.object(platform.GiteaPlatform, "check_auth", return_value=True):
            plat = platform.get_platform()
            self.assertIsInstance(plat, platform.GiteaPlatform)

    @patch("git_stack.src.platform.get_target_remote_url")
    def test_get_platform_bitbucket(self, mock_url):
        mock_url.return_value = "https://bitbucket.org/workspace/repo.git"
        with patch.object(platform.BitbucketPlatform, "check_auth", return_value=True):
            plat = platform.get_platform()
            self.assertIsInstance(plat, platform.BitbucketPlatform)

    @patch("git_stack.src.platform.get_target_remote_url")
    def test_get_platform_azure(self, mock_url):
        mock_url.return_value = "https://dev.azure.com/org/project/_git/repo"
        with patch.object(platform.AzurePlatform, "check_auth", return_value=True):
            plat = platform.get_platform()
            self.assertIsInstance(plat, platform.AzurePlatform)

    @patch("git_stack.src.platform.GitLabPlatform._request")
    @patch("git_stack.src.platform.GitLabPlatform.check_auth", return_value=True)
    def test_gitlab_sync_mr_create(self, mock_check, mock_request):
        # Setup: No existing MR
        mock_request.side_effect = [
            [],  # get_mr(open)
            [],  # get_mr(merged/closed)
            {"iid": 321, "web_url": "http://gl"},
        ]

        info = RemoteInfo(
            host="gitlab.com", owner="u", repo="r", service=GitService.GITLAB
        )
        plat = platform.GitLabPlatform(info)
        plat.token = "abc"

        plat.sync_mr("feat", "main", draft=False, title="feat", body="body")

        # GitLab checks open + merged + closed + create => 4
        self.assertEqual(mock_request.call_count, 4)
        create_call = mock_request.call_args_list[3]
        self.assertEqual(create_call[0][0], "POST")
        self.assertIn("merge_requests", create_call[0][1])

    @patch("git_stack.src.platform.GiteaPlatform._request")
    @patch("git_stack.src.platform.GiteaPlatform.check_auth", return_value=True)
    def test_gitea_sync_mr_create(self, mock_check, mock_request):
        # Gitea now attempts optimization: GET pulls/{base}/{head}
        # If that fails (404/Exception), it falls back to GET pulls?state=...

        # We need 5 responses:
        # 1. get_mr(open) -> Opt: Exception (404)
        # 2. get_mr(open) -> Fallback: []
        # 3. get_mr(closed) -> Opt: Exception (404)
        # 4. get_mr(closed) -> Fallback: []
        # 5. create_mr -> Success

        mock_request.side_effect = [
            Exception("404"),  # Opt open
            [],  # Fallback open
            Exception("404"),  # Opt closed
            [],  # Fallback closed
            {"number": 55, "html_url": "http://gitea"},  # Create
        ]

        info = RemoteInfo(
            host="gitea.example.com", owner="u", repo="r", service=GitService.GITEA
        )
        plat = platform.GiteaPlatform(info)
        plat.token = "tok"

        plat.sync_mr("feat", "main", draft=False, title="feat", body="body")

        self.assertEqual(mock_request.call_count, 5)
        create_call = mock_request.call_args_list[4]
        self.assertEqual(create_call[0][0], "POST")

    @patch("git_stack.src.platform.BitbucketPlatform._request")
    @patch("git_stack.src.platform.BitbucketPlatform.check_auth", return_value=True)
    def test_bitbucket_sync_mr_create(self, mock_check, mock_request):
        # GET returns empty values list, then POST
        mock_request.side_effect = [
            {"values": []},
            {"id": 7, "links": {"html": {"href": "http://bb"}}},
        ]

        info = RemoteInfo(
            host="bitbucket.org", owner="ws", repo="r", service=GitService.BITBUCKET
        )
        plat = platform.BitbucketPlatform(info)
        plat.token = "tok"

        plat.sync_mr("feat", "main", draft=False, title="feat", body="body")

        # Bitbucket does OPEN + MERGED + DECLINED + create => 4
        self.assertEqual(mock_request.call_count, 4)
        create_call = mock_request.call_args_list[3]
        self.assertEqual(create_call[0][0], "POST")

    @patch("git_stack.src.platform.AzurePlatform._request")
    @patch("git_stack.src.platform.AzurePlatform.check_auth", return_value=True)
    def test_azure_sync_mr_create(self, mock_check, mock_request):
        mock_request.side_effect = [
            {"value": []},
            {"pullRequestId": 9, "webUrl": "http://az"},
        ]

        info = RemoteInfo(
            host="dev.azure.com",
            owner="org/project",
            repo="r",
            service=GitService.AZURE,
        )
        plat = platform.AzurePlatform(info)
        plat.token = "tok"

        plat.sync_mr("feat", "main", draft=False, title="feat", body="body")

        # Azure: active + completed + create => 3
        self.assertEqual(mock_request.call_count, 3)
        create_call = mock_request.call_args_list[2]
        self.assertEqual(create_call[0][0], "POST")
