import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure workflow package in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from git_stack.src.machete import MacheteNode
from git_stack.src.sync import sync_stack


class TestMRTitleBodyExtraction(unittest.TestCase):
    @patch("uuid.uuid4")
    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.get_refs_map")
    @patch("git_stack.src.sync.resolve_base_branch")
    @patch("git_stack.src.sync.push_branch")
    def test_title_and_body_are_extracted_from_commits(
        self,
        mock_push,
        mock_resolve,
        mock_refs,
        mock_run_git,
        mock_machete,
        mock_get_platform,
        mock_uuid,
    ):
        """
        Ensure MR title comes from commit subject (not branch name) and
        description/body only contains the body portion (post-marker).
        """
        # Mock UUIDs for deterministic markers
        mock_uuid.side_effect = [MagicMock(hex="COMMIT"), MagicMock(hex="BODY")]

        # Setup simple stack: main -> feat
        main = MacheteNode("main")
        feat = MacheteNode("feat")
        feat.parent = main
        main.children = [feat]
        mock_machete.return_value = {"main": main, "feat": feat}
        mock_refs.return_value = {"main": "mhash", "feat": "fhash"}
        mock_resolve.return_value = "main"

        mock_plat = MagicMock()
        mock_get_platform.return_value = mock_plat
        # Ensure sync_mr delegates to create_mr so our assertions observe calls
        mock_plat.create_mr = MagicMock()

        def _sync_mr(branch, parent, **kwargs):
            return mock_plat.create_mr(branch, parent, **kwargs)

        mock_plat.sync_mr = _sync_mr

        # Simulate git log output for parent..branch with markers
        # Subject line and body after marker
        mocked_commit = (
            "GITSTACK_COMMIT_COMMIT\n"
            "TitleLine\n"
            "GITSTACK_BODY_BODY\n"
            "This is the body line 1\n"
            "This is body line 2\n"
        )

        def run_git_side(args, check=False):
            if "log" in args:
                return mocked_commit
            return ""

        mock_run_git.side_effect = run_git_side

        # No existing PRs
        mock_plat.get_mr.return_value = None

        # Run sync to trigger create_mr
        sync_stack(push=False, pr=True, title_source="last")

        # Verify create_mr was called
        self.assertTrue(mock_plat.create_mr.called)
        args, kwargs = mock_plat.create_mr.call_args
        # title should be the extracted subject, not the branch name
        self.assertEqual(kwargs.get("title"), "TitleLine")
        # body should be only the post-marker content
        self.assertIn("This is the body line 1", kwargs.get("body"))
        self.assertNotIn("GITSTACK_COMMIT_COMMIT", kwargs.get("body"))

    @patch("uuid.uuid4")
    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.get_refs_map")
    @patch("git_stack.src.sync.resolve_base_branch")
    @patch("git_stack.src.sync.push_branch")
    def test_fallback_to_head_and_body_only(
        self,
        mock_push,
        mock_resolve,
        mock_refs,
        mock_run_git,
        mock_machete,
        mock_get_platform,
        mock_uuid,
    ):
        """
        When parent..branch yields no commits (e.g. after slicing), we fallback
        to branch HEAD; description must still be only body portion.
        """
        # Mock UUIDs for deterministic markers
        mock_uuid.side_effect = [MagicMock(hex="COMMIT"), MagicMock(hex="BODY")]

        main = MacheteNode("main")
        newb = MacheteNode("newb")
        newb.parent = main
        main.children = [newb]
        mock_machete.return_value = {"main": main, "newb": newb}
        mock_refs.return_value = {"main": "mhash", "newb": "nhash"}
        mock_resolve.return_value = "main"

        mock_plat = MagicMock()
        mock_get_platform.return_value = mock_plat
        # Ensure sync_mr delegates to create_mr so our assertions observe calls
        mock_plat.create_mr = MagicMock()

        def _sync_mr(branch, parent, **kwargs):
            return mock_plat.create_mr(branch, parent, **kwargs)

        mock_plat.sync_mr = _sync_mr

        # parent..branch returns empty; show HEAD returns formatted commit
        def run_git_side(args, check=False):
            if args and args[0] == "log":
                return ""
            if args and args[0] == "show":
                return (
                    "GITSTACK_COMMIT_COMMIT\n"
                    "HeadTitle\n"
                    "GITSTACK_BODY_BODY\n"
                    "Head body only\n"
                )
            return ""

        mock_run_git.side_effect = run_git_side
        mock_plat.get_mr.return_value = None

        sync_stack(push=False, pr=True, title_source="last")

        self.assertTrue(mock_plat.create_mr.called)
        args, kwargs = mock_plat.create_mr.call_args
        self.assertEqual(kwargs.get("title"), "HeadTitle")
        self.assertEqual(kwargs.get("body").strip(), "Head body only")


if __name__ == "__main__":
    unittest.main()
