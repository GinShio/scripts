"""Archive management utilities reusable across projects."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
import bz2
import gzip
import lzma
import math
import os
import shutil
import sys
import tarfile
import tempfile
import zipfile

import zstandard as zstd

_SUFFIX_FORMATS: list[tuple[str, str]] = [
    (".tar.zst", "zst"),
    (".tzst", "zst"),
    (".tar.gz", "gztar"),
    (".tgz", "gztar"),
    (".tar.bz2", "bztar"),
    (".tbz", "bztar"),
    (".tar.xz", "xztar"),
    (".txz", "xztar"),
    (".tar", "tar"),
    (".zip", "zip"),
]

_FORMAT_ALIASES: dict[str, str] = {
    "zst": "zst",
    "tar.zst": "zst",
    "tzst": "zst",
    "gztar": "gztar",
    "gz": "gztar",
    "tar.gz": "gztar",
    "tgz": "gztar",
    "bztar": "bztar",
    "bz2": "bztar",
    "tar.bz2": "bztar",
    "tbz": "bztar",
    "xztar": "xztar",
    "xz": "xztar",
    "tar.xz": "xztar",
    "txz": "xztar",
    "tar": "tar",
    "zip": "zip",
}


@runtime_checkable
class ArchiveConsole(Protocol):
    """Minimal console interface required by :class:`ArchiveManager`."""

    dry_run: bool

    def info(self, message: str) -> None:
        ...

    def error(self, message: str) -> None:
        ...

    def dry(self, message: str) -> None:
        ...


@dataclass(slots=True)
class ArchiveArtifact:
    """Description of filesystem content to package into an archive."""

    source_dir: Path
    label: str | None = None


class ArchiveManager:
    """Create compressed archives from directories."""

    def __init__(
        self,
        console: ArchiveConsole,
    ) -> None:
        self._console = console

    @staticmethod
    def _available_memory_bytes() -> int:
        try:
            if hasattr(os, "sysconf"):
                page_size = os.sysconf("SC_PAGE_SIZE")
                phys_pages = os.sysconf("SC_PHYS_PAGES")
                if isinstance(
                        page_size,
                        int) and isinstance(
                        phys_pages,
                        int) and page_size > 0 and phys_pages > 0:
                    return page_size * phys_pages
        except (ValueError, OSError, AttributeError):
            pass

        if sys.platform.startswith("linux"):
            try:
                with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                    mem_total = None
                    mem_available = None
                    for line in handle:
                        if line.startswith("MemAvailable:"):
                            mem_available = int(line.split()[1]) * 1024
                        elif line.startswith("MemTotal:"):
                            mem_total = int(line.split()[1]) * 1024
                        if mem_available is not None and mem_total is not None:
                            break
                    if mem_available is not None:
                        return mem_available
                    if mem_total is not None:
                        return mem_total
            except (OSError, ValueError):
                pass

        try:
            import psutil  # type: ignore

            return getattr(psutil, "virtual_memory")().available
        except Exception:  # pragma: no cover - optional dependency
            return 0

    @staticmethod
    def _zstd_thread_count(source_size: int) -> int:
        cpu_count = os.cpu_count() or 1
        if cpu_count <= 1:
            return 1

        size = max(1, source_size)
        size_mb = size / (1024 * 1024)

        desired = 1
        if size_mb >= 32:
            desired = 2
        if size_mb >= 256:
            desired = 4
        if size_mb >= 1024:
            desired = 8
        if size_mb >= 4096:
            desired = 12
        if size_mb >= 16384:
            desired = 16

        desired = min(desired, cpu_count)

        max_by_work = max(1, size // (32 * 1024 * 1024))
        desired = min(desired, max_by_work)

        return max(1, desired)

    @classmethod
    def _zstd_window_log(cls, source_size: int) -> int:
        if source_size <= 0:
            base_log = 10
        else:
            base_log = max(10, min(29, (source_size - 1).bit_length()))

        mem_bytes = cls._available_memory_bytes()
        if mem_bytes > 0:
            max_window_bytes = min(1 << 29, max(1 << 20, mem_bytes // 4))
            max_window_log = int(math.floor(math.log2(max_window_bytes)))
            base_log = min(base_log, max_window_log)

        return max(10, min(base_log, 29))

    @classmethod
    def _zstd_compression_params(
            cls, source_size: int) -> zstd.ZstdCompressionParameters:
        size = max(1, source_size)
        window_log = cls._zstd_window_log(size)
        threads = cls._zstd_thread_count(size)
        enable_ldm = size >= 32 * 1024 * 1024 and window_log >= 23

        mem_bytes = cls._available_memory_bytes()
        if mem_bytes > 0:
            budget = max(mem_bytes // 2, 1)
            per_thread_bytes = 1 << window_log
            estimated_thread = per_thread_bytes * 2
            max_threads_by_mem = max(1, budget // max(estimated_thread, 1))
            if max_threads_by_mem < threads:
                threads = max_threads_by_mem
            while threads > 0 and estimated_thread * threads > budget and window_log > 10:
                window_log -= 1
                per_thread_bytes = 1 << window_log
                estimated_thread = per_thread_bytes * 2
                max_threads_by_mem = max(1, budget // max(estimated_thread, 1))
                threads = min(threads, max_threads_by_mem)
            if threads == 0:
                threads = 1
            if estimated_thread * threads > budget:
                threads = 1

        if enable_ldm and window_log < 23:
            enable_ldm = False

        params_kwargs: dict[str, Any] = {
            "compression_level": 22,
            "threads": threads,
            "write_checksum": True,
            "write_content_size": True,
            "enable_ldm": enable_ldm,
            "window_log": window_log,
        }

        job_size = 0
        if threads > 1:
            if threads <= 2:
                base_job = 4 * 1024 * 1024
            elif threads <= 4:
                base_job = 8 * 1024 * 1024
            elif threads <= 8:
                base_job = 16 * 1024 * 1024
            else:
                base_job = 32 * 1024 * 1024

            upper_bound = max(1 << 20, size // threads)
            job_size = max(1 << 20, min(base_job, upper_bound))

        if job_size and threads > 1:
            params_kwargs["job_size"] = job_size

        if enable_ldm:
            params_kwargs.update(
                {
                    "ldm_min_match": 64 if size < 512 * 1024 * 1024 else 128,
                    "ldm_hash_rate_log": 7,
                    "ldm_hash_log": 9,
                    "ldm_bucket_size_log": 3,
                }
            )

        return zstd.ZstdCompressionParameters(**params_kwargs)

    @classmethod
    def _xz_dict_size(cls, input_size: int) -> int:
        if input_size <= 0:
            input_size = 1

        min_power = 16  # 64 KiB
        max_power = 29  # 512 MiB

        mem_bytes = cls._available_memory_bytes()
        if mem_bytes > 0:
            max_dict_bytes = min(1 << max_power, max(1 << 20, mem_bytes // 6))
            max_power = max(
                min_power, int(
                    math.floor(
                        math.log2(max_dict_bytes))))

        target_power = max(
            min_power, min(
                max_power, (input_size - 1).bit_length()))
        return 1 << target_power

    @classmethod
    def _xz_filters(cls, input_size: int) -> list[dict[str, int]]:
        dict_size = cls._xz_dict_size(input_size)
        return [
            {
                "id": lzma.FILTER_LZMA2,
                "dict_size": dict_size,
                "lc": 3,
                "lp": 0,
                "pb": 2,
                "mode": lzma.MODE_NORMAL,
                "mf": lzma.MF_BT4,
            }
        ]

    def create_archive(
        self,
        *,
        artifact: ArchiveArtifact,
        target_path: Path | str,
        format_hint: str | None = None,
        overwrite: bool = True,
    ) -> Path:
        """Create an archive for *artifact* at *target_path*.

        Parameters
        ----------
        artifact:
            Data describing the directory to archive.
        target_path:
            Exact path (including filename) for the archive that should be created.
        format_hint:
            Optional explicit archive format such as ``"zst"`` or ``"zip"``. When
            omitted, the format is inferred from *target_path*'s suffix.
        overwrite:
            When ``False`` and the target already exists, a :class:`FileExistsError`
            is raised instead of replacing the file.
        """

        target = Path(target_path).expanduser()
        source_dir = Path(artifact.source_dir).expanduser()

        if not source_dir.exists():
            raise FileNotFoundError(
                f"Archive source directory '{source_dir}' does not exist")

        archive_format = self._resolve_archive_format(
            target=target, format_hint=format_hint)

        if self._console.dry_run:
            label = artifact.label or source_dir.name
            self._emit_dry(f"Would archive {label} to {target}")
            return target

        if target.exists() and not overwrite:
            raise FileExistsError(f"Archive target '{target}' already exists")

        target.parent.mkdir(parents=True, exist_ok=True)

        return self._make_archive(
            target_path=target,
            archive_format=archive_format,
            source_dir=source_dir,
        )

    def _resolve_archive_format(
            self,
            *,
            target: Path,
            format_hint: str | None) -> str:
        if format_hint:
            normalized = format_hint.strip().lower()
            if normalized in _FORMAT_ALIASES:
                return _FORMAT_ALIASES[normalized]
            raise ValueError(
                f"Unsupported archive format hint '{format_hint}'")

        filename = target.name.lower()
        for suffix, fmt in sorted(
            _SUFFIX_FORMATS, key=lambda item: len(
                item[0]), reverse=True):
            if filename.endswith(suffix):
                return fmt

        raise ValueError(
            "Unable to determine archive format from target path. "
            "Provide an explicit format_hint or use a supported suffix."
        )

    def _emit_dry(self, message: str) -> None:
        dry_method = getattr(self._console, "dry", None)
        if callable(dry_method):
            dry_method(message)
            return
        if getattr(self._console, "dry_run", False):
            self._console.info(f"[dry-run] {message}")

    def _make_archive(
        self,
        *,
        target_path: Path,
        archive_format: str,
        source_dir: Path,
    ) -> Path:
        if archive_format == "zst":
            return self._make_zst_archive(
                target_path=target_path,
                source_dir=source_dir)
        if archive_format == "gztar":
            return self._make_gzip_archive(
                target_path=target_path,
                source_dir=source_dir)
        if archive_format == "bztar":
            return self._make_bz2_archive(
                target_path=target_path,
                source_dir=source_dir)
        if archive_format == "xztar":
            return self._make_xz_archive(
                target_path=target_path,
                source_dir=source_dir)
        if archive_format == "tar":
            return self._make_tar_archive(
                target_path=target_path,
                source_dir=source_dir)
        if archive_format == "zip":
            return self._make_zip_archive(
                target_path=target_path,
                source_dir=source_dir)

        raise RuntimeError(f"Unsupported archive format '{archive_format}'")

    def _make_zst_archive(
        self,
        *,
        target_path: Path,
        source_dir: Path,
    ) -> Path:
        temp_tar = self._create_pax_tar(
            root_dir=source_dir,
            temp_dir=target_path.parent)

        try:
            params = self._zstd_compression_params(temp_tar.stat().st_size)
            compressor = zstd.ZstdCompressor(compression_params=params)
            with temp_tar.open("rb") as src, target_path.open("wb") as dst:
                compressor.copy_stream(src, dst)
        finally:
            temp_tar.unlink(missing_ok=True)

        return target_path

    def _make_gzip_archive(
        self,
        *,
        target_path: Path,
        source_dir: Path,
    ) -> Path:
        temp_tar = self._create_pax_tar(
            root_dir=source_dir,
            temp_dir=target_path.parent)

        try:
            with temp_tar.open("rb") as src, gzip.open(target_path, "wb", compresslevel=9, mtime=0) as dst:
                shutil.copyfileobj(src, dst)
        finally:
            temp_tar.unlink(missing_ok=True)

        return target_path

    def _make_bz2_archive(
        self,
        *,
        target_path: Path,
        source_dir: Path,
    ) -> Path:
        temp_tar = self._create_pax_tar(
            root_dir=source_dir,
            temp_dir=target_path.parent)

        try:
            with temp_tar.open("rb") as src, bz2.open(target_path, "wb", compresslevel=9) as dst:
                shutil.copyfileobj(src, dst)
        finally:
            temp_tar.unlink(missing_ok=True)

        return target_path

    def _make_xz_archive(
        self,
        *,
        target_path: Path,
        source_dir: Path,
    ) -> Path:
        temp_tar = self._create_pax_tar(
            root_dir=source_dir,
            temp_dir=target_path.parent)

        try:
            filters = self._xz_filters(temp_tar.stat().st_size)
            compressor = lzma.LZMACompressor(
                format=lzma.FORMAT_XZ,
                check=lzma.CHECK_CRC64,
                filters=filters,
            )
            with temp_tar.open("rb") as src, target_path.open("wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    data = compressor.compress(chunk)
                    if data:
                        dst.write(data)

                flushed = compressor.flush()
                if flushed:
                    dst.write(flushed)
        finally:
            temp_tar.unlink(missing_ok=True)

        return target_path

    def _make_tar_archive(
        self,
        *,
        target_path: Path,
        source_dir: Path,
    ) -> Path:
        temp_tar = self._create_pax_tar(
            root_dir=source_dir,
            temp_dir=target_path.parent)

        try:
            temp_tar.rename(target_path)
        except OSError:
            shutil.move(str(temp_tar), str(target_path))
        return target_path

    def _make_zip_archive(
        self,
        *,
        target_path: Path,
        source_dir: Path,
    ) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(
            target_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
            strict_timestamps=False,
        ) as archive:
            root_dir_path = Path(source_dir)
            for dirpath, dirnames, filenames in os.walk(
                    source_dir, topdown=True):
                dirnames.sort()
                filenames.sort()

                current_dir = Path(dirpath)
                relative_dir = current_dir.relative_to(root_dir_path)

                for filename in filenames:
                    file_path = current_dir / filename
                    if relative_dir != Path("."):
                        arcname_path = relative_dir / filename
                    else:
                        arcname_path = Path(filename)

                    archive.write(file_path, arcname_path.as_posix())

        return target_path

    def _create_pax_tar(
            self,
            *,
            root_dir: Path,
            temp_dir: Path) -> Path:
        with tempfile.NamedTemporaryFile(dir=temp_dir, suffix=".tar", delete=False) as temp_handle:
            temp_path = Path(temp_handle.name)

        try:
            with tarfile.open(temp_path, mode="w", format=tarfile.PAX_FORMAT) as tar:
                # Add contents of root_dir to archive root
                for item in root_dir.iterdir():
                    tar.add(item, arcname=item.name)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        return temp_path

    def extract_archive(
        self,
        *,
        archive_path: Path | str,
        destination_dir: Path | str,
        format_hint: str | None = None,
    ) -> None:
        """Extract an archive to a destination directory.

        Parameters
        ----------
        archive_path:
            Path to the archive file.
        destination_dir:
            Directory where contents should be extracted.
        format_hint:
            Optional explicit archive format.
        """
        archive = Path(archive_path).expanduser()
        dest = Path(destination_dir).expanduser()

        if not archive.exists():
            raise FileNotFoundError(f"Archive '{archive}' does not exist")

        if self._console.dry_run:
            self._emit_dry(f"Would extract {archive} to {dest}")
            return

        dest.mkdir(parents=True, exist_ok=True)
        archive_format = self._resolve_archive_format(
            target=archive, format_hint=format_hint)

        if archive_format == "zst":
            self._extract_zst(archive, dest)
        elif archive_format == "gztar":
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(path=dest)
        elif archive_format == "bztar":
            with tarfile.open(archive, "r:bz2") as tar:
                tar.extractall(path=dest)
        elif archive_format == "xztar":
            with tarfile.open(archive, "r:xz") as tar:
                tar.extractall(path=dest)
        elif archive_format == "tar":
            with tarfile.open(archive, "r:") as tar:
                tar.extractall(path=dest)
        elif archive_format == "zip":
            with zipfile.ZipFile(archive, "r") as zip_ref:
                zip_ref.extractall(dest)
        else:
            raise ValueError(f"Unsupported archive format: {archive_format}")

        self._console.info(f"Extracted {archive} to {dest}")

    def _extract_zst(self, archive: Path, dest: Path) -> None:
        dctx = zstd.ZstdDecompressor()
        with archive.open("rb") as ifh:
            with dctx.stream_reader(ifh) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    tar.extractall(path=dest)


__all__ = [
    "ArchiveConsole",
    "ArchiveManager",
    "ArchiveArtifact",
]
