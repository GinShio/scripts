import unittest
from unittest.mock import mock_open, patch

from git_stack.src import machete


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
