"""
Tests for gputest runner.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gputest.src.context import Console, Context
from gputest.src.runner import get_gpu_device_id, run_tests


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.console = MagicMock(spec=Console)
        self.console.dry_run = False
        self.runner = MagicMock()
        self.config = {
            "tests": {"test1": {"driver": "driver1", "suite": "suite1"}},
            "drivers": {"driver1": {"layout": "layout1", "root": "/usr"}},
            "layouts": {
                "layout1": {"env": {"VAR": "VAL", "AVAILABLE_CPUS_CNT": "{{jobs}}"}}
            },
            "suites": {"suite1": {"exe": "test_exe", "args": ["--arg"]}},
            "backends": {},
            "hooks": {},
        }
        # Use temporary directories for runner_root and result_dir to avoid
        # permission issues when tests run on systems where /runner or
        # /result are not writable.
        self._tmp_runner = tempfile.TemporaryDirectory(prefix="gputest-runner-")
        self._tmp_result = tempfile.TemporaryDirectory(prefix="gputest-result-")
        self.ctx = Context(
            config=self.config,
            console=self.console,
            runner=self.runner,
            project_root=Path("/project"),
            runner_root=Path(self._tmp_runner.name),
            result_dir=Path(self._tmp_result.name),
        )

    def tearDown(self):
        try:
            self._tmp_runner.cleanup()
        except Exception:
            pass
        try:
            self._tmp_result.cleanup()
        except Exception:
            pass

    @patch("gputest.src.runner.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.runner.os.cpu_count", return_value=4)
    @patch("gputest.src.runner.ArchiveManager")
    @patch("gputest.src.runner.get_gpu_id_from_vulkan", return_value=None)
    @patch("gputest.src.runner.get_gpu_id_from_gl", return_value=None)
    def test_run_tests(
        self, mock_get_gl, mock_get_vk, mock_archive_manager, mock_cpu, mock_gpu
    ):
        run_tests(self.ctx, ["test1"])

        self.runner.run.assert_called()
        # Check command
        # First call might be git hook if configured, but here no hooks.
        # Actually, run_tests calls runner.run for the test execution.
        # And then for tar.

        # We expect at least one call for the test
        calls = self.runner.run.call_args_list
        test_call = calls[0]
        cmd = test_call[0][0]
        self.assertIn("test_exe", cmd)
        self.assertIn("--arg", cmd)

        # Check env
        env = test_call[1]["env"]
        self.assertEqual(env["VAR"], "VAL")
        self.assertEqual(env["AVAILABLE_CPUS_CNT"], "3")

        # Check archive
        # In this test case, runner_bin is not set to deqp-runner or piglit, so archiving logic is skipped.
        # Let's update the test case to trigger archiving logic.

    @patch("gputest.src.runner.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.runner.os.cpu_count", return_value=4)
    @patch("gputest.src.runner.shutil")
    @patch("gputest.src.runner.os.replace")
    @patch("gputest.src.runner.ArchiveManager")
    @patch("gputest.src.runner.get_gpu_id_from_vulkan")
    @patch("gputest.src.runner.generate_testlist")
    def test_run_tests_with_archive(
        self,
        mock_gen_testlist,
        mock_get_vk,
        mock_archive_manager,
        mock_os_replace,
        mock_shutil,
        mock_cpu,
        mock_gpu,
    ):
        mock_get_vk.return_value = "1002-73bf"
        # Update config to trigger archiving (needs runner_bin to be
        # deqp-runner or piglit)
        self.ctx.config["suites"]["suite1"]["runner"] = "deqp-runner"

        run_tests(self.ctx, ["test1"])

        mock_archive_manager.return_value.create_archive.assert_called()
        call_args = mock_archive_manager.return_value.create_archive.call_args
        target_path = call_args.kwargs["target_path"]
        # Archive should be created inside the testing output directory first
        self.assertTrue(
            str(target_path).startswith(str(self.ctx.runner_root / "testing"))
        )
        self.assertIn("suite1_1002-73bf", target_path.name)

        # Verify the runner logged that it will preserve the temporary archive
        self.ctx.console.info.assert_any_call(
            f"Preserving temporary archive at {target_path} for debugging"
        )

        # Verify we attempted to copy to the final dir and atomically replace
        mock_shutil.copy2.assert_called()
        copy_src, copy_dest = mock_shutil.copy2.call_args[0]
        self.assertEqual(copy_src, str(target_path))
        self.assertTrue(str(copy_dest).startswith(str(self.ctx.result_dir / "driver1")))

        # os.replace should have been used to move the temp file into place
        mock_os_replace.assert_called()
        replace_src, replace_dest = mock_os_replace.call_args[0]
        self.assertTrue(
            str(replace_src).startswith(str(self.ctx.result_dir / "driver1"))
        )
        self.assertTrue(
            str(replace_dest).startswith(str(self.ctx.result_dir / "driver1"))
        )

    @patch("gputest.src.runner.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.runner.os.cpu_count", return_value=4)
    @patch("gputest.src.runner.ArchiveManager")
    @patch("gputest.src.runner.get_gpu_id_from_vulkan")
    @patch("pathlib.Path.mkdir")
    @patch("gputest.src.runner.generate_testlist")
    def test_run_tests_with_hooks(
        self,
        mock_gen_testlist,
        mock_mkdir,
        mock_get_vk,
        mock_archive_manager,
        mock_cpu,
        mock_gpu,
    ):
        mock_get_vk.return_value = "1002-73bf"
        # Update config for hooks
        self.ctx.config["suites"]["suite1"]["runner"] = "deqp-runner"
        self.ctx.config["suites"]["suite1"]["pre_run_hooks"] = ["pre_hook"]
        self.ctx.config["suites"]["suite1"]["post_run_hooks"] = ["post_hook"]
        self.ctx.config["hooks"]["pre_hook"] = "echo pre"
        self.ctx.config["hooks"]["post_hook"] = "echo post"

        run_tests(self.ctx, ["test1"])

        # Verify hooks were run
        # We expect calls to runner.run:
        # 1. vulkaninfo (check=False)
        # 2. pre_hook
        # 3. test execution
        # 4. post_hook
        # 5. (archive is handled by ArchiveManager, not runner.run)

        calls = self.runner.run.call_args_list

        # Filter for hook calls (check=False)
        # Note: vulkaninfo is also check=False
        hook_calls = [c for c in calls if c[1].get("check") is False]

        # We expect at least 2 hook calls + 1 vulkaninfo call = 3
        # But vulkaninfo is mocked out via get_gpu_id_from_vulkan patch, so it won't call runner.run
        # Wait, I patched get_gpu_id_from_vulkan, so runner.run won't be called
        # for vulkaninfo.

        self.assertTrue(len(hook_calls) >= 2)

        pre_hook_cmd = hook_calls[0][0][0]
        self.assertIn("echo pre", pre_hook_cmd[2])

        post_hook_cmd = hook_calls[1][0][0]
        self.assertIn("echo post", post_hook_cmd[2])

    @patch("gputest.src.runner.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.runner.os.cpu_count", return_value=4)
    @patch("gputest.src.runner.ArchiveManager")
    @patch("gputest.src.runner.get_gpu_id_from_vulkan")
    @patch("pathlib.Path.mkdir")
    @patch("gputest.src.runner.generate_testlist")
    def test_run_tests_with_excludes(
        self,
        mock_gen_testlist,
        mock_mkdir,
        mock_get_vk,
        mock_archive_manager,
        mock_cpu,
        mock_gpu,
    ):
        mock_get_vk.return_value = "1002-73bf"
        # Update config for excludes
        self.ctx.config["suites"]["suite1"]["type"] = "deqp"
        self.ctx.config["suites"]["suite1"]["runner"] = "deqp-runner"
        self.ctx.config["suites"]["suite1"]["excludes"] = ["exclude1", "exclude2"]

        # Mock open to verify file writing
        with patch("builtins.open", unittest.mock.mock_open()) as mock_file:
            run_tests(self.ctx, ["test1"])

            # Verify exclude file was written
            mock_file.assert_called()
            handle = mock_file()
            handle.write.assert_any_call("exclude1\n")
            handle.write.assert_any_call("exclude2\n")

            # Verify runner command includes --exclude-list
            calls = self.runner.run.call_args_list
            # Find the test execution call
            test_call = [c for c in calls if "deqp-runner" in c[0][0]][0]
            cmd = test_call[0][0]
            self.assertIn("--exclude-list", cmd)

    @patch("gputest.src.runner.shutil.which")
    @patch("gputest.src.runner.SubprocessCommandRunner")
    def test_get_gpu_device_id(self, mock_runner_cls, mock_which):
        # Test fallback to lspci if vulkan/gl fail
        mock_which.side_effect = lambda x: "/usr/bin/" + x if x == "lspci" else None

        mock_instance = mock_runner_cls.return_value
        mock_instance.run.return_value.returncode = 0
        mock_instance.run.return_value.stdout = "03:00.0 0300: 1002:73bf (rev c1)"

        gpu_id = get_gpu_device_id()
        self.assertEqual(gpu_id, "1002:73bf")

    @patch("gputest.src.runner.shutil.which")
    @patch("gputest.src.runner.SubprocessCommandRunner")
    def test_get_gpu_id_from_vulkan(self, mock_runner_cls, mock_which):
        from gputest.src.runner import get_gpu_id_from_vulkan

        mock_which.return_value = "/usr/bin/vulkaninfo"

        mock_instance = mock_runner_cls.return_value
        mock_instance.run.return_value.returncode = 0
        mock_instance.run.return_value.stdout = """
        vendorID          = 0x1002
        deviceID          = 0x150e
