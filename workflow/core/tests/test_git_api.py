import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pygit2
from core.git_api import (
    CommandError,
    GitRepository,
    GitService,
    RemoteInfo,
    normalize_domain,
    parse_remote_url,
    resolve_ssh_alias,
)


class TestGitRemoteParsing(unittest.TestCase):
    """Tests for the standalone remote parsing utilities."""

    def test_normalize_domain(self):
        self.assertEqual(normalize_domain("ssh.github.com"), "github.com")
        self.assertEqual(normalize_domain("altssh.gitlab.com"), "gitlab.com")
        self.assertEqual(normalize_domain("bitbucket.org"), "bitbucket.org")

    def test_parse_ssh_scp_style(self):
        url = "git@github.com:owner/repo.git"
        info = parse_remote_url(url)
        self.assertIsNotNone(info)
        self.assertEqual(info.host, "github.com")
        self.assertEqual(info.owner, "owner")
        self.assertEqual(info.repo, "repo")
        self.assertEqual(info.service, GitService.GITHUB)
        self.assertEqual(info.project_path, "owner/repo")

    def test_parse_https_uri(self):
        url = "https://gitlab.com/group/subgroup/project.git"
        info = parse_remote_url(url)
        self.assertIsNotNone(info)
        self.assertEqual(info.host, "gitlab.com")
        self.assertEqual(info.owner, "group/subgroup")
        self.assertEqual(info.repo, "project")
        self.assertEqual(info.service, GitService.GITLAB)

    def test_parse_ssh_uri(self):
        url = "ssh://git@my-gitlab-instance.com:2222/my-group/my-project.git"
        info = parse_remote_url(url)
        self.assertIsNotNone(info)
        self.assertEqual(info.host, "my-gitlab-instance.com")
        self.assertEqual(info.owner, "my-group")
        self.assertEqual(info.repo, "my-project")
        self.assertEqual(info.service, GitService.GITLAB)

    def test_parse_unknown_service(self):
        url = "https://myserver.com/user/repo.git"
        info = parse_remote_url(url)
        self.assertIsNotNone(info)
        self.assertEqual(info.service, GitService.AUTO)

    def test_parse_invalid_url(self):
        self.assertIsNone(parse_remote_url("invalid-url"))
        self.assertIsNone(parse_remote_url("http://domain-only.com"))


