import unittest
from unittest.mock import ANY, MagicMock, patch

from core.git_api import GitService, RemoteInfo

from git_stack.src import machete, platform, sync


class TestSync(unittest.TestCase):
    def setUp(self):
        # Mocking for Platform checks (merged from test_topology_update)
        self.info = RemoteInfo(
            host="github.com", owner="u", repo="r", service=GitService.GITHUB
        )
        # Mock platform auth check always true for ease
        patcher = patch(
            "git_stack.src.platform.GitHubPlatform.check_auth", return_value=True
        )
        self.mock_auth = patcher.start()
        self.addCleanup(patcher.stop)

        self.plat = platform.GitHubPlatform(self.info)
        self.plat.token = "abc"

    @patch("git_stack.src.sync.run_git")
    def test_push_branch(self, mock_run_git):
        sync.push_branch("feature")
        mock_run_git.assert_called_with(
            ["push", "origin", "feature", "--force-with-lease"], check=True
        )

    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.get_refs_map")
    @patch("git_stack.src.sync.push_branch")
    def test_push_stack(self, mock_push_branch, mock_refs, mock_parse):
        # Setup graph: main -> feat1
        root = machete.MacheteNode("main")
        feat = machete.MacheteNode("feat1")
        feat.parent = root
        root.children.append(feat)

        mock_parse.return_value = {"main": root, "feat1": feat}
        mock_refs.return_value = {"main": "111", "feat1": "222"}

        sync.sync_stack(push=True, pr=False)

        # push_branch should be called for feat1
        mock_push_branch.assert_called_with("feat1")

    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.get_refs_map")
    @patch("git_stack.src.sync.get_platform")
    def test_create_stack_prs(
        self, mock_get_platform, mock_refs, mock_parse, mock_run_git
    ):
        # Setup graph: main -> feat1
        root = machete.MacheteNode("main")
        feat = machete.MacheteNode("feat1")
        feat.parent = root
        root.children.append(feat)

        mock_parse.return_value = {"main": root, "feat1": feat}
        mock_refs.return_value = {"main": "111", "feat1": "222"}

        mock_plat = MagicMock()
        mock_get_platform.return_value = mock_plat

        # Mock run_git to return empty so it falls back to branch name
        mock_run_git.return_value = ""

        sync.sync_stack(push=False, pr=True)

        # sync_mr should be called for feat1 (parent=main), draft=False since it's base
        called = mock_plat.sync_mr.call_args
        args, kwargs = called
        self.assertEqual(args[0], "feat1")
        self.assertEqual(args[1], "main")
        self.assertEqual(kwargs.get("draft"), False)
        # Since platform API now requires title/body, ensure they are present
        self.assertIn("title", kwargs)
        self.assertIn("body", kwargs)

    # --- Tests from test_topology_update ---

    @patch("git_stack.src.platform.GitHubPlatform._request")
    def test_reparent_logic(self, mock_request):
        """
        Scenario: Local stack changed A->B->C to A->C.
        Remote PR for C has base B. Machete says base is A.
        'create' command logic (sync_mr) should update C's base to A.
        """
        # Mock get_mr response: PR exists, base is 'old_base' (B)
        mock_request.side_effect = [
            [{"number": 123, "base": {"ref": "old_base"}}],  # get_mr
            {},  # PATCH pulls/123
        ]

        # Use sync_mr on self.plat (GitHubPlatform)
        self.plat.sync_mr(
            branch="feature-C",
            base_branch="feature-A",
            draft=False,
            title="feature-C",
            body="Stack PR managed by git-stack.",
        )

        # Check that PATCH was called with base='feature-A'
        # Call 0 is get_mr
        # Call 1 is update
        self.assertEqual(mock_request.call_count, 2)
        patch_call = mock_request.call_args_list[1]
        self.assertEqual(patch_call[0][0], "PATCH")
        self.assertEqual(patch_call[0][2]["base"], "feature-A")

    @patch("git_stack.src.platform.GitHubPlatform._request")
    def test_merged_branch_reuse_prevention(self, mock_request):
        """
        Scenario: Local stack has 'feature-A'.
        Remote has an OLD Merged PR for 'feature-A'.
        'sync --pr' logic SHOULD NOW detect this and SKIP creation to prevent duplication/spam.
        """
        mock_request.side_effect = [
            [],  # get_mr (open) -> None
            # get_mr (all) -> Found Merged PR!
            [{"number": 125, "state": "merged"}],
        ]

        # Use sync_mr on self.plat (GitHubPlatform) matches test setup
        # But we need to update the logic to expect NO creation

        self.plat.sync_mr(
            branch="feature-A",
            base_branch="main",
            draft=False,
            title="feature-A",
            body="Stack PR managed by git-stack.",
        )

        # Verify:
        # 1. GET open
        # 2. GET all
        # 3. NO POST
        self.assertEqual(mock_request.call_count, 2)

        # Verify check logic
        status_check = mock_request.call_args_list[1]
        self.assertIn("state=all", status_check[0][1])

    @patch("git_stack.src.platform.GitHubPlatform._request")
    def test_merged_branch_safe_create(self, mock_request):
        """
        Scenario: Remote has NO PRs (open or merged).
        Then we create.
        """
        mock_request.side_effect = [
            [],  # get_mr (open) -> None
            [],  # get_mr (all) -> None
            {"number": 126, "html_url": "..."},  # create_mr -> Success
        ]

        self.plat.sync_mr(
            branch="feature-B",
            base_branch="main",
            draft=False,
            title="feature-B",
            body="Stack PR managed by git-stack.",
        )

        self.assertEqual(mock_request.call_count, 3)
        self.assertEqual(mock_request.call_args_list[2][0][0], "POST")


