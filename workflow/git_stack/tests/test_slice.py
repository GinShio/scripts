import os
import tempfile
import unittest
from unittest.mock import patch

from git_stack.src.machete import MacheteNode
from git_stack.src.slice import (
    _collect_stack_branches,
    _parse_todo_branches,
    _update_machete,
    get_stack_commits,
)


class TestParseTodoBranches(unittest.TestCase):
    def _write_todo(self, content: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".todo")
        with os.fdopen(fd, "w") as f:
            f.write(content)
        return path

    def tearDown(self):
        pass  # temp files cleaned up per-test

    def test_basic_active_lines(self):
        path = self._write_todo(
            "pick abc123 first\n"
            "update-ref refs/heads/feat/a\n"
            "pick def456 second\n"
            "update-ref refs/heads/feat/b\n"
        )
        try:
            result = _parse_todo_branches(path)
        finally:
            os.unlink(path)
        self.assertEqual(result, ["feat/a", "feat/b"])

    def test_commented_lines_ignored(self):
        path = self._write_todo(
            "pick abc123 first\n"
            "# update-ref refs/heads/feat/a\n"
            "pick def456 second\n"
            "update-ref refs/heads/feat/b\n"
        )
        try:
            result = _parse_todo_branches(path)
        finally:
            os.unlink(path)
        self.assertEqual(result, ["feat/b"])

    def test_all_commented_returns_empty(self):
        path = self._write_todo("pick abc123 first\n# update-ref refs/heads/feat/a\n")
        try:
            result = _parse_todo_branches(path)
        finally:
            os.unlink(path)
        self.assertEqual(result, [])

    def test_missing_file_returns_empty(self):
        result = _parse_todo_branches("/nonexistent/path/todo")
        self.assertEqual(result, [])

    def test_current_branch_middle_position(self):
        """
        Core regression: master (current branch) at position 2, feat/b after.
        The TODO is the source of truth — git may ignore the update-ref for
        the checked-out branch, but the machete ordering must still be correct.
        """
        path = self._write_todo(
            "pick aaa first\n"
            "# update-ref refs/heads/feat/a\n"
            "pick bbb second\n"
            "update-ref refs/heads/master\n"
            "pick ccc third\n"
            "update-ref refs/heads/feat/b\n"
        )
        try:
            result = _parse_todo_branches(path)
        finally:
            os.unlink(path)
        # master appears before feat/b → correct linear order for machete
        self.assertEqual(result, ["master", "feat/b"])
        self.assertLess(result.index("master"), result.index("feat/b"))

    def test_order_preserved(self):
        """Branch order must match the todo order (oldest-first commit order)."""
        path = self._write_todo(
            "pick h1 s1\nupdate-ref refs/heads/C\n"
            "pick h2 s2\nupdate-ref refs/heads/A\n"
            "pick h3 s3\nupdate-ref refs/heads/B\n"
        )
        try:
            result = _parse_todo_branches(path)
        finally:
            os.unlink(path)
        self.assertEqual(result, ["C", "A", "B"])


class TestGetStackCommits(unittest.TestCase):
    def setUp(self):
        self.mock_run_git = patch("git_stack.src.slice.run_git").start()

    def tearDown(self):
        patch.stopall()

    def test_parse_log_output(self):
        self.mock_run_git.return_value = "hash1 subject one\nhash2 subject two\nhash3 "
        commits = get_stack_commits("main")
        self.assertEqual(len(commits), 3)
        self.assertEqual(commits[0], ("hash1", "subject one"))
        self.assertEqual(commits[1], ("hash2", "subject two"))
        self.assertEqual(commits[2], ("hash3", ""))

    def test_empty_range(self):
        self.mock_run_git.return_value = ""
        self.assertEqual(get_stack_commits("main"), [])


