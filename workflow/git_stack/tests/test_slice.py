import os
import sys
import unittest
from typing import Dict, List, Tuple
from unittest.mock import MagicMock, mock_open, patch

from git_stack.src.machete import MacheteNode
from git_stack.src.slice import apply_slice, get_stack_commits


class TestSliceAlgorithms(unittest.TestCase):

    def setUp(self):
        self.mock_run_git = patch(
            'git_stack.src.slice.run_git').start()
        self.mock_read = patch('builtins.open', mock_open()).start()
        self.mock_write_machete = patch(
            'git_stack.src.slice.write_machete').start()
        self.mock_parse_machete = patch(
            'git_stack.src.slice.parse_machete').start()

    def tearDown(self):
        patch.stopall()

    def test_get_stack_commits(self):
        """Test parsing of git log output."""
        # hash subject
        self.mock_run_git.return_value = "hash1 subject one\nhash2 subject two\nhash3 "

        commits = get_stack_commits("main")

        self.assertEqual(len(commits), 3)
        self.assertEqual(commits[0], ("hash1", "subject one"))
        self.assertEqual(commits[1], ("hash2", "subject two"))
        self.assertEqual(commits[2], ("hash3", ""))

    def test_apply_slice_one_commit_per_branch(self):
        """
        Scenario:
        Base: main
        Commits: C1, C2
        Map: C1->B1, C2->B2
        Expected: main -> B1 -> B2
        """
        # Mock machete state: empty or just main
        root = MacheteNode("main")
        self.mock_parse_machete.return_value = {"main": root}

        # Mock commits definition (needed by apply_slice to order the map)
        with patch('git_stack.src.slice.get_stack_commits') as mock_get_commits:
            mock_get_commits.return_value = [("C1", "Msg1"), ("C2", "Msg2")]

            mapping = {"C1": "branch-1", "C2": "branch-2"}
            apply_slice("main", mapping)

            # Check git branch moves
            self.mock_run_git.assert_any_call(
                ['branch', '-f', 'branch-1', 'C1'])
            self.mock_run_git.assert_any_call(
                ['branch', '-f', 'branch-2', 'C2'])

            # Check machete structure updating
            # We expect write_machete to be called with a dict containing the updated nodes
            self.assertTrue(self.mock_write_machete.called)
            args, _ = self.mock_write_machete.call_args
            nodes = args[0]

            self.assertIn("main", nodes)
            self.assertIn("branch-1", nodes)
            self.assertIn("branch-2", nodes)

            # Verify Topology
            node_main = nodes["main"]
            self.assertEqual(len(node_main.children), 1)
            self.assertEqual(node_main.children[0].name, "branch-1")

            node_b1 = node_main.children[0]
            self.assertEqual(node_b1.parent, node_main)
            self.assertEqual(len(node_b1.children), 1)
            self.assertEqual(node_b1.children[0].name, "branch-2")

            node_b2 = node_b1.children[0]
            self.assertEqual(node_b2.parent, node_b1)

    def test_apply_slice_skip_commits(self):
        """
        Scenario:
        Base: main
        Commits: C1, C2, C3
        Map: C1->B1, C3->B2
        Result should be: main -> B1 -> B2
        """
        root = MacheteNode("main")
        self.mock_parse_machete.return_value = {"main": root}

        with patch('git_stack.src.slice.get_stack_commits') as mock_get_commits:
            mock_get_commits.return_value = [
                ("C1", "Msg1"), ("C2", "Msg2"), ("C3", "Msg3")]

            mapping = {"C1": "branch-1", "C3": "branch-2"}
            apply_slice("main", mapping)

            args, _ = self.mock_write_machete.call_args
            nodes = args[0]

            node_main = nodes["main"]
            # Should have B1
            self.assertEqual(node_main.children[0].name, "branch-1")
            # B1 should have B2
            self.assertEqual(
                node_main.children[0].children[0].name, "branch-2")

    def test_apply_slice_multiple_commits_same_branch(self):
        """
        Scenario:
        Base: main
        Commits: C1, C2
        Map: C1->B1, C2->B1
        We expect B1 to be applied once.
        """
        root = MacheteNode("main")
        self.mock_parse_machete.return_value = {"main": root}

        with patch('git_stack.src.slice.get_stack_commits') as mock_get_commits:
            mock_get_commits.return_value = [("C1", "Msg1"), ("C2", "Msg2")]

            mapping = {"C1": "branch-1", "C2": "branch-1"}
            apply_slice("main", mapping)

            # Verify only ONE child of main
            args, _ = self.mock_write_machete.call_args
            nodes = args[0]
            main = nodes['main']
            self.assertEqual(len(main.children), 1)
            self.assertEqual(main.children[0].name, 'branch-1')
            self.assertEqual(len(main.children[0].children), 0)

    def test_apply_slice_reparenting_insert(self):
        """
        Scenario: Re-slice.
        Initial State: main -> feather (B)
        User Action: Insert 'armor' (A) between main and feather.
        New Stack: main -> armor -> feather
        Commits: hashA -> armor, hashB -> feather
        """
        # Initial State: main -> feather (B)
        root = MacheteNode('main')
        feat = MacheteNode('feather')
        feat.parent = root
        root.children.append(feat)

        nodes_map = {'main': root, 'feather': feat}
        self.mock_parse_machete.return_value = nodes_map

        with patch('git_stack.src.slice.get_stack_commits') as mock_commits:
            mock_commits.return_value = [
                ('hashA', 'Armor'), ('hashB', 'Feather')]

            mapping = {
                'hashA': 'armor',
                'hashB': 'feather'
            }

            # Run
            apply_slice('main', mapping)

            # Check Write
            written_nodes = self.mock_write_machete.call_args[0][0]

            # Navigate from main
            main_node = written_nodes['main']

            # Expected: Main has 1 child: armor
            self.assertEqual(len(main_node.children), 1)
            self.assertEqual(main_node.children[0].name, 'armor')

            armor_node = main_node.children[0]
            # Expected: Armor has 1 child: feather
            # This verifies 'feather' was reparented from 'main' to 'armor'
            self.assertEqual(len(armor_node.children), 1)
            self.assertEqual(armor_node.children[0].name, 'feather')

            # Verify clean detach (feather not in main's children anymore)
            self.assertNotIn('feather', [c.name for c in main_node.children])

    def test_apply_slice_shrinking(self):
        """
        Scenario: Re-slice shrinking.
        Initial State: main -> A -> B -> C
        Action: Slice defines only main -> A -> B.
        Expected: A and B are reinforced. C remains attached to B (orphaned from stack definition, but present in tree).
        """
        root = MacheteNode('main')
        A = MacheteNode('A')
        B = MacheteNode('B')
        C = MacheteNode('C')

        # main -> A -> B -> C
        A.parent = root
        root.children.append(A)
        B.parent = A
        A.children.append(B)
        C.parent = B
        B.children.append(C)

        nodes_map = {'main': root, 'A': A, 'B': B, 'C': C}
        self.mock_parse_machete.return_value = nodes_map

        with patch('git_stack.src.slice.get_stack_commits') as mock_commits:
            # We assume user edits commits and removes C's commit from map?
            # Or C's commit is there but not mapped?
            mock_commits.return_value = [('hashA', 'MsgA'), ('hashB', 'MsgB')]
            mapping = {'hashA': 'A', 'hashB': 'B'}

            apply_slice('main', mapping)

            written_nodes = self.mock_write_machete.call_args[0][0]

            # Navigate
            main = written_nodes['main']
            self.assertEqual(main.children[0].name, 'A')
            a_node = main.children[0]
            self.assertEqual(a_node.children[0].name, 'B')
            b_node = a_node.children[0]

            # C should still be child of B? Yes, because we reused object B.
            self.assertEqual(len(b_node.children), 1)
            self.assertEqual(b_node.children[0].name, 'C')

    def test_apply_slice_fork_modification(self):
        """
        Scenario: Re-slice modifies fork pointer.
        Initial State: main -> A -> {B, C} (A is fork point)
        Action: Modify stack path to C. change A -> NewA.
        User intent: main -> NewA -> C.
        Expected: A remains child of main. A keeps B. NewA takes C.
        """
        root = MacheteNode('main')
        A = MacheteNode('A')
        B = MacheteNode('B')
        C = MacheteNode('C')  # Our target

        # main -> A -> B
        #            -> C
        A.parent = root
        root.children.append(A)
        B.parent = A
        A.children.append(B)
        C.parent = A
        A.children.append(C)

        nodes_map = {'main': root, 'A': A, 'B': B, 'C': C}
        self.mock_parse_machete.return_value = nodes_map

        with patch('git_stack.src.slice.get_stack_commits') as mock_commits:
            # Stack for C involves new commits?
            mock_commits.return_value = [
                ('hashXA', 'NewMsgA'), ('hashC', 'MsgC')]
            mapping = {'hashXA': 'NewA', 'hashC': 'C'}

            apply_slice('main', mapping)

            written_nodes = self.mock_write_machete.call_args[0][0]
            main = written_nodes['main']

            # Main should have A (preserved) and NewA (created)
            children_names = [c.name for c in main.children]
            self.assertIn('A', children_names)
            self.assertIn('NewA', children_names)

            # A should still have B.
            a_node = next(c for c in main.children if c.name == 'A')
            self.assertEqual(len(a_node.children), 1)  # Lost C
            self.assertEqual(a_node.children[0].name, 'B')

            # NewA should have C
            new_a_node = next(c for c in main.children if c.name == 'NewA')
            self.assertEqual(len(new_a_node.children), 1)
            self.assertEqual(new_a_node.children[0].name, 'C')

    def test_run_twice_idempotent(self):
        root = MacheteNode("main")
        self.mock_parse_machete.return_value = {"main": root}

        with patch('git_stack.src.slice.get_stack_commits') as mock_get_commits:
            mock_get_commits.return_value = [("C1", "Msg1")]
            mapping = {"C1": "branch-1"}

            # Run 1
            apply_slice("main", mapping)
            nodes_run1 = self.mock_write_machete.call_args[0][0]

            # Mock parse returning the result of run 1
            self.mock_parse_machete.return_value = nodes_run1

            # Run 2
            apply_slice("main", mapping)
            nodes_run2 = self.mock_write_machete.call_args[0][0]

            # Should be identical structure
            self.assertEqual(len(nodes_run2['main'].children), 1)
            self.assertEqual(nodes_run2['main'].children[0].name, 'branch-1')


