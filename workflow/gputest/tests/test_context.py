
import unittest
from unittest.mock import patch
from io import StringIO
import sys
from gputest.src.context import DryRunCommandRunner, CommandResult


class TestDryRunCommandRunner(unittest.TestCase):
    def test_run(self):
        runner = DryRunCommandRunner()

        with patch("sys.stdout", new=StringIO()) as fake_out:
            result = runner.run(["echo", "hello"], env={"FOO": "BAR"})

            output = fake_out.getvalue()
            self.assertIn("[DRY] echo hello", output)
            self.assertIn("(env: {'FOO': 'BAR'})", output)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.command, ["echo", "hello"])
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

    def test_run_cwd(self):
        runner = DryRunCommandRunner()

        with patch("sys.stdout", new=StringIO()) as fake_out:
            runner.run(["ls"], cwd="/tmp")

            output = fake_out.getvalue()
            self.assertIn("[DRY] ls", output)
            self.assertIn("(cwd: /tmp)", output)