class TestCollectStackBranches(unittest.TestCase):
    def setUp(self):
        self.mock_run_git = patch("git_stack.src.slice.run_git").start()
        self.mock_refs = patch("git_stack.src.slice.get_refs_map").start()
        self.mock_current = patch("git_stack.src.slice.get_current_branch").start()
        self.mock_current.return_value = "master"

    def tearDown(self):
        patch.stopall()

    def _setup_commits(self, commits):
        self.mock_run_git.return_value = "\n".join(f"{h} {s}" for h, s in commits)

    def test_basic_collection(self):
        """Each commit with a single branch → one group per commit."""
        self._setup_commits([("aaa", "msg1"), ("bbb", "msg2")])
        self.mock_refs.return_value = {"main": "000", "feat/a": "aaa", "feat/b": "bbb"}
        result = _collect_stack_branches("main")
        self.assertEqual(result, [["feat/a"], ["feat/b"]])

    def test_base_branch_excluded(self):
        """base_branch is never collected."""
        self._setup_commits([("aaa", "msg1")])
        self.mock_refs.return_value = {"main": "aaa", "feat/a": "aaa"}
        result = _collect_stack_branches("main")
        self.assertEqual(len(result), 1)
        self.assertNotIn("main", result[0])
        self.assertIn("feat/a", result[0])

    def test_current_branch_last_in_group(self):
        """
        When the current branch (master) is at the same commit as an assigned
        branch, it appears last in the group (sibling, not parent).
        """
        self._setup_commits([("aaa", "msg1"), ("bbb", "msg2")])
        self.mock_refs.return_value = {
            "main": "000",
            "feat/a": "aaa",
            "feat/b": "bbb",
            "master": "bbb",  # current branch at same commit as feat/b
        }
        result = _collect_stack_branches("main")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], ["feat/a"])
        # master must come after feat/b in its group
        self.assertIn("feat/b", result[1])
        self.assertIn("master", result[1])
        self.assertLess(result[1].index("feat/b"), result[1].index("master"))

    def test_current_branch_alone_in_group(self):
        """When only the current branch is at a commit, it forms its own group."""
        self._setup_commits([("aaa", "msg1"), ("bbb", "msg2")])
        self.mock_refs.return_value = {
            "main": "000",
            "feat/a": "aaa",
            "master": "bbb",  # no other branch at bbb
        }
        result = _collect_stack_branches("main")
        self.assertEqual(result, [["feat/a"], ["master"]])

    def test_unrelated_branches_excluded(self):
        """Branches outside the commit range are not collected."""
        self._setup_commits([("aaa", "msg1")])
        self.mock_refs.return_value = {"main": "000", "feat/a": "aaa", "other": "zzz"}
        result = _collect_stack_branches("main")
        self.assertEqual(result, [["feat/a"]])

    def test_empty_range(self):
        self.mock_run_git.return_value = ""
        self.assertEqual(_collect_stack_branches("main"), [])


