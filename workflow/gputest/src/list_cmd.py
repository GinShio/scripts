"""
List command implementation.
"""

import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .context import Context
from .utils import resolve_env, substitute


def run_list(ctx: Context, target: str, name: str = None):
    """List drivers or suites."""
    if target in ["drivers", "driver"]:
        _list_drivers(ctx, name)
    elif target in ["suites", "suite"]:
        _list_suites(ctx, name)
    else:
        ctx.console.error(f"Unknown target: {target}. Use 'drivers' or 'suites'.")


def _list_drivers(ctx: Context, name: str = None):
    drivers_cfg = ctx.config.get("drivers", {})
    layouts_cfg = ctx.config.get("layouts", {})

    # Common variables
    base_vars = {
        "project_root": str(ctx.project_root),
        "runner_root": str(ctx.runner_root),
        "home": str(Path.home()),
        "date": datetime.datetime.now().strftime("%Y%m%d"),
        "gpu_id": "unknown_gpu",
    }

    targets = [name] if name else sorted(drivers_cfg.keys())

    for driver_name in targets:
        if driver_name not in drivers_cfg:
            if name:
                ctx.console.error(f"Driver '{driver_name}' not found.")
            continue

        driver = drivers_cfg[driver_name]
        layout_name = driver.get("layout")
        layout = layouts_cfg.get(layout_name)

        if not layout:
            ctx.console.error(
                f"Layout '{layout_name}' not found for driver '{driver_name}'."
            )
            continue

        # Build Environment
        layout_root = substitute(driver.get("root", "/usr"), base_vars)
        env_vars = base_vars.copy()
        env_vars["root"] = layout_root

        # Inject names
        env_vars.update(
            {
                "driver_name": driver_name,
                "layout_name": layout_name,
            }
        )

        # Resolve Env
        merged_env = os.environ.copy()

        # Layout Env
        layout_env = resolve_env(layout.get("env", {}), env_vars)
        merged_env.update(layout_env)

        # Driver Env
        driver_env = resolve_env(driver.get("env", {}), env_vars)
        merged_env.update(driver_env)

        print(f"Driver: {driver_name}")
        print(f"  Root: {layout_root}")

        # Try to find ICD
        icd_files = []

        # 1. Check Env
        env_icd = merged_env.get("VK_ICD_FILENAMES") or merged_env.get(
            "VK_DRIVER_FILES"
        )
        if env_icd:
            icd_files.extend(env_icd.split(os.pathsep))
        else:
            # 2. Search in root
            search_paths = [
                Path(layout_root) / "share/vulkan/icd.d",
                Path(layout_root) / "etc/vulkan/icd.d",
            ]
            for sp in search_paths:
                if sp.exists():
                    # Convert Path objects to strings for consistency
                    icd_files.extend([str(p) for p in sorted(sp.glob("*.json"))])

        if icd_files:
            for icd_path in icd_files:
                print(f"  ICD: {icd_path}")
                try:
                    p = Path(icd_path)
                    if p.exists():
                        with open(p, "r") as f:
                            data = json.load(f)

                        icd_data = data.get("ICD", {})
                        lib_path = icd_data.get("library_path")

                        if lib_path:
                            real_lib_path = Path(lib_path)
                            if not real_lib_path.is_absolute():
                                real_lib_path = p.parent / real_lib_path

                            # Try to resolve, but handle if file doesn't exist
                            try:
                                resolved_path = real_lib_path.resolve()
                                print(f"  Library: {resolved_path}")
                            except Exception:
                                print(f"  Library: {real_lib_path} (not found)")
                except Exception:
                    pass

        if "LIBGL_DRIVERS_PATH" in merged_env:
            print(f"  LIBGL_DRIVERS_PATH: {merged_env['LIBGL_DRIVERS_PATH']}")

        if "LD_LIBRARY_PATH" in merged_env:
            print(f"  LD_LIBRARY_PATH: {merged_env['LD_LIBRARY_PATH']}")

        print("")


def _list_suites(ctx: Context, name: str = None):
    suites_cfg = ctx.config.get("suites", {})

    # Common variables
    base_vars = {
        "project_root": str(ctx.project_root),
        "runner_root": str(ctx.runner_root),
        "home": str(Path.home()),
        "date": datetime.datetime.now().strftime("%Y%m%d"),
    }

    targets = [name] if name else sorted(suites_cfg.keys())

    for suite_name in targets:
        if suite_name not in suites_cfg:
            if name:
                ctx.console.error(f"Suite '{suite_name}' not found.")
            continue

        suite = suites_cfg[suite_name]

        # Resolve binary path
        # Try 'executable' or 'exe'
        exe_template = suite.get("executable") or suite.get("exe")

        if exe_template:
            # We might need more variables here if the suite path depends on them
            # But usually suite paths are relative to runner_root or absolute
            env_vars = base_vars.copy()
            env_vars["suite_name"] = suite_name

            exe_path = substitute(exe_template, env_vars)
            print(f"Suite: {suite_name}")
            print(f"  Binary: {exe_path}")
        else:
            print(f"Suite: {suite_name}")
            print(f"  Binary: (not defined)")

        print("")
