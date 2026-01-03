"""
Tests for gputest toolbox.
"""
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
from gputest.src.toolbox import run_toolbox, force_copytree
from gputest.src.context import Context, Console
import shutil
import os
import tempfile


class TestToolbox(unittest.TestCase):
    def setUp(self):
        self.console = MagicMock(spec=Console)
        self.console.dry_run = False
        self.runner = MagicMock()
        self.config = {
            "toolbox": {
                "test": {
                    "src": "{{project_root}}/src",
                    "dest": "dest",
                    "excludes": ["*.txt"],
                    "post_install": ["hook1"]
                }
            },
            "hooks": {
                "hook1": "echo {{dest}}"
            }
        }
        self.ctx = Context(
            config=self.config,
            console=self.console,
            runner=self.runner,
            project_root=Path("/project"),
            runner_root=Path("/runner"),
            result_dir=Path("/result")
        )

    @patch("gputest.src.toolbox.force_copytree")
    @patch("gputest.src.toolbox.Path.exists")
    def test_run_toolbox(self, mock_exists, mock_force_copytree):
        mock_exists.return_value = True

        run_toolbox(self.ctx, ["test"])

        # Verify copy
        mock_force_copytree.assert_called()
        args, kwargs = mock_force_copytree.call_args
        self.assertEqual(str(args[0]), "/project/src")
        self.assertEqual(str(args[1]), "/runner/dest")

        # Verify hook
        self.runner.run.assert_called()
        cmd = self.runner.run.call_args[0][0]
        self.assertIn("echo /runner/dest", cmd[2])

    @patch("gputest.src.toolbox.force_copytree")
    @patch("gputest.src.toolbox.Path.exists")
    def test_run_toolbox_multiple_suites(
            self, mock_exists, mock_force_copytree):
        mock_exists.return_value = True

        # Add another suite
        self.config["toolbox"]["suite2"] = {
            "src": "src2",
            "dest": "dest2"
        }

        run_toolbox(self.ctx)  # Run all

        # Verify both were copied
        self.assertEqual(mock_force_copytree.call_count, 2)

    @patch("gputest.src.toolbox.force_copytree")
    @patch("gputest.src.toolbox.Path.exists")
    def test_run_toolbox_filter(self, mock_exists, mock_force_copytree):
        mock_exists.return_value = True

        # Add another suite
        self.config["toolbox"]["suite2"] = {
            "src": "src2",
            "dest": "dest2"
        }

        run_toolbox(self.ctx, ["test"])  # Run only 'test'

        # Verify only one was copied
        self.assertEqual(mock_force_copytree.call_count, 1)
        args, kwargs = mock_force_copytree.call_args
        self.assertIn("dest", str(args[1]))

    @patch("gputest.src.toolbox.force_copytree")
    @patch("gputest.src.toolbox.Path")
    def test_run_toolbox_includes(self, mock_path_cls, mock_force_copytree):
        # Setup the mock for the source path
        mock_src = MagicMock()
        mock_dest = MagicMock()

        # Configure includes
        self.config["toolbox"]["test"]["includes"] = ["include1"]

        # Mock Path constructor
        mock_path_instance = MagicMock()
        mock_path_cls.return_value = mock_path_instance
        mock_path_instance.exists.return_value = True

        # Setup glob results
        mock_item = MagicMock()
        mock_item.name = "include1"
        mock_item.is_dir.return_value = True
        mock_item.relative_to.return_value = Path("include1")

        mock_path_instance.glob.return_value = [mock_item]

        # Run
        run_toolbox(self.ctx, ["test"])

        # Verify copytree was called for the item
        mock_force_copytree.assert_called()

    def test_run_toolbox_dry_run(self):
        self.console.dry_run = True
        with patch("gputest.src.toolbox.Path.exists", return_value=True):
            run_toolbox(self.ctx, ["test"])
            self.runner.run.assert_called()
            # Check first call (copy)
            args = self.runner.run.call_args_list[0][0][0]
            self.assertEqual(args[0], "cp")

    @patch("gputest.src.toolbox.force_copytree")
    @patch("gputest.src.toolbox.Path.exists")
    def test_run_toolbox_paths(self, mock_exists, mock_force_copytree):
        mock_exists.return_value = True

        # Configure suite with paths
        self.config["toolbox"]["test_paths"] = {
            "src": "{{project_root}}/src",
            "dest": "{{runner_root}}/base_dest",
            "paths": [
                {"src": "sub1", "dest": "sub_dest"}
            ]
        }

        run_toolbox(self.ctx, ["test_paths"])

        # Verify copy
        mock_force_copytree.assert_called()
        args, kwargs = mock_force_copytree.call_args
        self.assertEqual(str(args[0]), "/project/src/sub1")
        self.assertEqual(str(args[1]), "/runner/base_dest/sub_dest")

    @patch("gputest.src.toolbox.force_copytree")
    @patch("gputest.src.toolbox.Path.exists")
    def test_run_toolbox_paths_hook_context(
            self, mock_exists, mock_force_copytree):
        mock_exists.return_value = True

        # Configure suite with paths and hook
        self.config["toolbox"]["test_paths"] = {
            "src": "{{project_root}}/src",
            "dest": "{{runner_root}}/base_dest",
            "post_install": ["hook1"],
            "paths": [
                {"src": "sub1", "dest": "sub_dest"}
            ]
        }

        run_toolbox(self.ctx, ["test_paths"])

        # Verify hook uses base_dest, not sub_dest
        self.runner.run.assert_called()
        cmd = self.runner.run.call_args[0][0]
        # hook1 is "echo {{dest}}"
        # We expect "/runner/base_dest", NOT "/runner/base_dest/sub_dest"
        self.assertIn("echo /runner/base_dest", cmd[2])

    def test_ignore_func(self):
        from gputest.src.toolbox import _create_ignore_func

        root = Path("/root")
        excludes = ["foo/bar.txt", "*.log"]
        ignore = _create_ignore_func(root, excludes)

        # Test ignoring file by name
        ignored = ignore(str(root / "subdir"), ["test.log", "keep.txt"])
        self.assertIn("test.log", ignored)
        self.assertNotIn("keep.txt", ignored)

        # Test ignoring file by path
        # We are visiting /root/foo
        ignored = ignore(str(root / "foo"), ["bar.txt", "baz.txt"])
        self.assertIn("bar.txt", ignored)  # Matches foo/bar.txt
        self.assertNotIn("baz.txt", ignored)

        # Test ignoring file by path (no match)
        ignored = ignore(str(root / "other"), ["bar.txt"])
        self.assertNotIn("bar.txt", ignored)  # other/bar.txt != foo/bar.txt


