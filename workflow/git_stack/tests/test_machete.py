import unittest
from unittest.mock import mock_open, patch

from git_stack.src import machete
from git_stack.src.machete import (
    MacheteNode,
    get_ancestors,
    get_anno_blocks,
    get_subtree_nodes,
)


class TestMachete(unittest.TestCase):
    @patch("git_stack.src.machete.get_machete_file_path")
    @patch("os.path.exists")
    def test_parse_machete_empty(self, mock_exists, mock_path):
        mock_path.return_value = "/path/to/machete"
        mock_exists.return_value = False

        nodes = machete.parse_machete()
        self.assertEqual(nodes, {})

    @patch("git_stack.src.machete.get_machete_file_path")
    @patch("os.path.exists")
    def test_parse_machete_simple(self, mock_exists, mock_path):
        mock_path.return_value = "/path/to/machete"
        mock_exists.return_value = True

        content = "main\n    feature1\n        feature2\n    feature3"
        with patch("builtins.open", mock_open(read_data=content)):
            nodes = machete.parse_machete()

        self.assertIn("main", nodes)
        self.assertIn("feature1", nodes)
        self.assertIn("feature2", nodes)
        self.assertIn("feature3", nodes)

        self.assertIsNone(nodes["main"].parent)
        self.assertEqual(len(nodes["main"].children), 2)

        f1 = nodes["feature1"]
        self.assertEqual(f1.parent.name, "main")
        self.assertEqual(len(f1.children), 1)
        self.assertEqual(f1.children[0].name, "feature2")

        f3 = nodes["feature3"]
        self.assertEqual(f3.parent.name, "main")

    def test_write_machete_structure(self):
        # Create a structure in memory
        root = machete.MacheteNode("main")
        c1 = machete.MacheteNode("child1")
        c1.parent = root
        root.children.append(c1)

        c2 = machete.MacheteNode("child2")
        c2.parent = c1
        c1.children.append(c2)

        nodes = {"main": root, "child1": c1, "child2": c2}

        with (
            patch("git_stack.src.machete.get_machete_file_path", return_value="dummy"),
            patch("builtins.open", mock_open()) as m,
        ):
            machete.write_machete(nodes)

            m.assert_called_with("dummy", "w")

            handle = m.return_value
            # Expect calls to write
            # main
            #     child1
            #         child2

            # Using write(str) calls. join is used.
            m.assert_called_with("dummy", "w")
            written_content = handle.write.call_args[0][0]
            expected = "main\n    child1\n        child2\n"
            self.assertEqual(written_content, expected)


# ---------------------------------------------------------------------------
# Helpers: build in-memory trees without touching the filesystem
# ---------------------------------------------------------------------------


def _make_linear_tree():
    """main -> A -> B  (pure linear)"""
    main = MacheteNode("main", 0)
    a = MacheteNode("A", 4)
    b = MacheteNode("B", 8)
    a.parent = main
    main.children.append(a)
    b.parent = a
    a.children.append(b)
    return {"main": main, "A": a, "B": b}


def _make_fork_tree():
    """
    main -> A (fork-point) -> B
                           -> C
    """
    main = MacheteNode("main", 0)
    a = MacheteNode("A", 4)
    b = MacheteNode("B", 8)
    c = MacheteNode("C", 8)
    a.parent = main
    main.children.append(a)
    b.parent = a
    a.children.append(b)
    c.parent = a
    a.children.append(c)
    return {"main": main, "A": a, "B": b, "C": c}


def _make_deep_fork_tree():
    """
    main -> A -> B (fork-point) -> C (fork-point) -> E
                                                   -> G
                               -> D -> F
    """
    main = MacheteNode("main", 0)
    a = MacheteNode("A", 4)
    b = MacheteNode("B", 8)
    c = MacheteNode("C", 12)
    d = MacheteNode("D", 12)
    e = MacheteNode("E", 16)
    f = MacheteNode("F", 16)
    g = MacheteNode("G", 16)
    a.parent = main
    main.children.append(a)
    b.parent = a
    a.children.append(b)
    c.parent = b
    b.children.append(c)
    d.parent = b
    b.children.append(d)
    e.parent = c
    c.children.append(e)
    g.parent = c
    c.children.append(g)
    f.parent = d
    d.children.append(f)
    return {"main": main, "A": a, "B": b, "C": c, "D": d, "E": e, "F": f, "G": g}


# ---------------------------------------------------------------------------
# get_ancestors
# ---------------------------------------------------------------------------


class TestGetAncestors(unittest.TestCase):
    def test_root_has_no_ancestors(self):
        nodes = _make_linear_tree()
        self.assertEqual(get_ancestors(nodes["main"]), [])

    def test_single_level(self):
        nodes = _make_linear_tree()
        names = [n.name for n in get_ancestors(nodes["A"])]
        self.assertEqual(names, ["main"])

    def test_deep_chain(self):
        nodes = _make_linear_tree()
        names = [n.name for n in get_ancestors(nodes["B"])]
        self.assertEqual(names, ["main", "A"])

    def test_fork_child(self):
        nodes = _make_fork_tree()
        self.assertEqual([n.name for n in get_ancestors(nodes["C"])], ["main", "A"])