class TestUpdateMachete(unittest.TestCase):
    def setUp(self):
        self.mock_write_machete = patch("git_stack.src.slice.write_machete").start()
        self.mock_parse_machete = patch("git_stack.src.slice.parse_machete").start()

    def tearDown(self):
        patch.stopall()

    def _written_nodes(self):
        return self.mock_write_machete.call_args[0][0]

    def test_linear_chain(self):
        """main -> branch-1 -> branch-2 (one branch per group)."""
        self.mock_parse_machete.return_value = {"main": MacheteNode("main")}
        _update_machete("main", [["branch-1"], ["branch-2"]])
        nodes = self._written_nodes()
        self.assertEqual(nodes["main"].children[0].name, "branch-1")
        self.assertEqual(nodes["main"].children[0].children[0].name, "branch-2")

    def test_siblings_same_commit(self):
        """
        Two branches in the same group → siblings (both children of the same parent).
        Problem 1: current branch (master) must NOT become a child of the assigned branch.
        """
        self.mock_parse_machete.return_value = {"main": MacheteNode("main")}
        # ["feat/last", "master"] — same commit, master is current (last in group)
        _update_machete("main", [["feat/a"], ["feat/last", "master"]])
        nodes = self._written_nodes()

        feat_last = nodes["feat/last"]
        master_node = nodes["master"]

        # Both must be children of feat/a (same parent → siblings)
        self.assertEqual(feat_last.parent.name, "feat/a")
        self.assertEqual(master_node.parent.name, "feat/a")
        # master must NOT be a child of feat/last
        self.assertNotIn("master", [c.name for c in feat_last.children])

    def test_current_branch_in_middle_preserves_children(self):
        """
        Problem 2: current branch (master) at a middle commit.
        After _update_machete([["master"], ["C"]]), existing children of master
        should be preserved and C is appended as a new child.
        """
        root = MacheteNode("main")
        master = MacheteNode("master")
        other = MacheteNode("other")
        master.parent = root
        root.children.append(master)
        other.parent = master
        master.children.append(other)
        self.mock_parse_machete.return_value = {
            "main": root,
            "master": master,
            "other": other,
        }

        _update_machete("main", [["master"], ["C"]])

        nodes = self._written_nodes()
        master_node = nodes["master"]
        child_names = [c.name for c in master_node.children]
        self.assertIn("other", child_names)
        self.assertIn("C", child_names)

    def test_first_branch_in_group_is_next_parent(self):
        """
        With two branches in a group, the FIRST (non-current) branch becomes
        the parent of the subsequent commit's branch.
        """
        self.mock_parse_machete.return_value = {"main": MacheteNode("main")}
        _update_machete("main", [["feat/a", "master"], ["feat/b"]])
        nodes = self._written_nodes()
        # feat/b should be a child of feat/a (the first in the group), not master
        self.assertEqual(nodes["feat/b"].parent.name, "feat/a")

    def test_creates_base_if_missing(self):
        self.mock_parse_machete.return_value = {}
        _update_machete("main", [["branch-1"]])
        nodes = self._written_nodes()
        self.assertIn("main", nodes)
        self.assertEqual(nodes["main"].children[0].name, "branch-1")

    def test_reparenting_insert(self):
        """main -> feather  →  main -> armor -> feather"""
        root = MacheteNode("main")
        feat = MacheteNode("feather")
        feat.parent = root
        root.children.append(feat)
        self.mock_parse_machete.return_value = {"main": root, "feather": feat}

        _update_machete("main", [["armor"], ["feather"]])

        nodes = self._written_nodes()
        main_node = nodes["main"]
        self.assertEqual(main_node.children[0].name, "armor")
        self.assertEqual(main_node.children[0].children[0].name, "feather")
        self.assertNotIn("feather", [c.name for c in main_node.children])

    def test_shrinking_leaves_tail_intact(self):
        """C (not re-sliced) stays as child of B after reducing to [A, B]."""
        root = MacheteNode("main")
        A, B, C = MacheteNode("A"), MacheteNode("B"), MacheteNode("C")
        A.parent = root
        root.children.append(A)
        B.parent = A
        A.children.append(B)
        C.parent = B
        B.children.append(C)
        self.mock_parse_machete.return_value = {"main": root, "A": A, "B": B, "C": C}

        _update_machete("main", [["A"], ["B"]])

        b_node = self._written_nodes()["B"]
        self.assertEqual(len(b_node.children), 1)
        self.assertEqual(b_node.children[0].name, "C")

    def test_fork_reparenting(self):
        """main -> A -> {B, C}  +  slice [NewA, C]  →  NewA takes C; A keeps B."""
        root = MacheteNode("main")
        A, B, C = MacheteNode("A"), MacheteNode("B"), MacheteNode("C")
        A.parent = root
        root.children.append(A)
        B.parent = A
        A.children.append(B)
        C.parent = A
        A.children.append(C)
        self.mock_parse_machete.return_value = {"main": root, "A": A, "B": B, "C": C}

        _update_machete("main", [["NewA"], ["C"]])

        nodes = self._written_nodes()
        main = nodes["main"]
        names = [c.name for c in main.children]
        self.assertIn("A", names)
        self.assertIn("NewA", names)
        a_node = next(c for c in main.children if c.name == "A")
        self.assertEqual(a_node.children[0].name, "B")
        new_a = next(c for c in main.children if c.name == "NewA")
        self.assertEqual(new_a.children[0].name, "C")

    def test_idempotent(self):
        root = MacheteNode("main")
        self.mock_parse_machete.return_value = {"main": root}
        _update_machete("main", [["branch-1"]])
        nodes_run1 = self._written_nodes()

        self.mock_parse_machete.return_value = nodes_run1
        _update_machete("main", [["branch-1"]])
        nodes_run2 = self._written_nodes()

        self.assertEqual(nodes_run2["main"].children[0].name, "branch-1")
        self.assertEqual(len(nodes_run2["main"].children), 1)
