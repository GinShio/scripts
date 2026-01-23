import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add the workflow directory to sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.append(os.path.join(os.path.dirname(__file__), "../../"))

from workflow.transcrypt.src import cli


class TestCli(unittest.TestCase):
    @patch("workflow.transcrypt.src.actions.clean")
    def test_clean_command(self, mock_clean):
        args = ["clean"]
        ret = cli.main(args)
        self.assertEqual(ret, 0)
        mock_clean.assert_called_with(context="default", file_path=None)

    @patch("workflow.transcrypt.src.actions.clean")
    def test_clean_command_with_context(self, mock_clean):
        args = ["-c", "custom", "clean", "somefile.txt"]
        ret = cli.main(args)
        self.assertEqual(ret, 0)
        mock_clean.assert_called_with(context="custom", file_path="somefile.txt")

    @patch("workflow.transcrypt.src.actions.smudge")
    def test_smudge_command(self, mock_smudge):
        args = ["smudge"]
        ret = cli.main(args)
        self.assertEqual(ret, 0)
        mock_smudge.assert_called_with(context="default", file_path=None)

    @patch("workflow.transcrypt.src.actions.install")
    def test_install_command(self, mock_install):
        args = ["install"]
        ret = cli.main(args)
        self.assertEqual(ret, 0)
        mock_install.assert_called_with(context="default")

    @patch("workflow.transcrypt.src.actions.uninstall")
    def test_uninstall_command(self, mock_uninstall):
        args = ["uninstall"]
        ret = cli.main(args)
        self.assertEqual(ret, 0)
        mock_uninstall.assert_called_with(context="default")

    @patch("workflow.transcrypt.src.actions.status")
    def test_status_command(self, mock_status):
        args = ["status"]
        ret = cli.main(args)
        self.assertEqual(ret, 0)
        mock_status.assert_called_with(context="default")

    @patch("workflow.transcrypt.src.actions.clean")
    def test_exception_handling(self, mock_clean):
        mock_clean.side_effect = Exception("Boom")
        # Capture stderr to avoid printing to console during test
        with patch("sys.stderr", new=unittest.mock.Mock()) as mock_stderr:
            ret = cli.main(["clean"])
            self.assertEqual(ret, 1)


if __name__ == "__main__":
    unittest.main()
