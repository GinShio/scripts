"""
Cleanup logic.
"""
import shutil
import time
from pathlib import Path
from .context import Context


def run_cleanup(ctx: Context):
    """Cleanup old results."""
    archive_retention = ctx.config.get(
        "global", {}).get(
        "archive_retention_days", 360)
    result_retention = ctx.config.get(
        "global", {}).get(
        "result_retention_days", 16)

    ctx.console.info(
        f"Cleaning up archives older than {archive_retention} days")
    ctx.console.info(
        f"Cleaning up runtime results older than {result_retention} days")

    now = time.time()

    # Clean archives
    if ctx.result_dir.exists():
        for f in ctx.result_dir.glob("**/*.tar.zst"):
            if f.stat().st_mtime < (now - archive_retention * 86400):
                ctx.console.info(f"Deleting old archive: {f.name}")
                ctx.runner.run(["rm", str(f)], check=False)

        # Clean empty directories in result_dir
        if not ctx.console.dry_run:
            for p in ctx.result_dir.iterdir():
                if p.is_dir() and not any(p.iterdir()):
                    try:
                        p.rmdir()
                    except OSError:
                        pass

    # Clean runtime results (baseline and testing)
    # Structure:
    #   testing/<test_name>/<timestamp>
    #   baseline/<driver_name>/<suite_date>
    for subdir in ["testing", "baseline"]:
        p = ctx.runner_root / subdir
        if p.exists():
            # Iterate over grouping directories (test_name or driver_name)
            for group_dir in p.iterdir():
                if not group_dir.is_dir():
                    continue

                # Iterate over actual run directories
                for item in group_dir.iterdir():
                    if item.stat().st_mtime < (now - result_retention * 86400):
                        ctx.console.info(
                            f"Deleting old result: {subdir}/{group_dir.name}/{item.name}")
                        ctx.runner.run(["rm", "-rf", str(item)], check=False)

                # Clean up empty grouping directories
                if not ctx.console.dry_run and not any(group_dir.iterdir()):
                    try:
                        group_dir.rmdir()
                    except OSError:
                        pass
