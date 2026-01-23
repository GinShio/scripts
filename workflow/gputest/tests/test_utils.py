"""
Tests for gputest utilities.
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from gputest.src.utils import deep_merge, load_merged_config, resolve_env, substitute


class TestUtils(unittest.TestCase):
    def test_substitute(self):
        variables = {"foo": "bar", "baz": "qux"}
        self.assertEqual(substitute("Hello {{foo}}", variables), "Hello bar")
        self.assertEqual(substitute("{{foo}} {{baz}}", variables), "bar qux")
        self.assertEqual(substitute("No vars", variables), "No vars")

    def test_resolve_env(self):
        env = {"KEY": "{{foo}}", "PATH": "/usr/{{baz}}"}
        variables = {"foo": "bar", "baz": "bin"}
        resolved = resolve_env(env, variables)
        self.assertEqual(resolved["KEY"], "bar")
        self.assertEqual(resolved["PATH"], "/usr/bin")

    def test_deep_merge(self):
        target = {"a": 1, "b": {"c": 2, "d": 3}}
        source = {"b": {"c": 4, "e": 5}, "f": 6}
        expected = {"a": 1, "b": {"c": 4, "d": 3, "e": 5}, "f": 6}
        result = deep_merge(target, source)
        self.assertEqual(result, expected)
        self.assertEqual(target, expected)  # Should modify in place

    @patch("gputest.src.utils.load_config_file")
    def test_load_merged_config_file(self, mock_load):
        mock_load.return_value = {"foo": "bar"}
        path = MagicMock(spec=Path)
        path.is_dir.return_value = False

        config = load_merged_config(path)
        self.assertEqual(config, {"foo": "bar"})
        mock_load.assert_called_once_with(path)

    @patch("gputest.src.utils.load_config_file")
    def test_load_merged_config_dir(self, mock_load):
        path = MagicMock(spec=Path)
        path.is_dir.return_value = True

        file1 = MagicMock(spec=Path)
        file2 = MagicMock(spec=Path)

        # Make mocks comparable for sorted()
        file1.__lt__ = lambda self, other: True
        file2.__lt__ = lambda self, other: False

        # Sort order matters, so we mock glob to return list
        path.glob.return_value = [file1, file2]

        mock_load.side_effect = [{"a": 1}, {"b": 2}]

        config = load_merged_config(path)
        self.assertEqual(config, {"a": 1, "b": 2})
        self.assertEqual(mock_load.call_count, 2)
        mock_load.assert_has_calls([call(file1), call(file2)])
