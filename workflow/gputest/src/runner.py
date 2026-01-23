"""
Test runner logic.
"""

import datetime
import glob
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from core.archive import ArchiveArtifact, ArchiveManager
from core.command_runner import CommandError, SubprocessCommandRunner

from .context import Context
from .utils import resolve_env, substitute


def get_gpu_id_from_vulkan(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Get GPU ID from vulkaninfo."""
    try:
        if not shutil.which("vulkaninfo"):
            return None

        res = SubprocessCommandRunner().run(
            ["vulkaninfo", "--summary"], env=env, check=False
        )
        if res.returncode != 0:
            return None

        # Parse vendorID and deviceID from GPU0 (first device)
        # GPU0:
        #    vendorID           = 0x1002
        #    deviceID           = 0x73bf

        vendor_match = re.search(r"vendorID\s*=\s*(0x[0-9a-fA-F]+)", res.stdout)
        device_match = re.search(r"deviceID\s*=\s*(0x[0-9a-fA-F]+)", res.stdout)

        if vendor_match and device_match:
            vendor = vendor_match.group(1).replace("0x", "")
            device = device_match.group(1).replace("0x", "")
            return f"{vendor}:{device}"

    except Exception:
        pass
    return None


def get_gpu_id_from_gl(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Get GPU ID from glxinfo."""
    try:
        if not shutil.which("glxinfo"):
            return None

        res = SubprocessCommandRunner().run(["glxinfo", "-B"], env=env, check=False)
        if res.returncode != 0:
            return None

        # Vendor: AMD (0x1002)
        # Device: AMD Radeon RX 6800 XT (RADV NAVI21) (0x73bf)

        vendor_match = re.search(r"Vendor:.*?\((0x[0-9a-fA-F]+)\)", res.stdout)
        device_match = re.search(r"Device:.*?\((0x[0-9a-fA-F]+)\)", res.stdout)

        if vendor_match and device_match:
            vendor = vendor_match.group(1).replace("0x", "")
            device = device_match.group(1).replace("0x", "")
            return f"{vendor}:{device}"

    except Exception:
        pass
    return None


def get_gpu_device_id(env: Optional[Dict[str, str]] = None) -> str:
    """Attempt to get the primary GPU Device ID."""
    # Try Vulkan first as it's most reliable for modern GPUs
    vk_id = get_gpu_id_from_vulkan(env)
    if vk_id:
        return vk_id

    # Try GL
    gl_id = get_gpu_id_from_gl(env)
    if gl_id:
        return gl_id

    try:
        # Try to find a PCI device that is a display controller
        lspci = shutil.which("lspci")
        if lspci:
            res = SubprocessCommandRunner().run(
                ["sh", "-c", "lspci -n | grep -E '0300|0302' | head -1"], check=False
            )
            if res.returncode == 0 and res.stdout.strip():
                parts = res.stdout.strip().split()
                for part in parts:
                    if (
                        ":" in part and len(part.split(":")) == 2 and len(part) == 9
                    ):  # vendor:device
                        return part
    except Exception:
        pass
    return "unknown_gpu"


def generate_testlist(ctx: Context, output_dir: Path, caselists: List[Path]):
    """Generate testlist.txt by concatenating input caselists."""
    dest = output_dir / "testlist.txt"
    try:
        # Ensure output directory exists
        if not output_dir.exists():
            output_dir.mkdir(parents=True, exist_ok=True)

        with open(dest, "w") as out_f:
            for cl in caselists:
                if cl.exists():
                    with open(cl, "r") as in_f:
                        for line in in_f:
                            line = line.strip()
                            if line:
                                out_f.write(f"{line}\n")

        ctx.console.info(f"Generated {dest} from {len(caselists)} caselists")
    except Exception as e:
        ctx.console.error(f"Failed to generate testlist.txt: {e}")


def run_tests(ctx: Context, test_names: List[str]):
    """Run specified tests."""
    tests_cfg = ctx.config.get("tests", {})
    suites_cfg = ctx.config.get("suites", {})
    drivers_cfg = ctx.config.get("drivers", {})
    layouts_cfg = ctx.config.get("layouts", {})
    backends_cfg = ctx.config.get("backends", {})
    hooks_cfg = ctx.config.get("hooks", {})

    if not test_names:
        ctx.console.error("No tests specified.")
        return

    # Common variables
    base_vars = {
        "project_root": str(ctx.project_root),
        "runner_root": str(ctx.runner_root),
        "home": str(Path.home()),
        "date": datetime.datetime.now().strftime("%Y%m%d"),
        "gpu_id": "unknown_gpu",  # Placeholder, will be resolved per test
    }

    for test_name in test_names:
        if test_name not in tests_cfg:
            ctx.console.error(f"Test '{test_name}' not defined in configuration.")
            continue

        ctx.console.info(f"Preparing test: {test_name}")
        test_def = tests_cfg[test_name]

        driver_name = test_def.get("driver")
        backend_name = test_def.get("backend")
        suite_name = test_def.get("suite")

        if not driver_name or not suite_name:
            ctx.console.error(f"Test '{test_name}' missing driver or suite definition.")
            continue

        # Resolve components
        driver = drivers_cfg.get(driver_name)
        if not driver:
            ctx.console.error(f"Driver '{driver_name}' not found.")
            continue

        layout_name = driver.get("layout")
        layout = layouts_cfg.get(layout_name)
        if not layout:
            ctx.console.error(
                f"Layout '{layout_name}' not found for driver '{driver_name}'."
            )
            continue

        suite = suites_cfg.get(suite_name)
        if not suite:
            ctx.console.error(f"Suite '{suite_name}' not found.")
            continue

        backend = backends_cfg.get(backend_name, {}) if backend_name else {}

        # Build Environment
        # 1. Layout variables
        # Note: We use base_vars with placeholder gpu_id here.
        # If layout path depends on gpu_id, this might be an issue, but usually
        # it doesn't.
        layout_root = substitute(driver.get("root", "/usr"), base_vars)
        env_vars = base_vars.copy()
        env_vars["root"] = layout_root

        # Inject names for template resolution
        env_vars.update(
            {
                "test_name": test_name,
                "suite_name": suite_name,
                "suite_type": suite.get("type", ""),
                "driver_name": driver_name,
                "backend_name": backend_name or "",
                "layout_name": layout_name,
            }
        )

        # Pass cpu_count to variables for template resolution
        cpu_count = os.cpu_count() or 1
        jobs = max(1, int(cpu_count * 0.75))
        env_vars["cpu_count"] = cpu_count
        env_vars["jobs"] = jobs

        # 2. Merge Env
        # Priority: Backend > Driver > Layout
        merged_env = os.environ.copy()

        # Layout Env
        merged_env.update(resolve_env(layout.get("env", {}), env_vars))
        # Driver Env
        merged_env.update(resolve_env(driver.get("env", {}), env_vars))
        # Backend Env
        merged_env.update(resolve_env(backend.get("env", {}), env_vars))

        # Detect GPU ID for this test environment
        # Heuristic: if suite name or exe contains 'gl' or 'piglit', try GL
        # first, else Vulkan
        runner_bin = suite.get("runner", "")
        exe_bin = suite.get("executable") or suite.get("exe") or ""

        is_gl = (
            "gl" in suite_name
            or "gles" in suite_name
            or "piglit" in runner_bin
            or "gl" in exe_bin
        )

        test_gpu_id = None
        if is_gl:
            test_gpu_id = get_gpu_id_from_gl(merged_env)
            if not test_gpu_id:
                test_gpu_id = get_gpu_id_from_vulkan(merged_env)
        else:
            test_gpu_id = get_gpu_id_from_vulkan(merged_env)
            if not test_gpu_id:
                test_gpu_id = get_gpu_id_from_gl(merged_env)

        if not test_gpu_id:
            # Fallback to lspci if everything fails (e.g. tools missing)
            test_gpu_id = get_gpu_device_id(merged_env)

        ctx.console.info(f"Detected GPU ID: {test_gpu_id}")

        # Update variables for this test
        test_vars = base_vars.copy()
        test_vars["gpu_id"] = test_gpu_id
        test_vars["root"] = layout_root  # Ensure root is available

        # Prepare Execution
        suite_type = suite.get("type")

        # Resolve args with template engine
        def resolve_list(items):
            return [substitute(item, env_vars) for item in items]

        # Determine working directory
        cwd = ctx.runner_root

        # --- Variable Preparation Phase ---

        # 1. Output Directory
        output_dir = (
            ctx.runner_root
            / "testing"
            / test_name
            / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        )

        # 2. Runner Binary
        runner_bin = suite.get("runner", "")
        if not runner_bin:
            if suite_type == "deqp":
                runner_bin = "deqp-runner"
            elif suite_type == "piglit":
                runner_bin = "piglit-runner"

        # Resolve runner_bin to absolute path if it exists in runner_root
        runner_path = ctx.runner_root / runner_bin
        if runner_path.exists() and os.access(runner_path, os.X_OK):
            runner_bin = str(runner_path)

        # 3. Executable Binary
        exe_bin = suite.get("executable") or suite.get("exe") or ""
        # Try to resolve exe_bin relative to install_dir if it's a relative path
        # For dEQP, it's usually in runner_root/deqp
        # For Piglit, it's usually in runner_root/piglit
        install_dir = ctx.runner_root
        if suite_type == "deqp":
            install_dir = ctx.runner_root / "deqp"
        elif suite_type == "piglit":
            install_dir = ctx.runner_root / "piglit"

        if exe_bin and not Path(exe_bin).is_absolute():
            potential_exe = install_dir / exe_bin
            if potential_exe.exists():
                exe_bin = str(potential_exe)

        # 4. Caselists & Testlist (dEQP specific mostly, but generic concept)
        caselists = suite.get("caselists", [])
        testlist_path = ""
        if caselists:
            resolved_caselists = []
            for cl in caselists:
                # Expand wildcards
                cl_path = install_dir / cl
                # If it contains wildcards, glob it
                if "*" in str(cl_path):
                    matches = glob.glob(str(cl_path))
                    for m in sorted(matches):
                        resolved_caselists.append(Path(m))
                else:
                    resolved_caselists.append(cl_path)

            if not ctx.console.dry_run:
                generate_testlist(ctx, output_dir, resolved_caselists)
            testlist_path = str(output_dir / "testlist.txt")

        # 5. Excludes
        excludes = suite.get("excludes", [])
        exclude_list_path = ""
        exclude_list_arg = ""
        if excludes:
            exclude_list_path = str(output_dir / "exclude_list.txt")
            exclude_list_arg = f"--exclude-list {exclude_list_path}"
            if not ctx.console.dry_run:
                output_dir.mkdir(parents=True, exist_ok=True)
                with open(exclude_list_path, "w") as f:
                    for excl in excludes:
                        f.write(f"{excl}\n")

        # 6. Arguments
        runner_args_list = resolve_list(suite.get("runner_args", []))
        runner_args = " ".join(runner_args_list)

        deqp_args_list = resolve_list(suite.get("deqp_args", []))
        deqp_args = " ".join(deqp_args_list)

        args_list = resolve_list(suite.get("args", []))
        args = " ".join(args_list)

        # Update variables for template
        cmd_vars = test_vars.copy()
        cmd_vars.update(
            {
                "output_dir": str(output_dir),
                "testlist_path": testlist_path,
                "exclude_list_path": exclude_list_path,
                "exclude_list_arg": exclude_list_arg,
                "runner_bin": runner_bin,
                "exe_bin": exe_bin,
                "runner_args": runner_args,
                "deqp_args": deqp_args,
                "args": args,
                "install_dir": str(install_dir),
                "piglit_folder": str(install_dir),  # Alias for piglit-runner
            }
        )

        # --- Command Generation Phase ---

        cmd_template = suite.get("command")
        cmd_str = ""

        if cmd_template:
            # User defined command
            cmd_str = substitute(cmd_template, cmd_vars)
        else:
            # Default templates
            if suite_type == "deqp":
                # Default dEQP command (using deqp-runner)
                # We construct it manually to ensure correct list structure for subprocess
                # But here we are building a string or list?
                # If we use substitute, we get a string.
                # Let's define the default template string.

                # Note: We need to handle caselists carefully.
                # If we use testlist_path, we pass --caselist testlist.txt?
                # No, deqp-runner takes multiple --caselist args.
                # But we generated a single testlist.txt containing all cases?
                # Wait, generate_testlist concatenates them.
                # So we can just pass that one file?
                # deqp-runner --caselist expects a file with case names.
                # Yes, testlist.txt is exactly that.

                caselist_arg = f"--caselist {testlist_path}" if testlist_path else ""

                cmd_template = """
                {{runner_bin}} run \
                    --output {{output_dir}} \
                    {{caselist_arg}} \
                    {{exclude_list_arg}} \
                    {{runner_args}} \
                    --deqp {{exe_bin}} \
                    -- \
                    {{deqp_args}}
                """
                cmd_vars["caselist_arg"] = caselist_arg
                cmd_str = substitute(cmd_template, cmd_vars)

            elif suite_type == "piglit":
                # Default Piglit command (using piglit-runner)
                # piglit-runner run --output <output-dir> --piglit-folder <piglit-folder> -- <piglit-args>...

                cmd_template = """
                {{runner_bin}} run \
                    --output {{output_dir}} \
                    --piglit-folder {{piglit_folder}} \
                    {{runner_args}} \
                    -- \
                    {{deqp_args}}
                """
                # Note: piglit-runner passes args after -- to piglit?
                # Or does it take piglit args directly?
                # The user provided example: ... -- <deqp-args>...
                # So we use deqp_args here as a placeholder for "args passed to the underlying tool"

                cmd_str = substitute(cmd_template, cmd_vars)

            else:
                # Generic fallback
                cmd_template = "{{exe_bin}} {{args}}"
                cmd_str = substitute(cmd_template, cmd_vars)

        # Parse command string to list for subprocess
        # We use shlex to split correctly handling quotes
        import shlex

        cmd = shlex.split(cmd_str)

        # Execute
        ctx.console.info(f"Running test '{test_name}'")
        ctx.console.debug(f"Environment: {merged_env}")
        ctx.console.debug(f"Command: {cmd}")

        try:
            # Create output dir if not exists (it might have been created for excludes/testlist)
            if not ctx.console.dry_run:
                output_dir.mkdir(parents=True, exist_ok=True)

            # Prepare hook variables
            hook_vars = env_vars.copy()
            hook_vars.update(
                {
                    "name": suite_name,
                    "output_dir": str(output_dir),
                    "test_name": test_name,
                }
            )

            # Run pre-run hooks
            pre_hooks = suite.get("pre_run_hooks", []) + test_def.get("pre_run", [])
            # So I should probably default to running 'get_git_info' if
            # it exists, or add a global 'default_pre_hooks'.

            # Let's implement generic hooks execution
            def run_hooks(hooks_list, phase_name):
                for hook_name in hooks_list:
                    hook_cmd_tpl = hooks_cfg.get(hook_name)
                    if not hook_cmd_tpl:
                        ctx.console.error(f"Hook '{hook_name}' not found in [hooks]")
                        continue

                    ctx.console.info(f"Running {phase_name} hook: {hook_name}")
                    try:
                        ctx.runner.run(
                            ["sh", "-c", substitute(hook_cmd_tpl, hook_vars)],
                            cwd=output_dir,
                            check=False,
                        )
                    except Exception as e:
                        ctx.console.error(
                            f"{phase_name} hook '{hook_name}' failed: {e}"
                        )

            # Run explicit pre-run hooks
            run_hooks(pre_hooks, "pre-run")

            # Run the test
            ctx.runner.run(cmd, cwd=cwd, env=merged_env, check=True, stream=True)

            # Run post-run hooks
            post_hooks = suite.get("post_run_hooks", []) + test_def.get("post_run", [])
            run_hooks(post_hooks, "post-run")

            # Archive results
            # Naming: vendor/suite_device_date.arch
            archive_filename = (
                f"{suite_name}_{test_vars['gpu_id']}_{test_vars['date']}.tar.zst"
            )
            archive_parent = ctx.result_dir / driver_name

            if not ctx.console.dry_run:
                archive_parent.mkdir(parents=True, exist_ok=True)

            archive_path = archive_parent / archive_filename

            ctx.console.info(f"Archiving results to {archive_path}")

            # Baseline naming: vendor_suite_date
            baseline_name = f"{driver_name}_{suite_name}_{test_vars['date']}"

            # Determine what to archive
            archive_files = suite.get("archive_files")
            source_dir_for_archive = output_dir

            if archive_files and not ctx.console.dry_run:
                # Create a staging directory
                staging_dir = output_dir / ".archive_staging"
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)
                staging_dir.mkdir()

                ctx.console.info(f"Collecting artifacts matching: {archive_files}")

                for pattern in archive_files:
                    # Use glob to find matches
                    matches = list(output_dir.glob(pattern))
                    for match in matches:
                        if match == staging_dir:
                            continue

                        rel_path = match.relative_to(output_dir)
                        dest_path = staging_dir / rel_path

                        if match.is_file():
                            dest_path.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(match, dest_path)
                        elif match.is_dir():
                            # For directories, we copy the whole tree
                            # If dest exists (e.g. from another pattern), we might need to merge?
                            # shutil.copytree fails if dest exists.
                            # Let's use a simple approach: if dest exists, skip or merge?
                            # For now, assume distinct matches or handle simple cases.
                            if not dest_path.exists():
                                shutil.copytree(match, dest_path)

                source_dir_for_archive = staging_dir

            # Use ArchiveManager for archiving
            archive_manager = ArchiveManager(ctx.console)
            artifact = ArchiveArtifact(
                source_dir=source_dir_for_archive, label=test_name
            )

            try:
                if source_dir_for_archive.exists():
                    archive_manager.create_archive(
                        artifact=artifact, target_path=archive_path, overwrite=True
                    )
                elif ctx.console.dry_run:
                    ctx.console.info(
                        f"[dry-run] Would archive {test_name} to {archive_path}"
                    )
            except Exception as e:
                ctx.console.error(f"Failed to archive results: {e}")
            finally:
                # Cleanup staging directory
                if (
                    archive_files
                    and source_dir_for_archive.exists()
                    and source_dir_for_archive.name == ".archive_staging"
                ):
                    shutil.rmtree(source_dir_for_archive, ignore_errors=True)

        except CommandError as e:
            ctx.console.error(f"Test failed: {e}")
            # If dry run, we don't care about failure
            if not ctx.console.dry_run:
                pass  # Or exit? Usually we want to continue to next test.