class TestSyncFeatures(unittest.TestCase):
    def setUp(self):
        # Mock machete nodes
        # root(main) -> feat1 -> feat2
        self.root = machete.MacheteNode("main")
        self.feat1 = machete.MacheteNode("feat1")
        self.feat1.parent = self.root
        self.root.children.append(self.feat1)

        self.feat2 = machete.MacheteNode("feat2")
        self.feat2.parent = self.feat1
        self.feat1.children.append(self.feat2)

        self.nodes = {"main": self.root, "feat1": self.feat1, "feat2": self.feat2}

    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.resolve_base_branch", return_value="main")
    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.get_refs_map")
    def test_sync_scope_limit(
        self,
        mock_refs,
        mock_machete,
        mock_get_platform,
        mock_resolve_base,
        mock_run_git,
    ):
        """Test that sync limits scope correctly."""
        mock_machete.return_value = self.nodes
        mock_refs.return_value = {"main": "h1", "feat1": "h2", "feat2": "h3"}
        mock_plat = MagicMock()
        mock_get_platform.return_value = mock_plat

        mock_run_git.return_value = ""  # Default empty for title derivation

        # Limit to feat2 -> Should verify parent chain up to main, then sync that tree
        sync.sync_stack(push=False, pr=True, limit_to_branch="feat2")

        # Check calls via platform
        found_feat1 = False
        found_feat2 = False
        for call in mock_plat.sync_mr.call_args_list:
            args, kwargs = call
            if args[0] == "feat1" and args[1] == "main":
                found_feat1 = True
            if args[0] == "feat2" and args[1] == "feat1":
                found_feat2 = True

        self.assertTrue(found_feat1)
        self.assertTrue(found_feat2)

    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.resolve_base_branch", return_value="main")
    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.get_refs_map")
    def test_sync_draft_logic(
        self,
        mock_refs,
        mock_machete,
        mock_get_platform,
        mock_resolve_base,
        mock_run_git,
    ):
        """Test draft status logic based on stack position."""
        mock_machete.return_value = self.nodes
        mock_refs.return_value = {"main": "h1", "feat1": "h2", "feat2": "h3"}
        mock_plat = MagicMock()
        mock_get_platform.return_value = mock_plat

        mock_run_git.return_value = ""

        sync.sync_stack(push=False, pr=True)

        # Check that calls exist with expected branch/base and draft kwargs
        found_feat1 = False
        found_feat2 = False
        for call in mock_plat.sync_mr.call_args_list:
            args, kwargs = call
            if (
                args[0] == "feat1"
                and args[1] == "main"
                and kwargs.get("draft") is False
            ):
                found_feat1 = True
            if (
                args[0] == "feat2"
                and args[1] == "feat1"
                and kwargs.get("draft") is True
            ):
                found_feat2 = True

        self.assertTrue(found_feat1)
        self.assertTrue(found_feat2)

    @patch("uuid.uuid4")
    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.resolve_base_branch", return_value="main")
    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.get_refs_map")
    def test_title_source_last(
        self,
        mock_refs,
        mock_machete,
        mock_get_platform,
        mock_resolve,
        mock_run_git,
        mock_uuid,
    ):
        """Title should be chosen from the last commit subject by default."""
        mock_machete.return_value = self.nodes
        mock_refs.return_value = {"main": "h1", "feat1": "h2", "feat2": "h3"}
        mock_plat = MagicMock()
        mock_get_platform.return_value = mock_plat

        # Mock UUIDs
        mock_uuid.side_effect = [MagicMock(hex="COMMIT"), MagicMock(hex="BODY")] * 2

        # Simulate git log returning two commits with UUID markers
        mock_run_git.return_value = (
            "GITSTACK_COMMIT_COMMIT\nFirst subject\nGITSTACK_BODY_BODY\nFirst body\n"
            "GITSTACK_COMMIT_COMMIT\nLast subject\nGITSTACK_BODY_BODY\nLast body\n"
        )

        sync.sync_stack(push=False, pr=True, title_source="last")

        # Inspect calls and ensure title/body kwarg is for the last commit
        found = False
        for call in mock_plat.sync_mr.call_args_list:
            args, kwargs = call
            if (
                kwargs.get("title") == "Last subject"
                and kwargs.get("body") == "Last body"
            ):
                found = True
        self.assertTrue(found)

    @patch("uuid.uuid4")
    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.resolve_base_branch", return_value="main")
    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.get_refs_map")
    def test_title_source_first(
        self,
        mock_refs,
        mock_machete,
        mock_get_platform,
        mock_resolve,
        mock_run_git,
        mock_uuid,
    ):
        """Title should be chosen from the first commit subject when requested."""
        mock_machete.return_value = self.nodes
        mock_refs.return_value = {"main": "h1", "feat1": "h2", "feat2": "h3"}
        mock_plat = MagicMock()
        mock_get_platform.return_value = mock_plat

        # Mock UUIDs
        mock_uuid.side_effect = [MagicMock(hex="COMMIT"), MagicMock(hex="BODY")] * 2

        mock_run_git.return_value = (
            "GITSTACK_COMMIT_COMMIT\nFirst subject\nGITSTACK_BODY_BODY\nFirst body\n"
            "GITSTACK_COMMIT_COMMIT\nLast subject\nGITSTACK_BODY_BODY\nLast body\n"
        )

        sync.sync_stack(push=False, pr=True, title_source="first")

        found = False
        for call in mock_plat.sync_mr.call_args_list:
            args, kwargs = call
            if (
                kwargs.get("title") == "First subject"
                and kwargs.get("body") == "First body"
            ):
                found = True
        self.assertTrue(found)

    @patch("subprocess.check_call")
    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.resolve_base_branch", return_value="main")
    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.get_refs_map")
    def test_title_source_custom_from_template(
        self,
        mock_refs,
        mock_machete,
        mock_get_platform,
        mock_resolve,
        mock_run_git,
        mock_check_call,
    ):
        """Custom title/body should be loaded from commit.template when present."""
        mock_machete.return_value = self.nodes
        mock_refs.return_value = {"main": "h1", "feat1": "h2"}
        mock_plat = MagicMock()
        mock_get_platform.return_value = mock_plat

        # Create a real temp file to act as commit.template
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tf:
            tf.write("Custom Title\n\nCustom body line")
            temp_path = tf.name

        # run_git will be called to query commit.template; return that path
        mock_run_git.return_value = temp_path

        try:
            sync.sync_stack(push=False, pr=True, title_source="custom")

            found = False
            for call in mock_plat.sync_mr.call_args_list:
                args, kwargs = call
                if (
                    kwargs.get("title") == "Custom Title"
                    and kwargs.get("body") == "Custom body line"
                ):
                    found = True
            self.assertTrue(found)
        finally:
            try:
                import os

                os.remove(temp_path)
            except Exception:
                pass


