import unittest
from unittest.mock import MagicMock, patch

from git_stack.src import anno
from git_stack.src.machete import (MacheteNode, StackItem,
                                   generate_nested_list,
                                   strip_existing_stack_block)


class TestAnnoFormatting(unittest.TestCase):
    def test_description_append_logic(self):
        """Verify stack block is appended to end and replaces old block correctly."""
        # Scenario 1: Clean Description
        original = "Fix a bug.\n\nExplanation here."
        stripped = strip_existing_stack_block(original)
        self.assertEqual(stripped, original)

        # Scenario 2: Description with old stack block
        old_block = """
Fix a bug.

<!-- start git-stack-sync generated -->
Old junk
<!-- end git-stack-sync generated -->
"""
        stripped = strip_existing_stack_block(old_block)
        expected = "Fix a bug."
        self.assertEqual(stripped, expected)

        # Scenario 3: Description with stack block NOT at end
        mixed_block = """
Intro.

<!-- start git-stack-sync generated -->
Old Stack
<!-- end git-stack-sync generated -->

Outro.
"""
        stripped = strip_existing_stack_block(mixed_block)
        self.assertIn("Intro.", stripped)
        self.assertIn("Outro.", stripped)
        self.assertNotIn("Old Stack", stripped)

    def test_generate_nested_list(self):
        # Setup a mock stack
        node_root = MacheteNode("root-branch", 0, "")

        node_a = MacheteNode("feature-a", 2, "PR #100")
        node_a.parent = node_root

        node_b = MacheteNode("feature-b", 4, "PR #101")
        node_b.parent = node_a

        node_c = MacheteNode("feature-c", 6, "PR #102")
        node_c.parent = node_b

        stack = [
            {'node': node_a, 'pr_num': '100'},
            {'node': node_b, 'pr_num': '101'},
            {'node': node_c, 'pr_num': '102'}
        ]

        target_branch = "feature-b"
        output = generate_nested_list(stack, target_branch)

        # Validation
        self.assertIn("PR #101", output)
        self.assertIn("**[2/3] PR #101** 👈 **(THIS PR)**", output)
        self.assertIn("**[1/3] PR #100**", output)

    def test_mr_label_support(self):
        """Test GitLab MR labeling style."""
        node_a = MacheteNode("feature-a", 2, "PR #100")
        stack = [{'node': node_a, 'pr_num': '100'}]

        output = generate_nested_list(stack, "feature-a", item_label="MR")
        self.assertIn("**[1/1] MR #100** 👈 **(THIS MR)**", output)


class TestAnnotateCommand(unittest.TestCase):
    @patch('git_stack.src.anno.get_platform')
    @patch('git_stack.src.anno.parse_machete')
    @patch('git_stack.src.anno.write_machete')
    def test_annotate_stack_flow(self, mock_write, mock_parse, mock_get_platform):
        # Setup
        mock_plat = MagicMock()
        mock_plat.get_item_label.return_value = "PR"
        mock_get_platform.return_value = mock_plat

        # Machete Graph: Main (0) -> Feat (1)
        root = MacheteNode("main", 0)
        feat = MacheteNode("feat", 2)
        feat.parent = root
        root.children.append(feat)

        mock_parse.return_value = {'main': root, 'feat': feat}

        # Mock PR responses
        # feat returns a PR, main returns None
        mock_plat.get_mr.side_effect = lambda name: {
            'number': 123} if name == 'feat' else None
        mock_plat.get_mr_description.return_value = "Old Desc"

        anno.annotate_stack()

        # Verify
        # 1. get_mr called for feat
        mock_plat.get_mr.assert_called_with('feat')

        # 2. write_machete called (feat annotation updated)
        mock_write.assert_called()
        self.assertEqual(feat.annotation, "PR #123")

        # 3. update_mr_description called
        mock_plat.update_mr_description.assert_called()
        args = mock_plat.update_mr_description.call_args[0]
        self.assertEqual(args[0], '123')
        self.assertIn("🥞 Stack", args[1])
        self.assertIn("Old Desc", args[1])