class TestSliceCleanup(unittest.TestCase):

    def setUp(self):
        self.mock_run_git = patch('git_stack.src.slice.run_git').start()
        self.mock_write_machete = patch(
            'git_stack.src.slice.write_machete').start()
        self.mock_parse_machete = patch(
            'git_stack.src.slice.parse_machete').start()
        self.mock_get_commits = patch(
            'git_stack.src.slice.get_stack_commits').start()
        self.mock_refs = patch('git_stack.src.slice.get_refs_map').start()

    def tearDown(self):
        patch.stopall()

    def test_cleanup_orphaned_branches(self):
        """
        Scenario:
        Base: main
        Stack Commits: C1, C2
        Old State: 
           main -> old-branch-1 (C1)
                -> old-branch-2 (C2)
        User Action: slice mapping defines C1 -> new-branch-1. 
                     C2 is mapped to NOTHING (dropped).
                     old-branch-1 is renamed/remapped.
                     old-branch-2 is abandoned.
        """

        # Machete knows about old branches
        root = MacheteNode("main")
        old1 = MacheteNode("old-branch-1")
        old2 = MacheteNode("old-branch-2")
        self.mock_parse_machete.return_value = {
            "main": root,
            "old-branch-1": old1,
            "old-branch-2": old2
        }

        # Commits in scope
        self.mock_get_commits.return_value = [("C1", "Msg1"), ("C2", "Msg2")]

        # Current Refs (must exist to be deleted)
        self.mock_refs.return_value = {
            "main": "C0",
            "old-branch-1": "C1",
            "old-branch-2": "C2",
            "random-branch": "C1"  # Should not be deleted as not in Machete
        }

        # User defined ONLY new-branch-1
        mapping = {"C1": "new-branch-1"}

        apply_slice("main", mapping)

        # Expectation:
        # new-branch-1 is created (C1) - handled by existing code
        self.mock_run_git.assert_any_call(
            ['branch', '-f', 'new-branch-1', 'C1'])

        # Check specific delete calls
        # Helper to extract arguments from run_git(['branch', '-D', 'name'])
        delete_calls = []
        for call in self.mock_run_git.call_args_list:
            args = call[0]  # tuple of args
            if args and isinstance(args[0], list):
                cmd_list = args[0]
                if len(cmd_list) >= 3 and cmd_list[0] == 'branch' and cmd_list[1] == '-D':
                    delete_calls.append(cmd_list[2])

        self.assertIn('old-branch-1', delete_calls)
        self.assertIn('old-branch-2', delete_calls)
        self.assertNotIn('random-branch', delete_calls)
        self.assertNotIn('main', delete_calls)
        self.assertNotIn('new-branch-1', delete_calls)

    def test_no_cleanup_if_kept(self):
        """
        Scenario: Renaming just keeps the commit, but old name is gone.
        If mapped: C1 -> existing-branch
        Then existing-branch is preserved.
        """
        self.mock_parse_machete.return_value = {
            "main": MacheteNode("main"),
            "existing-branch": MacheteNode("existing-branch")
        }
        self.mock_get_commits.return_value = [("C1", "Msg1")]
        self.mock_refs.return_value = {
            "main": "C0",
            "existing-branch": "C1"
        }

        mapping = {"C1": "existing-branch"}

        apply_slice("main", mapping)

        # Should NOT delete existing-branch
        delete_calls = []
        for call in self.mock_run_git.call_args_list:
            args = call[0]
            if args and isinstance(args[0], list):
                cmd_list = args[0]
                if len(cmd_list) >= 3 and cmd_list[0] == 'branch' and cmd_list[1] == '-D':
                    delete_calls.append(cmd_list[2])

        self.assertEqual(len(delete_calls), 0)