class TestSyncParallel(unittest.TestCase):
    def setUp(self):
        pass

    @patch("git_stack.src.sync.run_git")
    def test_push_branch_safety(self, mock_run_git):
        """Test that push_branch uses --force-with-lease."""
        sync.push_branch("my-branch", check=False)
        mock_run_git.assert_called_with(
            ["push", "origin", "my-branch", "--force-with-lease"], check=True
        )

    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.get_refs_map")
    @patch("git_stack.src.sync.resolve_base_branch")
    @patch("git_stack.src.sync.ThreadPoolExecutor")
    @patch("git_stack.src.sync.as_completed")
    def test_sync_stack_parallel_push(
        self, mock_as_completed, mock_tpe, mock_resolve, mock_refs, mock_parse
    ):
        """Test that sync_stack uses ThreadPoolExecutor for pushing."""
        # Setup graph: main -> [feat1, feat2]
        root = machete.MacheteNode("main")
        feat1 = machete.MacheteNode("feat1")
        feat2 = machete.MacheteNode("feat2")
        feat1.parent = root
        feat2.parent = root
        root.children = [feat1, feat2]

        mock_parse.return_value = {"main": root, "feat1": feat1, "feat2": feat2}
        mock_refs.return_value = {"main": "111", "feat1": "222", "feat2": "333"}
        mock_resolve.return_value = "main"

        # Setup Executor Mock
        mock_executor = MagicMock()
        mock_tpe.return_value.__enter__.return_value = mock_executor

        # Mock futures
        mock_future1 = MagicMock()
        mock_future2 = MagicMock()
        mock_executor.submit.side_effect = [mock_future1, mock_future2]
        mock_as_completed.return_value = [mock_future1, mock_future2]

        # Run sync
        sync.sync_stack(push=True, pr=False)

        # Verify Executor was called
        self.assertTrue(mock_tpe.called)
        self.assertEqual(mock_executor.submit.call_count, 2)

        submitted_args = {c[0][1] for c in mock_executor.submit.call_args_list}
        self.assertIn("feat1", submitted_args)
        self.assertIn("feat2", submitted_args)

    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.get_refs_map")
    @patch("git_stack.src.sync.resolve_base_branch")
    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.ThreadPoolExecutor")
    @patch("git_stack.src.sync.as_completed")
    def test_sync_stack_parallel_pr(
        self,
        mock_as_completed,
        mock_tpe,
        mock_get_platform,
        mock_resolve,
        mock_refs,
        mock_parse,
    ):
        """Test that sync_stack uses ThreadPoolExecutor for PR syncing."""
        root = machete.MacheteNode("main")
        feat1 = machete.MacheteNode("feat1")
        feat1.parent = root
        root.children = [feat1]

        mock_parse.return_value = {"main": root, "feat1": feat1}
        mock_refs.return_value = {"main": "111", "feat1": "222"}
        mock_resolve.return_value = "main"

        mock_plat = MagicMock()
        mock_get_platform.return_value = mock_plat

        mock_executor = MagicMock()
        mock_tpe.return_value.__enter__.return_value = mock_executor

        mock_future = MagicMock()
        mock_executor.submit.return_value = mock_future
        mock_as_completed.return_value = [mock_future]

        sync.sync_stack(push=False, pr=True)

        self.assertTrue(mock_tpe.called)
        self.assertEqual(mock_executor.submit.call_count, 1)
        # The args passed to submit are (method, arg1, arg2)
        # method is self._sync_single_pr bound method
        # arg1 is branch, arg2 is parent_name
        # So call_args[0] is (method, "feat1", "main")
        call_args = mock_executor.submit.call_args[0]
        self.assertEqual(call_args[1], "feat1")
        self.assertEqual(call_args[2], "main")