"""
        gpu_id = get_gpu_id_from_vulkan()
        self.assertEqual(gpu_id, "1002-150e")

    @patch("gputest.src.runner.shutil.which")
    @patch("gputest.src.runner.SubprocessCommandRunner")
    def test_get_gpu_id_from_gl(self, mock_runner_cls, mock_which):
        from gputest.src.runner import get_gpu_id_from_gl

        mock_which.return_value = "/usr/bin/glxinfo"

        mock_instance = mock_runner_cls.return_value
        mock_instance.run.return_value.returncode = 0
        mock_instance.run.return_value.stdout = """
    Vendor: AMD (0x1002)
    Device: AMD Radeon 890M Graphics (radeonsi, gfx1150, LLVM 21.1.6, DRM 3.64, 6.18.0-2-default) (0x150e)
"""
        gpu_id = get_gpu_id_from_gl()
        self.assertEqual(gpu_id, "1002-150e")

    @patch("gputest.src.runner.ArchiveManager")
    @patch("gputest.src.runner.get_gpu_device_id")
    @patch("gputest.src.runner.datetime")
    @patch("gputest.src.runner.Path.mkdir")
    @patch("gputest.src.runner.get_gpu_id_from_vulkan", return_value=None)
    @patch("gputest.src.runner.get_gpu_id_from_gl", return_value=None)
    def test_run_tests_suite_hooks(
        self,
        mock_get_gl,
        mock_get_vk,
        mock_mkdir,
        mock_datetime,
        mock_get_gpu,
        mock_archive_manager,
    ):
        mock_get_gpu.return_value = "gpu1"
        mock_datetime.datetime.now.return_value.strftime.return_value = "20230101"

        # Configure suite hooks
        self.config["suites"]["suite1"]["runner"] = "deqp-runner"
        self.config["suites"]["suite1"]["pre_run_hooks"] = ["hook1"]
        self.config["suites"]["suite1"]["post_run_hooks"] = ["hook2"]
        self.config["hooks"]["hook1"] = "echo pre"
        self.config["hooks"]["hook2"] = "echo post"

        run_tests(self.ctx, ["test1"])

        # Verify hooks were run
        # We expect 3 runner calls: hook1, test execution, hook2
        # (plus maybe others if implementation details change, but at least these)

        # Check for hook1
        found_pre = False
        found_post = False

        for call in self.runner.run.call_args_list:
            args = call[0][0]
            if args[0] == "sh" and "echo pre" in args[2]:
                found_pre = True
            if args[0] == "sh" and "echo post" in args[2]:
                found_post = True

        self.assertTrue(found_pre, "Pre-run hook not found")
        self.assertTrue(found_post, "Post-run hook not found")

    @patch("gputest.src.runner.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.runner.os.cpu_count", return_value=4)
    @patch("gputest.src.runner.ArchiveManager")
    @patch("gputest.src.runner.get_gpu_id_from_vulkan")
    @patch("gputest.src.runner.shutil")
    @patch("gputest.src.runner.generate_testlist")
    def test_run_tests_with_archive_files(
        self,
        mock_gen_testlist,
        mock_shutil,
        mock_get_vk,
        mock_archive_manager,
        mock_cpu,
        mock_gpu,
    ):
        mock_get_vk.return_value = "1002-73bf"
        # Update config
        self.ctx.config["suites"]["suite1"]["runner"] = "deqp-runner"
        self.ctx.config["suites"]["suite1"]["archive_files"] = ["*.txt"]

        run_tests(self.ctx, ["test1"])

        mock_archive_manager.return_value.create_archive.assert_called()
        call_args = mock_archive_manager.return_value.create_archive.call_args
        artifact = call_args.kwargs["artifact"]

        # source_dir should be the staging dir
        self.assertIn(".archive_staging", str(artifact.source_dir))

        # Verify we attempted to move the temporary archive into result_dir
        mock_shutil.move.assert_called()
        move_src, move_dest = mock_shutil.move.call_args[0]
        self.assertTrue(str(move_src).startswith(str(self.ctx.runner_root / "testing")))
        self.assertTrue(str(move_dest).startswith(str(self.ctx.result_dir / "driver1")))
