"""
Restore logic.
"""

import time
from pathlib import Path

from core.archive import ArchiveManager
from core.command_runner import CommandError

from .context import Context
from .runner import get_gpu_device_id


def run_restore(ctx: Context, days: int = 10):
    """Restore baseline results."""
    gpu_id = get_gpu_device_id()
    ctx.console.info(f"Restoring results for GPU: {gpu_id} (last {days} days)")

    if not ctx.result_dir.exists():
        ctx.console.error(f"Result directory not found: {ctx.result_dir}")
        return

    baseline_dir = ctx.runner_root / "baseline"
    ctx.runner.run(["mkdir", "-p", str(baseline_dir)], check=False)

    now = time.time()
    cutoff = now - (days * 86400)

    # Pattern: vendor/suite_device_date.arch -> */*_{gpu_id}_*.tar.zst
    files = set(ctx.result_dir.glob(f"*/*_{gpu_id}_*.tar.zst"))

    # Also include lvp results (10005:0000)
    files.update(ctx.result_dir.glob("*/*_10005:0000_*.tar.zst"))

    archive_manager = ArchiveManager(ctx.console)

    for archive in files:
        if archive.stat().st_mtime > cutoff:
            ctx.console.info(f"Restoring {archive.name}")

            # Extract to baseline/driver/suite_date
            # e.g. deqp-vk_1002:150e_20260101.tar.zst -> baseline/radv-local/deqp-vk_20260101

            driver_name = archive.parent.name

            stem = archive.name
            while "." in stem:
                stem = Path(stem).stem

            # Parse filename: {suite}_{gpu_id}_{date}
            # We split by '_' from the right
            parts = stem.split("_")
            if len(parts) >= 3:
                # date is last, gpu_id is second to last
                # suite is everything before
                date_str = parts[-1]
                # gpu_id_str = parts[-2] # Unused
                suite_name = "_".join(parts[:-2])
                clean_stem = f"{suite_name}_{date_str}"
            else:
                # Fallback for unexpected format
                clean_stem = stem

            target_dir = baseline_dir / driver_name / clean_stem

            try:
                archive_manager.extract_archive(
                    archive_path=archive, destination_dir=target_dir
                )
            except Exception as e:
                ctx.console.error(f"Failed to extract {archive}: {e}")