# ---------------------------------------------------------------------------
# get_subtree_nodes
# ---------------------------------------------------------------------------


class TestGetSubtreeNodes(unittest.TestCase):
    def test_leaf_returns_self(self):
        nodes = _make_linear_tree()
        result = {n.name for n in get_subtree_nodes(nodes["B"])}
        self.assertEqual(result, {"B"})

    def test_fork_subtree(self):
        nodes = _make_fork_tree()
        result = {n.name for n in get_subtree_nodes(nodes["A"])}
        self.assertEqual(result, {"A", "B", "C"})

    def test_full_tree_from_root(self):
        nodes = _make_fork_tree()
        result = {n.name for n in get_subtree_nodes(nodes["main"])}
        self.assertEqual(result, {"main", "A", "B", "C"})

    def test_preorder(self):
        nodes = _make_linear_tree()
        names = [n.name for n in get_subtree_nodes(nodes["main"])]
        self.assertEqual(names, ["main", "A", "B"])

    def test_deep_fork_full(self):
        nodes = _make_deep_fork_tree()
        result = {n.name for n in get_subtree_nodes(nodes["B"])}
        self.assertEqual(result, {"B", "C", "D", "E", "F", "G"})


# ---------------------------------------------------------------------------
# get_anno_blocks
# ---------------------------------------------------------------------------


class TestGetAnnoBlocks(unittest.TestCase):
    # --- linear tree ---

    def test_leaf_single_block(self):
        """B is a leaf: one block = all ancestors + B."""
        nodes = _make_linear_tree()
        blocks = get_anno_blocks(nodes["B"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual([n.name for n in blocks[0]], ["main", "A", "B"])

    def test_single_child_walks_to_next_stop(self):
        """A has one child B (leaf): block = [main, A, B]."""
        nodes = _make_linear_tree()
        blocks = get_anno_blocks(nodes["A"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual([n.name for n in blocks[0]], ["main", "A", "B"])

    def test_root_single_child_stops_at_fork(self):
        """
        In the fork tree, main has one child A which is a fork-point (2 children).
        path_to_next_fork_or_leaf(A) should return [A] only (fork-point: stop).
        So block = [main, A].
        """
        nodes = _make_fork_tree()
        blocks = get_anno_blocks(nodes["main"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual([n.name for n in blocks[0]], ["main", "A"])

    # --- fork tree ---

    def test_fork_point_two_blocks(self):
        """A is a fork-point with children B, C: two blocks."""
        nodes = _make_fork_tree()
        blocks = get_anno_blocks(nodes["A"])
        self.assertEqual(len(blocks), 2)
        self.assertEqual([n.name for n in blocks[0]], ["main", "A", "B"])
        self.assertEqual([n.name for n in blocks[1]], ["main", "A", "C"])

    def test_fork_child_leaf_block(self):
        """B is a leaf child of A: linear block through ancestors."""
        nodes = _make_fork_tree()
        blocks = get_anno_blocks(nodes["B"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual([n.name for n in blocks[0]], ["main", "A", "B"])

    # --- deep fork tree ---

    def test_deep_fork_point_b(self):
        """
        B is a fork-point (children C, D).
        Block 0: [main, A, B, C]  — C is itself a fork-point, path stops at C.
        Block 1: [main, A, B, D, F]  — D has one child F (leaf).
        """
        nodes = _make_deep_fork_tree()
        blocks = get_anno_blocks(nodes["B"])
        self.assertEqual(len(blocks), 2)
        self.assertEqual([n.name for n in blocks[0]], ["main", "A", "B", "C"])
        self.assertEqual([n.name for n in blocks[1]], ["main", "A", "B", "D", "F"])

    def test_deep_fork_point_c(self):
        """C is a fork-point (children E, G): two leaf blocks."""
        nodes = _make_deep_fork_tree()
        blocks = get_anno_blocks(nodes["C"])
        self.assertEqual(len(blocks), 2)
        self.assertEqual([n.name for n in blocks[0]], ["main", "A", "B", "C", "E"])
        self.assertEqual([n.name for n in blocks[1]], ["main", "A", "B", "C", "G"])

    def test_deep_non_fork_d(self):
        """D has one child F (leaf): one linear block."""
        nodes = _make_deep_fork_tree()
        blocks = get_anno_blocks(nodes["D"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual([n.name for n in blocks[0]], ["main", "A", "B", "D", "F"])

    def test_deep_ancestor_a_stops_at_b(self):
        """A has one child B which is a fork-point: path stops at B."""
        nodes = _make_deep_fork_tree()
        blocks = get_anno_blocks(nodes["A"])
        self.assertEqual(len(blocks), 1)
        self.assertEqual([n.name for n in blocks[0]], ["main", "A", "B"])
