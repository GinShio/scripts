"""
Tests for gputest cleanup.
"""

import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gputest.src.cleanup import run_cleanup
from gputest.src.context import Console, Context


class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.console = MagicMock(spec=Console)
        self.console.dry_run = False
        self.runner = MagicMock()
        self.config = {
            "global": {"archive_retention_days": 10, "result_retention_days": 5}
        }
        self.ctx = Context(
            config=self.config,
            console=self.console,
            runner=self.runner,
            project_root=Path("/project"),
            runner_root=Path("/runner"),
            result_dir=Path("/result"),
        )

    @patch("gputest.src.cleanup.shutil")
    @patch("gputest.src.cleanup.time.time")
    @patch("gputest.src.cleanup.Path.glob")
    @patch("gputest.src.cleanup.Path.exists")
    @patch("gputest.src.cleanup.Path.iterdir")
    def test_run_cleanup(
        self, mock_iterdir, mock_exists, mock_glob, mock_time, mock_shutil
    ):
        mock_exists.return_value = True
        mock_time.return_value = 1000000

        # Mock files
        old_file = MagicMock()
        old_file.stat.return_value.st_mtime = 1000000 - (11 * 86400)  # 11 days old
        old_file.name = "old.tar.zst"

        new_file = MagicMock()
        new_file.stat.return_value.st_mtime = 1000000 - (9 * 86400)  # 9 days old
        new_file.name = "new.tar.zst"

        mock_glob.return_value = [old_file, new_file]

        # Mock iterdir for runtime results
        old_result = MagicMock()
        old_result.stat.return_value.st_mtime = 1000000 - (6 * 86400)  # 6 days old
        old_result.name = "old_result"
        old_result.is_dir.return_value = True

        new_result = MagicMock()
        new_result.stat.return_value.st_mtime = 1000000 - (4 * 86400)  # 4 days old
        new_result.name = "new_result"

        # iterdir is called twice: once for result_dir (empty dir check) and once for runner_root subdirs
        # We need to handle the calls.
        # The code calls:
        # 1. ctx.result_dir.iterdir() (for empty dir cleanup)
        # 2. (ctx.runner_root / subdir).iterdir() (for result cleanup)

        # Let's simplify by mocking iterdir to return empty list for the first call (result_dir cleanup)
        # and the results for the second call.
        # However, mock_iterdir is the same mock object.

        # Mock group dir
        mock_group = MagicMock()
        mock_group.is_dir.return_value = True
        mock_group.iterdir.return_value = [old_result, new_result]
        mock_group.name = "test_group"

        mock_iterdir.side_effect = [
            [],  # result_dir empty dir check
            [mock_group],  # results
            [],  # baseline
        ]

        run_cleanup(self.ctx)

        self.runner.run.assert_any_call(["rm", str(old_file)], check=False)
        new_file.unlink.assert_not_called()

        self.runner.run.assert_any_call(["rm", "-rf", str(old_result)], check=False)

        # Verify glob pattern
        mock_glob.assert_called_with("**/*.tar.zst")

    def test_run_cleanup_dry_run(self):
        self.console.dry_run = True
        self.ctx.result_dir = MagicMock()

        # Mock files
        old_file = MagicMock()
        old_file.stat.return_value.st_mtime = 0
        old_file.name = "old.tar.zst"

        self.ctx.result_dir.glob.return_value = [old_file]
        self.ctx.result_dir.exists.return_value = True

        run_cleanup(self.ctx)

        # Should not unlink
        old_file.unlink.assert_not_called()
        # Should run rm command
        self.runner.run.assert_called()
        cmd = self.runner.run.call_args[0][0]
        self.assertEqual(cmd[0], "rm")
        self.assertEqual(cmd[1], str(old_file))

    def test_run_cleanup_empty_dirs(self):
        self.console.dry_run = False
        self.ctx.result_dir = MagicMock()
        self.ctx.result_dir.exists.return_value = True
        self.ctx.result_dir.glob.return_value = []

        # Mock empty dir
        empty_dir = MagicMock()
        empty_dir.is_dir.return_value = True
        empty_dir.iterdir.return_value = []  # Empty

        # Mock non-empty dir
        full_dir = MagicMock()
        full_dir.is_dir.return_value = True
        full_dir.iterdir.return_value = [MagicMock()]  # Not empty

        self.ctx.result_dir.iterdir.return_value = [empty_dir, full_dir]

        run_cleanup(self.ctx)

        empty_dir.rmdir.assert_called()
        full_dir.rmdir.assert_not_called()
