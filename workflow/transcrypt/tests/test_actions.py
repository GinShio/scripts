import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add the workflow directory to sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), "../../.."))
sys.path.append(os.path.join(os.path.dirname(__file__), "../../"))

from workflow.core import crypto
from workflow.transcrypt.src import actions


class TestActions(unittest.TestCase):
    def setUp(self):
        # Reset any patched modules if needed
        pass

    @patch("workflow.transcrypt.src.actions.get_git_config")
    def test_get_password_success(self, mock_get_config):
        mock_get_config.return_value = "secret"
        pwd = actions._get_password("ctx")
        self.assertEqual(pwd, "secret")
        mock_get_config.assert_called_with("transcrypt.ctx.password")

    @patch("workflow.transcrypt.src.actions.get_git_config")
    def test_get_password_fail(self, mock_get_config):
        mock_get_config.return_value = None
        with self.assertRaises(ValueError):
            actions._get_password("ctx")

    @patch("workflow.transcrypt.src.actions.sys.stdin")
    @patch("workflow.transcrypt.src.actions.sys.stdout")
    @patch("workflow.transcrypt.src.actions._get_password")
    @patch("workflow.transcrypt.src.actions.crypto.encrypt")
    def test_clean_encrypts(self, mock_encrypt, mock_get_pwd, mock_stdout, mock_stdin):
        # Setup input
        mock_stdin.buffer.read.return_value = b"plain text"

        # Setup config
        mock_get_pwd.return_value = "password"

        # Setup expected calls
        mock_encrypt.return_value = b"encrypted"

        # Run
        actions.clean("default", file_path="my/file.txt")

        # Verify
        mock_encrypt.assert_called_once()
        args = mock_encrypt.call_args
        self.assertEqual(args[0][0], b"plain text")
        self.assertEqual(args[0][1], "password")
        # Check kwargs
        kwargs = args[1]
        self.assertEqual(kwargs.get("deterministic"), True)
        self.assertEqual(kwargs.get("context"), b"my/file.txt")

        mock_stdout.buffer.write.assert_called_with(b"encrypted")

    @patch("workflow.transcrypt.src.actions.sys.stdin")
    @patch("workflow.transcrypt.src.actions.sys.stdout")
    def test_clean_empty(self, mock_stdout, mock_stdin):
        mock_stdin.buffer.read.return_value = b""
        actions.clean("default")
        mock_stdout.buffer.write.assert_not_called()

    @patch("workflow.transcrypt.src.actions.sys.stdin")
    @patch("workflow.transcrypt.src.actions.sys.stdout")
    @patch("workflow.transcrypt.src.actions._get_password")
    @patch("workflow.transcrypt.src.actions.crypto.decrypt")
    @patch("workflow.transcrypt.src.actions.base64")
    def test_smudge_decrypts(
        self, mock_b64, mock_decrypt, mock_get_pwd, mock_stdout, mock_stdin
    ):
        # Setup input
        encrypted_data = b"encrypted stuff"
        mock_stdin.buffer.read.return_value = encrypted_data

        # Setup b64 check to pass (it simulates validation of Salted__ header)
        mock_b64.b64decode.return_value = crypto.SALT_HEADER + b"..."

        mock_get_pwd.return_value = "password"
        mock_decrypt.return_value = b"plain text"

        actions.smudge("default", file_path="my/file.txt")

        mock_decrypt.assert_called_once()
        kwargs = mock_decrypt.call_args[1]
        self.assertEqual(kwargs.get("deterministic"), True)
        self.assertEqual(kwargs.get("context"), b"my/file.txt")
        mock_stdout.buffer.write.assert_called_with(b"plain text")

    @patch("workflow.transcrypt.src.actions.sys.stdin")
    @patch("workflow.transcrypt.src.actions.sys.stdout")
    @patch("workflow.transcrypt.src.actions.base64")
    def test_smudge_not_encrypted_passthrough(self, mock_b64, mock_stdout, mock_stdin):
        # If it doesn't look like b64 or doesn't have header
        # Case 1: Not valid base64
        mock_stdin.buffer.read.return_value = b"just plain text"
        mock_b64.b64decode.side_effect = Exception("Not b64")

        actions.smudge("default")

        mock_stdout.buffer.write.assert_called_with(b"just plain text")

    @patch("workflow.transcrypt.src.actions.sys.stdin")
    @patch("workflow.transcrypt.src.actions.sys.stdout")
    @patch("workflow.transcrypt.src.actions.base64")
    def test_smudge_encrypted_but_no_header_passthrough(
        self, mock_b64, mock_stdout, mock_stdin
    ):
        # Case 2: Valid b64 but no header
        mock_stdin.buffer.read.return_value = b"SGVsbG8="  # Hello
        mock_b64.b64decode.return_value = b"Hello"

        actions.smudge("default")

        mock_stdout.buffer.write.assert_called_with(b"SGVsbG8=")

    @patch("workflow.transcrypt.src.actions.set_git_config")
    @patch("workflow.transcrypt.src.actions.sys.executable", "/usr/bin/python3")
    @patch("workflow.transcrypt.src.actions.get_relative_path")
    @patch("sys.argv", ["/path/to/script.py"])
    def test_install(self, mock_rel_path, mock_set_config):
        mock_rel_path.return_value = Path("script.py")

        actions.install("default")

        # Verify calls
        # We expect 4 calls: clean, smudge, required, textconv
        self.assertEqual(mock_set_config.call_count, 4)

        mock_set_config.assert_any_call("filter.transcrypt.required", "true")

        # Check command structure somewhat
        args_clean = mock_set_config.call_args_list[0][0]
        self.assertEqual(args_clean[0], "filter.transcrypt.clean")
        self.assertIn("/usr/bin/python3 script.py", args_clean[1])
        self.assertIn("-c default clean %f", args_clean[1])

    @patch("workflow.transcrypt.src.actions.unset_git_config")
    def test_uninstall(self, mock_unset_config):
        actions.uninstall("default")
        self.assertEqual(mock_unset_config.call_count, 4)
        mock_unset_config.assert_any_call("filter.transcrypt.clean")
        mock_unset_config.assert_any_call("filter.transcrypt.smudge")
        mock_unset_config.assert_any_call("filter.transcrypt.required")
        mock_unset_config.assert_any_call("diff.transcrypt.textconv")

    @patch("workflow.transcrypt.src.actions.sys.stdout")
    @patch("workflow.transcrypt.src.actions._get_password")
    @patch("workflow.transcrypt.src.actions.crypto.decrypt")
    @patch("workflow.transcrypt.src.actions.base64")
    @patch("builtins.open")
    def test_textconv(
        self, mock_open, mock_b64, mock_decrypt, mock_get_pwd, mock_stdout
    ):
        # Mock file open
        mock_file = MagicMock()
        mock_file.__enter__.return_value = mock_file
        mock_file.read.return_value = b"encrypted data"
        mock_open.return_value = mock_file

        # Mock validation
        mock_b64.b64decode.return_value = crypto.SALT_HEADER + b"..."

        mock_get_pwd.return_value = "pass"
        mock_decrypt.return_value = b"plain text"

        actions.textconv("somefile.txt")

        mock_decrypt.assert_called_once()
        mock_stdout.buffer.write.assert_called_with(b"plain text")

    @patch("workflow.transcrypt.src.actions.get_git_config")
    @patch("workflow.transcrypt.src.actions.sys.stdout")  # capture print
    def test_status(self, mock_stdout, mock_get_config):
        # Mock returns: pwd, cipher, digest, iterations, clean_filter (installed)
        mock_get_config.side_effect = ["secret", "aes", "sha", "100", "some cmd"]

        actions.status("default")

        # Just ensure no exception and config was queried
        self.assertEqual(mock_get_config.call_count, 5)


if __name__ == "__main__":
    unittest.main()