class BaseGitTest(unittest.TestCase):
    """Base class for git repo tests."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.path = Path(self.test_dir)
        self.signature = pygit2.Signature("Test User", "test@example.com")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _setup_repo(self, init=True) -> GitRepository:
        """Helper to init repo and configure user."""
        if init:
            repo = GitRepository.init(self.path)
            # Disable hooks to avoid test hang/failures on systems with global hooks
            repo.set_config("core.hooksPath", "/dev/null")
            repo.set_config("user.name", "Test User")
            repo.set_config("user.email", "test@example.com")
            # For submodule tests involving file protocol
            repo.set_config("protocol.file.allow", "always")
            return repo
        return GitRepository(self.path)

    def _create_file_and_commit(
        self, repo: GitRepository, filename: str, content: str, msg: str = "msg"
    ):
        """Create file and commit it."""
        file_path = repo.root_dir / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        repo.add([str(file_path)])
        repo.commit(msg)
        return repo.get_head_commit()


class TestGitRepositoryBasics(BaseGitTest):
    """Tests for basic repo lifecycle and config."""

    def test_init_and_validity(self):
        api = self._setup_repo(init=False)
        self.assertFalse(api.is_valid)
        api = self._setup_repo(init=True)
        self.assertTrue(api.is_valid)
        self.assertTrue(api.root_dir.exists())

    def test_config_operations_complex(self):
        api = self._setup_repo()
        api.set_config("test.foo", "bar", scope="local")
        self.assertEqual(api.get_config("test.foo"), "bar")

        # Test unset
        api.unset_config("test.foo", scope="local")
        self.assertIsNone(api.get_config("test.foo"))

        # Test multivalue set (CLI behavior usually overwrites unless --add)
        # We need to verify if set_config uses --add or simple set
        # Our impl uses plain 'config key value', which overwrites single value.
        api.set_config("test.multi", "v1")
        self.assertEqual(api.get_config("test.multi"), "v1")

        # Manually add multiple values via run_git_cmd to test get_config_all
        api.run_git_cmd(["config", "--add", "test.multi", "v2"])
        values = api.get_config_all("test.multi")
        self.assertEqual(len(values), 2)
        self.assertIn("v1", values)
        self.assertIn("v2", values)

    def test_sparse_checkout(self):
        api = self._setup_repo()
        api.set_config("core.sparseCheckout", "true")
        self.assertTrue(api.is_sparse_checkout())
        api.set_config("core.sparseCheckout", "false")
        self.assertFalse(api.is_sparse_checkout())

    def test_relpath_complex(self):
        api = self._setup_repo()
        # Normal
        self.assertEqual(str(api.relpath(self.path / "foo")), "foo")
        # Nested
        self.assertEqual(str(api.relpath(self.path / "a/b/c")), "a/b/c")
        # Dot
        self.assertEqual(str(api.relpath(self.path)), ".")
        # Parent references should be resolved if possible or handled?
        # Path.relative_to with .. usually fails if not strictly below.
        # Our impl returns original if fail.
        outside = Path("/tmp/outside")
        self.assertEqual(api.relpath(outside), outside)


class TestGitRepositoryStatus(BaseGitTest):
    """Tests for repository status (dirty checking)."""

    def test_is_dirty_detailed(self):
        api = self._setup_repo()

        # Unborn HEAD, clean
        self.assertFalse(api.is_dirty())

        # 1. Untracked file
        (self.path / "untracked.txt").write_text("u")
        self.assertTrue(api.is_dirty(untracked=True))
        self.assertFalse(api.is_dirty(untracked=False))

        # 2. Stage new file
        api.add(["untracked.txt"])
        self.assertTrue(api.is_dirty(untracked=False))  # Staged is dirty

        api.commit("c1")
        self.assertFalse(api.is_dirty(untracked=True))

        # 3. Modified file (Unstaged)
        (self.path / "untracked.txt").write_text("mod")
        self.assertTrue(api.is_dirty(untracked=False))

        # 4. Modified file (Staged)
        api.add(["untracked.txt"])
        self.assertTrue(api.is_dirty(untracked=False))
        api.commit("c2")

        # 5. Deleted file
        os.remove(self.path / "untracked.txt")
        self.assertTrue(api.is_dirty(untracked=False))


class TestGitRepositoryBranching(BaseGitTest):
    """Tests for branching, checkout, ref resolution."""

    def test_basic_branching(self):
        api = self._setup_repo()
        c1 = self._create_file_and_commit(api, "f1", "c1")
        self.assertEqual(api.get_head_branch(), "main")

        # Create new branch
        api.checkout("HEAD", create_branch="feature")
        self.assertEqual(api.get_head_branch(), "feature")

        # Commit on feature
        c2 = self._create_file_and_commit(api, "f2", "c2")
        self.assertNotEqual(c1, c2)

        # Switch back
        api.checkout("main")
        self.assertEqual(api.get_head_branch(), "main")
        self.assertEqual(api.get_head_commit(), c1)

    def test_checkout_detached(self):
        api = self._setup_repo()
        c1 = self._create_file_and_commit(api, "f1", "c1")

        # Checkout commit hash -> Detached HEAD
        api.checkout(c1)
        self.assertIsNone(api.get_head_branch())
        self.assertEqual(api.get_head_commit(), c1)

        # Checkout branch again
        api.checkout("main")
        self.assertEqual(api.get_head_branch(), "main")

    def test_force_checkout_dirty(self):
        api = self._setup_repo()
        self._create_file_and_commit(api, "f1", "c1")
        api.checkout("HEAD", create_branch="other")

        # Make content different on 'other'
        (self.path / "f1").write_text("other_content")
        api.add(["f1"])
        api.commit("change on other")

        api.checkout("main")

        # Modify f1 on main (dirty)
        (self.path / "f1").write_text("dirty_main")

        # Try checkout other without force -> Fail
        # Because f1 differs between main(c1) and other(change), git must update it.
        # But it's dirty, so it should fail.
        with self.assertRaises(CommandError):
            api.checkout("other")

        # With force
        api.checkout("other", force=True)
        self.assertEqual(api.get_head_branch(), "other")
        # Should have reset f1 to what is in 'other' branch
        self.assertEqual((self.path / "f1").read_text(), "other_content")

    def test_resolve_rev(self):
        api = self._setup_repo()
        c1 = self._create_file_and_commit(api, "f1", "c1")

        # Hash
        self.assertEqual(api.resolve_rev(c1), c1)
        # Branch name
        self.assertEqual(api.resolve_rev("main"), c1)
        # HEAD
        self.assertEqual(api.resolve_rev("HEAD"), c1)
        # Invalid
        self.assertIsNone(api.resolve_rev("invalid-ref"))

    def test_resolve_default_branch(self):
        api = self._setup_repo()

        # Default fallback
        self.assertEqual(api.resolve_default_branch(), "main")

        # Configured
        api.set_config("workflow.base-branch", "develop")
        self.assertEqual(api.resolve_default_branch(), "develop")

        # Mock remote HEAD logic?
        # Simulating refs/remotes/origin/HEAD is hard without a real remote.
        # We can mock pygit2 lookup_reference if needed, but integration test with local remote is better.
        # See TestGitRepositorySync for that.


class TestGitRepositoryRemotes(BaseGitTest):
    """Tests for remote management."""

    def test_crud_remotes(self):
        api = self._setup_repo()
        api.add_remote("origin", "https://example.com/repo.git")
        self.assertEqual(api.get_remote_url("origin"), "https://example.com/repo.git")

        api.set_remote_url("origin", "ssh://git@example.com/repo.git")
        self.assertEqual(api.get_remote_url("origin"), "ssh://git@example.com/repo.git")

        api.set_remote_url("origin", "ssh://git@example.com/push.git", push=True)
        self.assertEqual(
            api.get_remote_url("origin", push=True), "ssh://git@example.com/push.git"
        )


class TestGitRepositorySync(BaseGitTest):
    """Tests for sync operations (fetch, push, merge) using local remotes."""

    def setUp(self):
        super().setUp()
        # Create a "remote" repo
        self.remote_path = Path(tempfile.mkdtemp())
        self.remote_repo = GitRepository.init(self.remote_path, initial_branch="main")
        self.remote_repo.set_config("core.hooksPath", "/dev/null")
        self.remote_repo.set_config("user.name", "Remote User")
        self.remote_repo.set_config("user.email", "remote@example.com")
        # Allow push to non-bare repo for testing
        self.remote_repo.set_config("receive.denyCurrentBranch", "ignore")

        # Local repo
        self.local_repo = self._setup_repo()
        self.local_repo.add_remote("origin", str(self.remote_path))

    def tearDown(self):
        shutil.rmtree(self.remote_path)
        super().tearDown()

    def test_push_fetch_merge(self):
        # Commit on local
        c1 = self._create_file_and_commit(self.local_repo, "f1", "c1")

        # Push to origin
        self.local_repo.push("origin", "main")

        # Verify remote has c1
        self.assertEqual(self.remote_repo.get_head_commit(), c1)

        # Commit on remote
        c2 = self._create_file_and_commit(self.remote_repo, "f2", "c2")

        # Fetch on local
        self.local_repo.fetch("origin")

        # Verify remote tracking branch
        remote_head = self.local_repo.resolve_rev("remotes/origin/main")
        self.assertEqual(remote_head, c2)

        # Merge FF
        self.local_repo.merge("origin/main", fast_forward_only=True)
        self.assertEqual(self.local_repo.get_head_commit(), c2)
        self.assertTrue((self.local_repo.root_dir / "f2").exists())

    def test_resolve_default_branch_with_remote(self):
        # Create commit on remote to ensure HEAD is valid
        self._create_file_and_commit(self.remote_repo, "remote_file", "initial")

        # HEAD on remote is main
        # We need to fetch invalid refs/remotes/origin/HEAD for resolve_default_branch heuristic to work
        # 'git fetch' usually implies fetching refs/heads/*
        # To get origin/HEAD, we typically run 'git remote set-head origin -a'
        self.local_repo.fetch("origin")
        self.local_repo.run_git_cmd(["remote", "set-head", "origin", "-a"])

        default = self.local_repo.resolve_default_branch("origin")
        self.assertEqual(default, "main")


class TestGitRepositoryFlows(BaseGitTest):
    """Tests for complex workflows."""

    def test_safe_checkout_dirty_flow(self):
        api = self._setup_repo()
        self._create_file_and_commit(api, "base", "base")
        api.checkout("HEAD", create_branch="feature")
        api.checkout("main")

        # Dirty
        (self.path / "base").write_text("mod")

        # Safe checkout to feature (autostash)
        with api.safe_checkout("feature", auto_stash=True):
            # Inside: should be feature, clean
            self.assertEqual(api.get_head_branch(), "feature")
            self.assertFalse(api.is_dirty())
            self.assertEqual((self.path / "base").read_text(), "base")

        # After exit: still feature, but stash popped -> dirty
        self.assertEqual(api.get_head_branch(), "feature")
        self.assertTrue(api.is_dirty())
        self.assertEqual((self.path / "base").read_text(), "mod")

    def test_safe_checkout_no_stash_dirty_fail(self):
        api = self._setup_repo()
        self._create_file_and_commit(api, "base", "base")
        (self.path / "base").write_text("mod")

        with self.assertRaises(RuntimeError):
            with api.safe_checkout("main", auto_stash=False):
                pass

    def test_stash_push_pop(self):
        api = self._setup_repo()
        self._create_file_and_commit(api, "f", "c")
        (self.path / "f").write_text("mod")

        # Stash
        self.assertTrue(api.stash("test"))
        self.assertFalse(api.is_dirty())

        # Pop
        api.stash_pop()
        self.assertTrue(api.is_dirty())

    def test_stash_empty(self):
        api = self._setup_repo()
        self._create_file_and_commit(api, "f", "c")
        self.assertFalse(api.stash("empty"))


class TestGitRepositorySubmodules(BaseGitTest):
    """Tests for submodule operations."""

    def setUp(self):
        super().setUp()
        self.sub_path = Path(tempfile.mkdtemp())
        self.sub_repo = GitRepository.init(self.sub_path)
        self.sub_repo.set_config("core.hooksPath", "/dev/null")
        self.sub_repo.set_config("user.name", "Sub User")
        self.sub_repo.set_config("user.email", "sub@example.com")
        self._create_file_and_commit(self.sub_repo, "lib.py", "lib")

    def tearDown(self):
        shutil.rmtree(self.sub_path)
        super().tearDown()

    def test_submodule_lifecycle(self):
        repo = self._setup_repo()

        # Add submodule
        repo.run_git_cmd(
            [
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(self.sub_path),
                "libs/sub",
            ]
        )
        repo.commit("add sub")

        # List
        subs = repo.get_submodules()
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0].path, "libs/sub")

        # Modify submodule
        (repo.root_dir / "libs/sub/lib.py").write_text("mod")
        self.assertTrue(repo.is_dirty())

        # Update (noop check)
        repo.update_submodules()


class TestGitRepositoryAdvanced(BaseGitTest):
    """Tests for advanced features like log parsing, multi-remotes."""

    def test_get_commits_graph(self):
        """Test getting commits from a range."""
        api = self._setup_repo()

        # Initial commit
        c1 = self._create_file_and_commit(
            api, "f1", "c1", "Initial commit\n\nBody line 1\nBody line 2"
        )

        # Second commit
        c2 = self._create_file_and_commit(api, "f2", "c2", "Feature commit")

        # Third commit
        c3 = self._create_file_and_commit(api, "f3", "c3", "Fix commit")

        # Test range c1..c3
        # Start is exclusive, End is inclusive usually in git walk?
        # git log c1..c3 shows c3, c2.
        # API expects rev_range string.
        commits = api.get_commits(f"{c1}..{c3}")

        self.assertEqual(len(commits), 2)
        # Order is topological (newest first)
        self.assertEqual(commits[0].oid, c3)
        self.assertEqual(commits[0].subject, "Fix commit")
        self.assertEqual(commits[1].oid, c2)

        # Test single commit
        commits = api.get_commits(c1)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0].oid, c1)
        self.assertEqual(commits[0].body, "Body line 1\nBody line 2")

    def test_get_branches(self):
        """Test listing branches."""
        api = self._setup_repo()
        self._create_file_and_commit(api, "f1", "c1")

        api.checkout("HEAD", create_branch="feature")
        api.checkout("HEAD", create_branch="bugfix")

        branches = api.get_branches()
        self.assertIn("main", branches)
        self.assertIn("feature", branches)
        self.assertIn("bugfix", branches)
        self.assertEqual(len(branches), 3)

    def test_remote_advanced_urls(self):
        """Test multiple URLs and renaming."""
        api = self._setup_repo()
        api.add_remote("origin", "https://example.com/repo.git")

        # Rename
        api.rename_remote("origin", "upstream")
        self.assertEqual(api.get_remote_url("upstream"), "https://example.com/repo.git")
        self.assertIsNone(api.get_remote_url("origin"))

        # Multiple Push URLs
        api.set_remote_url(
            "upstream", "https://mirror1.com/repo.git", push=True, add=True
        )
        api.set_remote_url(
            "upstream", "https://mirror2.com/repo.git", push=True, add=True
        )

        urls = api.get_remote_urls("upstream", push=True)
        # origin URL is also a push URL by default if not strictly distinct?
        # Checking git behavior: if push URLs are defined, fetch URL is not used for push.
        # So we expect mirror1 and mirror2.
        self.assertIn("https://mirror1.com/repo.git", urls)
        self.assertIn("https://mirror2.com/repo.git", urls)

    def test_push_arguments(self):
        """Test push arguments (mocking _run_git)."""
        api = self._setup_repo()
        with patch.object(api, "_run_git") as mock_run:
            api.push("origin", "main", force_with_lease=True)
            mock_run.assert_called_with(
                ["push", "--force-with-lease", "origin", "main"]
            )

    def test_default_branch_stack_config(self):
        """Test stack.base config priority."""
        api = self._setup_repo()
        api.set_config("stack.base", "trunk")
        self.assertEqual(api.resolve_default_branch(), "trunk")


if __name__ == "__main__":
    unittest.main()