class TestForceCopyTree(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.src = Path(self.test_dir) / "src"
        self.dst = Path(self.test_dir) / "dst"
        self.src.mkdir()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_force_copytree_basic(self):
        # Create some files
        (self.src / "file1.txt").write_text("content1")
        (self.src / "subdir").mkdir()
        (self.src / "subdir" / "file2.txt").write_text("content2")

        force_copytree(self.src, self.dst)

        self.assertTrue((self.dst / "file1.txt").exists())
        self.assertEqual((self.dst / "file1.txt").read_text(), "content1")
        self.assertTrue((self.dst / "subdir" / "file2.txt").exists())

    def test_force_copytree_overwrite_file(self):
        # Setup dest with conflicting file
        self.dst.mkdir()
        (self.dst / "file1.txt").write_text("old_content")

        (self.src / "file1.txt").write_text("new_content")

        force_copytree(self.src, self.dst, dirs_exist_ok=True)

        self.assertEqual((self.dst / "file1.txt").read_text(), "new_content")

    def test_force_copytree_overwrite_symlink_with_file(self):
        # Dest has symlink
        self.dst.mkdir()
        (self.dst / "target").write_text("target")
        os.symlink("target", self.dst / "link")

        # Src has file with same name as link
        (self.src / "link").write_text("im_a_file")

        force_copytree(self.src, self.dst, dirs_exist_ok=True)

        self.assertTrue((self.dst / "link").is_file())
        self.assertFalse((self.dst / "link").is_symlink())
        self.assertEqual((self.dst / "link").read_text(), "im_a_file")

    def test_force_copytree_overwrite_file_with_symlink(self):
        # Dest has file
        self.dst.mkdir()
        (self.dst / "link").write_text("im_a_file")

        # Src has symlink
        (self.src / "target").write_text("target")
        os.symlink("target", self.src / "link")

        force_copytree(self.src, self.dst, dirs_exist_ok=True)

        self.assertTrue((self.dst / "link").is_symlink())
        self.assertEqual(os.readlink(self.dst / "link"), "target")

    def test_force_copytree_overwrite_symlink_with_symlink(self):
        # Dest has symlink -> target1
        self.dst.mkdir()
        os.symlink("target1", self.dst / "link")

        # Src has symlink -> target2
        os.symlink("target2", self.src / "link")

        force_copytree(self.src, self.dst, dirs_exist_ok=True)

        self.assertTrue((self.dst / "link").is_symlink())
        self.assertEqual(os.readlink(self.dst / "link"), "target2")
