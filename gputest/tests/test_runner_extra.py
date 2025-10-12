
import unittest
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path
import shutil
import tempfile
import os
from gputest.src.runner import (
    get_gpu_id_from_vulkan,
    get_gpu_id_from_gl,
    generate_testlist,
    run_tests
)
from gputest.src.context import Context, Console
from core.command_runner import CommandResult

class TestRunnerExtra(unittest.TestCase):
    def setUp(self):
        self.console = MagicMock(spec=Console)
        self.console.dry_run = False
        self.runner = MagicMock()
        self.config = {
            "tests": {},
            "drivers": {},
            "layouts": {},
            "suites": {},
            "backends": {},
            "hooks": {}
        }
        self.ctx = Context(
            config=self.config,
            console=self.console,
            runner=self.runner,
            project_root=Path("/project"),
            runner_root=Path("/runner"),
            result_dir=Path("/result")
        )

    @patch("gputest.src.runner.shutil.which")
    @patch("gputest.src.runner.SubprocessCommandRunner")
    def test_get_gpu_id_from_vulkan(self, mock_runner_cls, mock_which):
        mock_which.return_value = "/usr/bin/vulkaninfo"
        mock_runner_instance = mock_runner_cls.return_value

        # Mock output
        stdout = """
GPU0:
    apiVersion         = 1.2.131
    driverVersion      = 83886082
    vendorID           = 0x1002
    deviceID           = 0x73bf
    deviceType         = DISCRETE_GPU
    deviceName         = AMD Radeon RX 6800 XT
        """
        mock_runner_instance.run.return_value = CommandResult(
            command=[], returncode=0, stdout=stdout, stderr="", streamed=False
        )

        gpu_id = get_gpu_id_from_vulkan()
        self.assertEqual(gpu_id, "1002:73bf")

    @patch("gputest.src.runner.shutil.which")
    @patch("gputest.src.runner.SubprocessCommandRunner")
    def test_get_gpu_id_from_gl(self, mock_runner_cls, mock_which):
        mock_which.return_value = "/usr/bin/glxinfo"
        mock_runner_instance = mock_runner_cls.return_value

        # Mock output
        stdout = """
name of display: :0
display: :0  screen: 0
direct rendering: Yes
Extended renderer info (GLX_MESA_query_renderer):
    Vendor: AMD (0x1002)
    Device: AMD Radeon RX 6800 XT (RADV NAVI21) (0x73bf)
    Version: 21.1.0
    Accelerated: yes
    Video memory: 16384MB
    Unified memory: no
    Preferred profile: core (0x1)
    Max core profile version: 4.6
    Max compat profile version: 4.6
    Max GLES1 profile version: 1.1
    Max GLES[23] profile version: 3.2
        """
        mock_runner_instance.run.return_value = CommandResult(
            command=[], returncode=0, stdout=stdout, stderr="", streamed=False
        )

        gpu_id = get_gpu_id_from_gl()
        self.assertEqual(gpu_id, "1002:73bf")

    def test_generate_testlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            # Create caselists
            cl1 = tmp_path / "cl1.txt"
            cl1.write_text("test1\ntest2\n")

            cl2 = tmp_path / "cl2.txt"
            cl2.write_text("test3\n")

            output_dir = tmp_path / "output"

            generate_testlist(self.ctx, output_dir, [cl1, cl2])

            testlist = output_dir / "testlist.txt"
            self.assertTrue(testlist.exists())
            content = testlist.read_text()
            lines = content.splitlines()

            self.assertIn("test1", lines)
            self.assertIn("test2", lines)
            self.assertIn("test3", lines)

            # Ensure no empty lines
            self.assertFalse("" in lines)
            self.assertEqual(len(lines), 3)

    @patch("gputest.src.runner.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.runner.os.cpu_count", return_value=4)
    def test_run_tests_piglit(self, mock_cpu, mock_gpu):
        self.config["tests"]["test_piglit"] = {
            "driver": "driver1",
            "suite": "suite_piglit"
        }
        self.config["drivers"]["driver1"] = {
            "layout": "layout1"
        }
        self.config["layouts"]["layout1"] = {
            "env": {}
        }
        self.config["suites"]["suite_piglit"] = {
            "type": "piglit", # Explicitly set type to trigger piglit template
            "runner": "piglit-runner",
            "exe": "piglit",
            "deqp_args": ["summary", "console"] # Use deqp_args for args after --
        }

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.mkdir"), \
             patch("os.access", return_value=True):

            run_tests(self.ctx, ["test_piglit"])

            self.runner.run.assert_called()
            cmd = self.runner.run.call_args[0][0]
            self.assertTrue(any("piglit" in arg for arg in cmd))
            self.assertIn("run", cmd)
            self.assertIn("--piglit-folder", cmd)
            self.assertIn("summary", cmd)
            self.assertIn("console", cmd)

    @patch("gputest.src.runner.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.runner.os.cpu_count", return_value=4)
    @patch("gputest.src.runner.generate_testlist")
    def test_run_tests_excludes(self, mock_gen, mock_cpu, mock_gpu):
        self.config["tests"]["test_deqp"] = {
            "driver": "driver1",
            "suite": "suite_deqp"
        }
        self.config["drivers"]["driver1"] = {
            "layout": "layout1"
        }
        self.config["layouts"]["layout1"] = {
            "env": {}
        }
        self.config["suites"]["suite_deqp"] = {
            "type": "deqp",
            "runner": "deqp-runner",
            "exe": "deqp-vk",
            "excludes": ["exclude1", "exclude2"]
        }

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.mkdir"), \
             patch("os.access", return_value=True), \
             patch("builtins.open", mock_open()) as mock_file:

            run_tests(self.ctx, ["test_deqp"])

            # Verify exclude file writing
            mock_file.assert_called()
            handle = mock_file()
            handle.write.assert_any_call("exclude1\n")
            handle.write.assert_any_call("exclude2\n")

            # Verify command includes exclude-list
            self.runner.run.assert_called()
            cmd = self.runner.run.call_args[0][0]
            self.assertIn("--exclude-list", cmd)

    @patch("gputest.src.runner.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.runner.os.cpu_count", return_value=4)
    def test_run_tests_hooks(self, mock_cpu, mock_gpu):
        self.config["tests"]["test_hooks"] = {
            "driver": "driver1",
            "suite": "suite_hooks",
            "pre_run": ["hook1"],
            "post_run": ["hook2"]
        }
        self.config["drivers"]["driver1"] = {
            "layout": "layout1"
        }
        self.config["layouts"]["layout1"] = {
            "env": {}
        }
        self.config["suites"]["suite_hooks"] = {
            "runner": "deqp-runner", # Triggers hook logic
            "exe": "exe"
        }
        self.config["hooks"] = {
            "hook1": "echo pre",
            "hook2": "echo post"
        }

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.mkdir"), \
             patch("os.access", return_value=True), \
             patch("gputest.src.runner.ArchiveManager"):

            run_tests(self.ctx, ["test_hooks"])

            # Check calls
            # 1. pre-hook
            # 2. test run
            # 3. post-hook
            # 4. archive (maybe)

            calls = self.runner.run.call_args_list

            # Filter for shell calls (hooks)
            shell_calls = [c for c in calls if c[0][0][0] == "sh"]
            self.assertEqual(len(shell_calls), 2)
            self.assertIn("echo pre", shell_calls[0][0][0][2])
            self.assertIn("echo post", shell_calls[1][0][0][2])

    @patch("gputest.src.runner.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.runner.os.cpu_count", return_value=4)
    @patch("gputest.src.runner.ArchiveManager")
    @patch("gputest.src.runner.datetime")
    def test_run_tests_archive_files(self, mock_datetime, mock_archive_mgr, mock_cpu, mock_gpu):
        mock_datetime.datetime.now.return_value.strftime.return_value = "20260101-120000"

        self.config["tests"]["test_arch"] = {
            "driver": "driver1",
            "suite": "suite_arch"
        }
        self.config["drivers"]["driver1"] = {
            "layout": "layout1"
        }
        self.config["layouts"]["layout1"] = {
            "env": {}
        }
        self.config["suites"]["suite_arch"] = {
            "runner": "deqp-runner",
            "exe": "exe",
            "archive_files": ["*.log"]
        }

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.mkdir"), \
             patch("os.access", return_value=True), \
             patch("shutil.copy2") as mock_copy, \
             patch("shutil.rmtree"):

            # Mock glob to return a file
            mock_path = MagicMock()
            mock_path.glob.return_value = [Path("/runner/testing/test_arch/date/test.log")]

            # We need to mock the output_dir path object which is created inside run_tests
            # This is hard because it's created dynamically.
            # However, we can mock Path.glob globally or on the instance if we could intercept it.
            # Since we can't easily intercept the specific instance, we rely on the fact that
            # run_tests calls output_dir.glob(pattern).

            # Let's use a simpler approach: mock Path.glob
            mock_match = MagicMock(spec=Path)
            mock_match.is_file.return_value = True
            mock_match.relative_to.return_value = Path("test.log")
            mock_match.name = "test.log"
            # We need to ensure str(mock_match) returns something valid if used in logging
            mock_match.__str__.return_value = "/runner/testing/test_arch/20260101-120000/test.log"

            with patch("pathlib.Path.glob", return_value=[mock_match]) as mock_glob:
                run_tests(self.ctx, ["test_arch"])

                mock_glob.assert_called()
                mock_copy.assert_called()

                # Verify ArchiveArtifact creation
                mock_archive_mgr.return_value.create_archive.assert_called()
                call_args = mock_archive_mgr.return_value.create_archive.call_args
                artifact = call_args.kwargs['artifact']
                # Source dir should be the staging dir
                self.assertEqual(artifact.source_dir.name, ".archive_staging")

    @patch("gputest.src.runner.get_gpu_device_id", return_value="gpu1")
    @patch("gputest.src.runner.os.cpu_count", return_value=4)
    @patch("gputest.src.runner.generate_testlist")
    def test_run_tests_deqp_caselists(self, mock_gen, mock_cpu, mock_gpu):
        self.config["tests"]["test_deqp_cl"] = {
            "driver": "driver1",
            "suite": "suite_deqp_cl"
        }
        self.config["drivers"]["driver1"] = {
            "layout": "layout1"
        }
        self.config["layouts"]["layout1"] = {
            "env": {}
        }
        self.config["suites"]["suite_deqp_cl"] = {
            "type": "deqp",
            "runner": "deqp-runner",
            "exe": "deqp-vk",
            "caselists": ["cl1.txt", "cl_*.txt"]
        }

        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.mkdir"), \
             patch("os.access", return_value=True), \
             patch("glob.glob") as mock_glob:

            # Mock glob for cl_*.txt
            # The code calls glob.glob(str(cl_path))
            # cl_path will be /runner/deqp/cl_*.txt
            mock_glob.return_value = ["/runner/deqp/cl_2.txt", "/runner/deqp/cl_3.txt"]

            run_tests(self.ctx, ["test_deqp_cl"])

            self.runner.run.assert_called()
            cmd = self.runner.run.call_args[0][0]

            # Check caselists in command
            # In the new implementation, we generate a single testlist.txt and pass it
            self.assertIn("--caselist", cmd)
            # The path will be the generated testlist.txt in output dir
            # We can't easily predict the timestamp, but we can check for testlist.txt
            self.assertTrue(any("testlist.txt" in arg for arg in cmd))

            # Verify generate_testlist called with resolved list
            mock_gen.assert_called()
            args = mock_gen.call_args[0]
            caselists = args[2]
            self.assertEqual(len(caselists), 3)
            self.assertEqual(caselists[0], Path("/runner/deqp/cl1.txt"))
            self.assertEqual(caselists[1], Path("/runner/deqp/cl_2.txt"))
            self.assertEqual(caselists[2], Path("/runner/deqp/cl_3.txt"))
