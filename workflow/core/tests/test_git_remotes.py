import unittest
from unittest.mock import patch

from core.git_remotes import (
    GitService,
    RemoteInfo,
    normalize_domain,
    parse_remote_url,
    resolve_ssh_alias,
)


class TestGitRemotes(unittest.TestCase):
    def test_normalize_domain(self):
        self.assertEqual(normalize_domain("ssh.github.com"), "github.com")
        self.assertEqual(normalize_domain("altssh.gitlab.com"), "gitlab.com")
        self.assertEqual(normalize_domain("bitbucket.org"), "bitbucket.org")

    def test_parse_ssh_scp_style(self):
        # user@host:path/to/repo.git
        url = "git@github.com:owner/repo.git"
        info = parse_remote_url(url)
        self.assertIsNotNone(info)
        self.assertEqual(info.host, "github.com")
        self.assertEqual(info.owner, "owner")
        self.assertEqual(info.repo, "repo")
        self.assertEqual(info.service, GitService.GITHUB)

    def test_parse_https_uri(self):
        # https://host/path/to/repo.git
        url = "https://gitlab.com/group/subgroup/project.git"
        info = parse_remote_url(url)
        self.assertIsNotNone(info)
        self.assertEqual(info.host, "gitlab.com")
        self.assertEqual(info.owner, "group/subgroup")
        self.assertEqual(info.repo, "project")
        self.assertEqual(info.service, GitService.GITLAB)

    def test_parse_ssh_uri(self):
        # ssh://user@host:port/path/to/repo
        url = "ssh://git@my-gitlab-instance.com:2222/my-group/my-project.git"
        info = parse_remote_url(url)
        self.assertIsNotNone(info)
        self.assertEqual(info.host, "my-gitlab-instance.com")
        self.assertEqual(info.owner, "my-group")
        self.assertEqual(info.repo, "my-project")
        self.assertEqual(info.service, GitService.GITLAB)

    def test_parse_alias_resolve_mock(self):
        # Mocking resolve_ssh_alias
        with patch("core.git_remotes.resolve_ssh_alias") as mock_resolve:
            mock_resolve.return_value = "github.com"
            url = "gh-alias:owner/repo.git"
            info = parse_remote_url(url)
            self.assertIsNotNone(info)
            self.assertEqual(info.host, "github.com")
            self.assertEqual(info.service, GitService.GITHUB)

    def test_parse_unknown_service(self):
        url = "https://myserver.com/user/repo.git"
        info = parse_remote_url(url)
        self.assertIsNotNone(info)
        self.assertEqual(info.service, GitService.AUTO)
        self.assertFalse(info.is_github)
        self.assertFalse(info.is_gitlab)

    def test_parse_invalid_url(self):
        self.assertIsNone(parse_remote_url("invalid-url"))
        self.assertIsNone(parse_remote_url("http://domain-only.com"))
