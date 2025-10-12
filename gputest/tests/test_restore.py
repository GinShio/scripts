"""
Tests for gputest restore.
"""
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import time
from gputest.src.restore import run_restore
from gputest.src.context import Context, Console


class TestRestore(unittest.TestCase):
    def setUp(self):
        self.console = MagicMock(spec=Console)
        self.console.dry_run = False
        self.runner = MagicMock()
        self.config = {}
        self.ctx = Context(
            config=self.config,
            console=self.console,
            runner=self.runner,
            project_root=Path("/project"),
            runner_root=Path("/runner"),
            result_dir=Path("/result")
        )

    @patch("gputest.src.restore.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.restore.time.time")
    @patch("gputest.src.restore.Path.glob")
    @patch("gputest.src.restore.Path.exists")
    @patch("gputest.src.restore.ArchiveManager")
    @patch("pathlib.Path.mkdir")
    def test_run_restore(
            self,
            mock_mkdir,
            mock_archive_manager,
            mock_exists,
            mock_glob,
            mock_time,
            mock_gpu):
        mock_exists.return_value = True
        mock_time.return_value = 1000000

        # Mock files
        recent_file = MagicMock()
        recent_file.stat.return_value.st_mtime = 1000000 - \
            (1 * 86400)  # 1 day old
        recent_file.name = "suite1_gpu1_date.tar.zst"
        recent_file.parent.name = "driver1"

        old_file = MagicMock()
        old_file.stat.return_value.st_mtime = 1000000 - \
            (20 * 86400)  # 20 days old
        old_file.name = "suite1_gpu1_old.tar.zst"

        mock_glob.return_value = [recent_file, old_file]

        run_restore(self.ctx, days=10)

        # Should restore recent file
        mock_archive_manager.return_value.extract_archive.assert_called_with(
            archive_path=recent_file,
            destination_dir=self.ctx.runner_root / "baseline" / "driver1" / "suite1_date"
        )

        # Verify glob pattern
        mock_glob.assert_called_with("*/*_gpu1_*.tar.zst")

    @patch("gputest.src.restore.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.restore.ArchiveManager")
    @patch("pathlib.Path.glob")
    @patch("time.time")
    @patch("pathlib.Path.exists")
    def test_run_restore_dry_run(
            self,
            mock_exists,
            mock_time,
            mock_glob,
            mock_archive_manager,
            mock_gpu):
        self.console.dry_run = True
        mock_exists.return_value = True
        mock_time.return_value = 1000000

        # Mock files
        recent_file = MagicMock()
        recent_file.stat.return_value.st_mtime = 1000000 - \
            (1 * 86400)  # 1 day old
        recent_file.name = "suite1_gpu1_date.tar.zst"
        recent_file.parent.name = "driver1"

        mock_glob.return_value = [recent_file]

        run_restore(self.ctx, days=10)

        # Should NOT call extract_archive
        mock_archive_manager.return_value.extract_archive.assert_not_called()

        # Should call tar command
        self.runner.run.assert_called()
        # Check for tar command
        tar_calls = [c for c in self.runner.run.call_args_list if c[0][0][0] == "tar"]
        self.assertTrue(len(tar_calls) > 0)
