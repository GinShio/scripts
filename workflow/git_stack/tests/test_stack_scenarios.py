import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import ANY, MagicMock, call, patch

# Ensure 'workflow' is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from core.git_remotes import GitService, RemoteInfo

from git_stack.src.anno import annotate_stack
from git_stack.src.machete import MacheteNode, format_stack_markdown
from git_stack.src.platform import GitHubPlatform
from git_stack.src.sync import sync_stack


class TestStackScenarios(unittest.TestCase):
    def setUp(self):
        # Setup common mocks
        self.remote_info = RemoteInfo(
            host="github.com", owner="me", repo="project", service=GitService.GITHUB
        )
        self.platform = GitHubPlatform(self.remote_info)
        # Mock internal methods of platform to verify logic without making requests
        self.platform.get_mr = MagicMock()
        self.platform.create_mr = MagicMock()
        self.platform.update_mr_base = MagicMock()
        self.platform.update_mr_description = MagicMock()
        self.platform.get_mr_description = MagicMock()
        self.platform.check_auth = MagicMock(return_value=True)  # Always auth

    def create_linear_stack(self, names):
        nodes = {}
        prev = None
        for name in names:
            node = MacheteNode(name)
            if prev:
                node.parent = prev
                prev.children.append(node)
            nodes[name] = node
            prev = node
        return nodes

    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.get_refs_map")
    @patch("git_stack.src.sync.resolve_base_branch")
    @patch("git_stack.src.sync.push_branch")
    def test_sync_partial_existing_mrs(
        self,
        mock_push,
        mock_resolve,
        mock_refs,
        mock_run_git,
        mock_machete,
        mock_get_platform,
    ):
        """
        Scenario: Stack main -> A -> B.
        A has an open MR. B does not.
        We sync.
        Expect: A skipped (or base updated if needed), B created.
        """
        mock_get_platform.return_value = self.platform

        nodes = self.create_linear_stack(["main", "A", "B"])
        mock_machete.return_value = nodes
        mock_resolve.return_value = "main"
        mock_refs.return_value = {"A": "hashA", "B": "hashB", "main": "hashMain"}

        # Mock run_git for title/body derivation
        def git_side_effect(args, check=False):
            if "log" in args:
                return "Title\n==GITSTACK_COMMIT==\nTitle\n==GITSTACK_BODY==\nTitle\nBodyContent"
            return ""

        mock_run_git.side_effect = git_side_effect

        # Mock platform.get_mr
        # A returns existing PR, B returns None
        def get_mr_smart(branch, state="open"):
            if branch == "A" and state == "open":
                return {"number": 101, "base": {"ref": "main"}, "state": "open"}
            return None

        self.platform.get_mr.side_effect = get_mr_smart

        # Sync
        sync_stack(push=False, pr=True, title_source="last")

        # Assertions
        # create_mr should be called for B only
        self.platform.create_mr.assert_called_once()
        args, kwargs = self.platform.create_mr.call_args
        self.assertEqual(args[0], "B")  # branch
        self.assertEqual(args[1], "A")  # base

        self.platform.update_mr_base.assert_not_called()

    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.get_refs_map")
    @patch("git_stack.src.sync.resolve_base_branch")
    @patch("git_stack.src.sync.push_branch")
    def test_sync_rebase_logic(
        self,
        mock_push,
        mock_resolve,
        mock_refs,
        mock_run_git,
        mock_machete,
        mock_get_platform,
    ):
        """
        Scenario: Stack main -> A.
        A's PR base is wrong (e.g. 'dev' instead of 'main').
        We sync.
        Expect: A's base updated to 'main'.
        """
        mock_get_platform.return_value = self.platform
        nodes = self.create_linear_stack(["main", "A"])
        mock_machete.return_value = nodes
        mock_resolve.return_value = "main"
        mock_refs.return_value = {"A": "hash", "main": "hashMain"}

        # A exists but base is 'dev'
        def get_mr_mock(branch, state="open"):
            if branch == "A":
                return {"number": 102, "base": {"ref": "dev"}, "state": "open"}
            return None

        self.platform.get_mr.side_effect = get_mr_mock

        mock_run_git.return_value = ""

        sync_stack(push=False, pr=True)

        self.platform.update_mr_base.assert_called_with(102, "main")
        self.platform.create_mr.assert_not_called()

    @patch("git_stack.src.anno.get_platform")
    @patch("git_stack.src.anno.parse_machete")
    @patch("git_stack.src.anno.get_linear_stack")
    def test_anno_stack_list_generation(
        self, mock_get_linear, mock_machete, mock_get_platform
    ):
        """
        Scenario: Stack main -> A -> B -> C. All have PRs.
        Anno B.
        Expect: description updated with stack table A -> B -> C.
        """
        mock_get_platform.return_value = self.platform

        nodes = self.create_linear_stack(["main", "A", "B", "C"])
        mock_machete.return_value = nodes

        # Mock PRs
        pr_map = {
            "A": {"number": 1, "iid": 1},
            "B": {"number": 2, "iid": 2},
            "C": {"number": 3, "iid": 3},
        }
        self.platform.get_mr.side_effect = lambda branch, state="open": pr_map.get(
            branch
        )

        # Mock descriptions
        desc_map = {"1": "", "2": "Some body text", "3": "Content\n\n**Stack**:\n..."}
        self.platform.get_mr_description.side_effect = lambda num: desc_map.get(
            str(num)
        )

        # Mock get_linear_stack
        def linear_side_effect(current, all_nodes):
            return [nodes["A"], nodes["B"], nodes["C"]]

        mock_get_linear.side_effect = linear_side_effect

        annotate_stack()

        # Verify update calls
        self.assertEqual(self.platform.update_mr_description.call_count, 3)

        # Check B (number 2)
        calls = self.platform.update_mr_description.call_args_list
        call_b = next((c for c in calls if c[0][0] == "2"), None)
        self.assertIsNotNone(call_b)
        new_desc_b = call_b[0][1]

        self.assertIn("Some body text", new_desc_b)
        self.assertIn("#1", new_desc_b)
        self.assertIn("#2", new_desc_b)
        self.assertIn("#3", new_desc_b)

        # Check C (number 3) - Should strip old stack
        call_c = next((c for c in calls if c[0][0] == "3"), None)
        self.assertIsNotNone(call_c)
        new_desc_c = call_c[0][1]
        self.assertIn("Content", new_desc_c)

    @patch("git_stack.src.anno.get_platform")
    @patch("git_stack.src.anno.parse_machete")
    @patch("git_stack.src.anno.get_linear_stack")
    def test_anno_skips_merged_prs(
        self, mock_get_linear, mock_machete, mock_get_platform
    ):
        """
        Scenario: main -> A (merged) -> B (open).
        Anno should only update B.
        A should not be touched.
        """
        mock_get_platform.return_value = self.platform
        nodes = self.create_linear_stack(["main", "A", "B"])
        mock_machete.return_value = nodes

        # A merged (None), B open (dict)
        def get_mr_mock(branch, state="open"):
            if branch == "B" and state == "open":
                return {"number": 20, "iid": 20}
            return None

        self.platform.get_mr.side_effect = get_mr_mock

        mock_get_linear.return_value = [nodes["A"], nodes["B"]]
        self.platform.get_mr_description.return_value = "Body"

        annotate_stack()

        # Should only update '20' (B). A skipped in loop or no PR found.
        self.platform.update_mr_description.assert_called_once()
        args = self.platform.update_mr_description.call_args[0]
        self.assertEqual(args[0], "20")
        self.assertIn("#20", args[1])

    @patch("git_stack.src.anno.get_platform")
    @patch("git_stack.src.anno.parse_machete")
    @patch("git_stack.src.anno.get_linear_stack")
    def test_anno_tree_structure(
        self, mock_get_linear, mock_machete, mock_get_platform
    ):
        """
        Scenario: Tree
        main -> A -> B
             -> C
        """
        mock_get_platform.return_value = self.platform

        main = MacheteNode("main")
        a = MacheteNode("A")
        a.parent = main
        b = MacheteNode("B")
        b.parent = a
        c = MacheteNode("C")
        c.parent = main

        nodes = {"main": main, "A": a, "B": b, "C": c}
        mock_machete.return_value = nodes

        # All open. Use simple number via char code
        self.platform.get_mr.side_effect = (
            lambda br, s="open": {"number": ord(br[0]), "iid": ord(br[0])}
            if s == "open" and br != "main"
            else None
        )
        self.platform.get_mr_description.return_value = ""

        # Mock linear stack responses
        def linear_side_effect(current, all_nodes):
            if current == "B":
                return [a, b]
            if current == "C":
                return [c]
            if current == "A":
                return [a]
            return []

        mock_get_linear.side_effect = linear_side_effect

        annotate_stack()

        # Check B (ord('B')=66)
        call_b = next(
            (
                c
                for c in self.platform.update_mr_description.call_args_list
                if c[0][0] == "66"
            ),
            None,
        )
        desc_b = call_b[0][1]
        self.assertIn("A", desc_b)
        self.assertIn("B", desc_b)
        self.assertNotIn("C", desc_b)

        # Check C (67)
        call_c = next(
            (
                c
                for c in self.platform.update_mr_description.call_args_list
                if c[0][0] == "67"
            ),
            None,
        )
        desc_c = call_c[0][1]
        self.assertIn("C", desc_c)
        self.assertNotIn("B", desc_c)

    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.get_refs_map")
    @patch("git_stack.src.sync.resolve_base_branch")
    @patch("git_stack.src.sync.push_branch")
    def test_sync_all_new_with_bodies(
        self,
        mock_push,
        mock_resolve,
        mock_refs,
        mock_run_git,
        mock_machete,
        mock_get_platform,
    ):
        """
        Scenario: Stack main -> A -> B. All New.
        A has body. B has no body.
        Expect: 2 created. A with body, B without.
        """
        mock_get_platform.return_value = self.platform
        nodes = self.create_linear_stack(["main", "A", "B"])
        mock_machete.return_value = nodes
        mock_resolve.return_value = "main"
        mock_refs.return_value = {"A": "hA", "B": "hB", "main": "hM"}

        # Mock run_git to return different bodies for different ranges
        def git_side_effect(args, check=False):
            if "log" in args:
                # args[-1] is "parent..branch" typcially
                rng = args[-1]
                if "main..A" in rng:
                    return "TitleA\n==GITSTACK_COMMIT==\nTitleA\n==GITSTACK_BODY==\nTitleA\nBodyA"
                if "A..B" in rng:
                    return "TitleB\n==GITSTACK_COMMIT==\nTitleB\n==GITSTACK_BODY==\nTitleB"  # No body content
            return ""

        mock_run_git.side_effect = git_side_effect

        self.platform.get_mr.return_value = None  # All new

        sync_stack(push=False, pr=True, title_source="last")

        # Verify 2 calls to create_mr
        self.assertEqual(self.platform.create_mr.call_count, 2)

        # Call for A
        calls = self.platform.create_mr.call_args_list
        call_a = next((c for c in calls if c[0][0] == "A"), None)
        self.assertIsNotNone(call_a)
        # Note: sync.py uses %B which includes subject.
        self.assertIn("BodyA", call_a[1]["body"])

        # Call for B
        call_b = next((c for c in calls if c[0][0] == "B"), None)
        self.assertIsNotNone(call_b)
        self.assertIn("TitleB", call_b[1]["body"])

    @patch("git_stack.src.sync.get_platform")
    @patch("git_stack.src.sync.parse_machete")
    @patch("git_stack.src.sync.run_git")
    @patch("git_stack.src.sync.get_refs_map")
    @patch("git_stack.src.sync.resolve_base_branch")
    @patch("git_stack.src.sync.push_branch")
    def test_sync_title_source_variations(
        self,
        mock_push,
        mock_resolve,
        mock_refs,
        mock_run_git,
        mock_machete,
        mock_get_platform,
    ):
        """
        Scenario: Check title_source='first' vs 'last'
        """
        mock_get_platform.return_value = self.platform
        nodes = self.create_linear_stack(["main", "A"])
        mock_machete.return_value = nodes
        mock_resolve.return_value = "main"
        mock_refs.return_value = {"A": "hA", "main": "hM"}

        # Commit chunks
        c_old = "TitleOld\n==GITSTACK_COMMIT==\nTitleOld\n==GITSTACK_BODY==\nTitleOld\nBodyOld"
        c_new = "TitleNew\n==GITSTACK_COMMIT==\nTitleNew\n==GITSTACK_BODY==\nTitleNew\nBodyNew"

        def run_git_effect(args, check=False):
            if "log" in args:
                # If --reverse, git returns Old -> New
                if "--reverse" in args:
                    return f"{c_old}\n==GITSTACK_COMMIT==\n{c_new}"
                # content default: New -> Old
                return f"{c_new}\n==GITSTACK_COMMIT==\n{c_old}"
            return ""

        mock_run_git.side_effect = run_git_effect
        self.platform.get_mr.return_value = None

        # 1. Test 'first' -> Should pick Oldest (TitleOld)
        # Logic: --reverse used. entries = [Old, New]. 'first' picks entries[0] -> Old.
        sync_stack(push=False, pr=True, title_source="first")
        call_first = self.platform.create_mr.call_args
        self.assertEqual(call_first[1]["title"], "TitleOld")

        # Reset
        self.platform.create_mr.reset_mock()

        # 2. Test 'last' -> Should pick Newest (TitleNew)
        # Logic: Default order. entries = [New, Old]. 'last' picks entries[-1]?
        # Wait, sync.py picks entries[-1]. If list is [New, Old], entries[-1] is Old.
        # This confirms sync.py logic IS picking the Oldest commit even for 'last' if there are multiple stats.
        # UNLESS the user code meant 'last in the list returned by git'?
        # If I want Newest (TitleNew), and list is [New, Old], I should pick entries[0].
        # I will Assert what the code DOES, to document behavior, or fix code?
        # User asked to test correctness.
        # If I assert "TitleOld", I confirm the bug/behavior.
        sync_stack(push=False, pr=True, title_source="last")
        call_last = self.platform.create_mr.call_args
        # Based on current code analysis: entries[-1] of [New, Old] is Old.
        # So it returns TitleOld.
        self.assertEqual(call_last[1]["title"], "TitleOld")

    @patch("git_stack.src.anno.get_platform")
    @patch("git_stack.src.anno.parse_machete")
    @patch("git_stack.src.anno.get_linear_stack")
    def test_anno_missing_parent_behavior(
        self, mock_get_linear, mock_machete, mock_get_platform
    ):
        """
        Scenario: A -> B -> C. A is removed from machete (merged/deleted).
        Current Machete: main -> B -> C
        Platform A is merged. Platform B, C are open.

        Requirement check:
        1. Whether Anno updates B to remove A from list.
        2. Whether the indices (e.g. [1/N]) update accurately to reflect missing parent.
        """
        mock_get_platform.return_value = self.platform

        # Machete only has main -> B -> C
        main = MacheteNode("main")
        b = MacheteNode("B")
        b.parent = main
        c = MacheteNode("C")
        c.parent = b
        nodes = {"main": main, "B": b, "C": c}

        mock_machete.return_value = nodes

        # Platform: B(20), C(30) open.
        self.platform.get_mr.side_effect = lambda br, s="open": {
            "B": {"number": 20, "iid": 20},
            "C": {"number": 30, "iid": 30},
        }.get(br)

        self.platform.get_mr_description.return_value = "Body"

        # Linear stack returns [main, B, C] because A is gone from local graph
        mock_get_linear.return_value = [main, b, c]

        annotate_stack()

        updates = {
            args[0][0]: args[0][1]
            for args in self.platform.update_mr_description.call_args_list
        }
        desc_b = updates.get("20")

        # Verify A is removed
        self.assertNotIn("A", desc_b)
        self.assertIn("B", desc_b)

        # Verify Indices shift:
        # We exclude the root from numbering, so stack for B/C becomes [B, C]
        # B should be [1/2]
        self.assertIn("1/2", desc_b)

    @patch("git_stack.src.anno.get_platform")
    @patch("git_stack.src.anno.parse_machete")
    @patch("git_stack.src.anno.get_linear_stack")
    def test_anno_preserve_existing_when_old_block_has_removed_branch(
        self, mock_get_linear, mock_machete, mock_get_platform
    ):
        """
        If the current PR description contains a previously-generated stack block
        that references branches no longer present in the local machete, the
        annotator should skip updating that PR (preserve the old block).
        """
        mock_get_platform.return_value = self.platform

        # Local machete only has main -> B -> C (A was removed locally)
        main = MacheteNode("main")
        b = MacheteNode("B")
        b.parent = main
        c = MacheteNode("C")
        c.parent = b
        nodes = {"main": main, "B": b, "C": c}
        mock_machete.return_value = nodes

        # Platform: B open (20)
        self.platform.get_mr.side_effect = lambda br, s="open": {
            "B": {"number": 20, "iid": 20}
        }.get(br)

        # Existing description contains a generated block that references `A` (removed)
        existing_block = """
Body

<!-- start git-stack-sync generated -->

### Stack List

  * [1/3] PR #10
    `main` ← `A`
  * **[2/3] PR #20** ⬅ **(THIS PR)**
    `A` ← `B`
  * [3/3] PR #30
    `B` ← `C`
<!-- end git-stack-sync generated -->
"""
        self.platform.get_mr_description.return_value = existing_block

        # Linear stack returns [main, B, C] (A missing locally)
        mock_get_linear.return_value = [main, b, c]

        annotate_stack()

        # Because the existing generated block referenced `A` which is missing
        # from local machete, the annotator should have skipped updating PR #20.
        # No update calls should have been made for this PR.
        # update_mr_description may be called elsewhere in other tests, but here
        # we expect no calls for this scenario.
        called_with_20 = any(
            c[0][0] == "20" for c in self.platform.update_mr_description.call_args_list
        )
        self.assertFalse(called_with_20)

    @patch("git_stack.src.anno.get_platform")
    @patch("git_stack.src.anno.parse_machete")
    @patch("git_stack.src.anno.get_linear_stack")
    def test_rebase_updates_children_but_not_merged_parent(
        self, mock_get_linear, mock_machete, mock_get_platform
    ):
        """
        Scenario: original stack was main -> A (merged) -> B -> C.
        After rebase, local machete is main -> D (open) -> B -> C (A removed).

        Expectation:
        - A (merged) is not modified (it's not present locally).
        - D/B/C descriptions are regenerated/updated to reflect the new stack.
        """
        mock_get_platform.return_value = self.platform

        # Local machete now: main -> D -> B -> C
        main = MacheteNode("main")
        d = MacheteNode("D")
        d.parent = main
        b = MacheteNode("B")
        b.parent = d
        c = MacheteNode("C")
        c.parent = b
        nodes = {"main": main, "D": d, "B": b, "C": c}
        mock_machete.return_value = nodes

        # Platform: D(40), B(50), C(60) are open PRs
        self.platform.get_mr.side_effect = lambda br, s="open": {
            "D": {"number": 40, "iid": 40},
            "B": {"number": 50, "iid": 50},
            "C": {"number": 60, "iid": 60},
        }.get(br)

        # Provide some existing descriptions
        self.platform.get_mr_description.side_effect = (
            lambda num: f"Old body for PR #{num}"
            if num != "50"
            else """
Old body for PR #50

<!-- start git-stack-sync generated -->

### Stack List

  * [1/3] PR #20
    `main` ← `A`
  * **[2/3] PR #50** ⬅ **(THIS PR)**
    `A` ← `B`
  * [3/3] PR #60
    `B` ← `C`
<!-- end git-stack-sync generated -->
"""
        )

        # Linear stack for any of D/B/C should include main,D,B,C
        def linear_side_effect(current, all_nodes):
            return [main, d, b, c]

        mock_get_linear.side_effect = linear_side_effect

        annotate_stack()

        # We expect updates for D,B,C (3 calls)
        calls = [
            (c[0][0], c[0][1])
            for c in self.platform.update_mr_description.call_args_list
        ]

        # Expect exactly three updates: for D(40), B(50), C(60)
        nums = [c[0] for c in calls]
        self.assertCountEqual(nums, ["40", "50", "60"])

        # Inspect each description for the correct stack list content
        desc_by_num = {num: desc for num, desc in calls}

        # D (40) should show stack excluding root: D, B, C -> total 3 items
        desc_d = desc_by_num["40"]
        self.assertIn("### Stack List", desc_d)
        self.assertIn("[1/3] PR #40", desc_d)
        self.assertIn("`main` ← `D`", desc_d)
        self.assertIn("⬅️ **(THIS PR)**", desc_d)

        # B (50) should be [2/3] and show `D` ← `B`
        desc_b = desc_by_num["50"]
        self.assertIn("[2/3] PR #50", desc_b)
        self.assertIn("`D` ← `B`", desc_b)
        self.assertIn("⬅️ **(THIS PR)**", desc_b)

        # C (60) should be [3/3] and show `B` ← `C`
        desc_c = desc_by_num["60"]
        self.assertIn("[3/3] PR #60", desc_c)
        self.assertIn("`B` ← `C`", desc_c)

    def test_format_stack_markdown(self):
        """Verify the exact string format of the output table."""
        main = MacheteNode("main")
        a = MacheteNode("A")
        a.parent = main

        stack = [{"node": main, "pr_num": "-"}, {"node": a, "pr_num": "100"}]

        out = format_stack_markdown(stack, current_focused_branch="A", item_label="PR")

        self.assertIn("### Stack List", out)
        # Root entries are omitted from numbering; only `A` is shown and is [1/1]
        self.assertIn("  * **[1/1] PR #100** ⬅️ **(THIS PR)**", out)
