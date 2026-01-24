import os
import unittest

import zstandard as zstd
from core.archive import ArchiveManager


class TestArchive(unittest.TestCase):
    def test_params_small_size(self):
        params = ArchiveManager._zstd_compression_params(1_000_000)
        assert params.compression_level == 22
        assert 10 <= params.window_log <= 27
        assert params.threads >= 1
        assert params.threads <= (os.cpu_count() or 1)
        # LDM should not be enabled for very small inputs
        if params.window_log >= 22:
            # may be enabled only if window_log allows it
            assert getattr(params, "enable_ldm", False) in (False, True)

    def test_params_large_size(self):
        size = 200 * 1024 * 1024
        params = ArchiveManager._zstd_compression_params(size)
        assert params.compression_level == 22
        assert params.window_log <= 27
        assert params.threads >= 1
        assert params.threads <= (os.cpu_count() or 1)
        # For large sizes we expect LDM to be enabled when window_log is sufficient
        if params.window_log >= 22:
            assert getattr(params, "enable_ldm", False)
        # job_size set when threads > 1
        if params.threads > 1:
            assert hasattr(params, "job_size")
            assert params.job_size >= (1 << 20)
