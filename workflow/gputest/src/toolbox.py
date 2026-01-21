"""
Toolbox installation logic.
"""
import fnmatch
import os
import shutil
from pathlib import Path
from typing import List

from core.command_runner import CommandError

from .context import Context
from .utils import substitute


def _create_ignore_func(root, excludes):
    """Create an ignore function that handles path-based excludes."""
    def ignore_func(path, names):
        ignored = set()
        # path is the absolute path of the directory being visited
        try:
            rel_parent = Path(path).relative_to(root)
        except ValueError:
            # Should not happen if we are traversing inside root
            return ignored

        for name in names:
            rel_path = rel_parent / name
            path_str = str(rel_path)

            for pattern in excludes:
                # Match against filename (standard behavior)
                if fnmatch.fnmatch(name, pattern):
                    ignored.add(name)
                    break
                # Match against relative path
                if fnmatch.fnmatch(path_str, pattern):
                    ignored.add(name)
                    break
        return ignored
    return ignore_func


def run_toolbox(ctx: Context, targets: List[str] = None):
    """Install test suites defined in [toolbox]."""
    toolbox_cfg = ctx.config.get("toolbox", {})
    hooks_cfg = ctx.config.get("hooks", {})

    # Identify suites by iterating over keys that are dictionaries
    suites = set()
    for key, value in toolbox_cfg.items():
        if isinstance(value, dict):
            if ("src" in value and "dest" in value) or "paths" in value:
                suites.add(key)

    if targets:
        suites = suites.intersection(targets)

    if not suites:
        ctx.console.info("No toolbox suites to install.")
        return

    variables = {
        "project_root": str(ctx.project_root),
        "runner_root": str(ctx.runner_root),
        "home": str(Path.home())
    }

    for suite_name in sorted(suites):
        ctx.console.info(f"Installing suite: {suite_name}")

        suite_def = toolbox_cfg[suite_name]

        # Resolve base paths if they exist
        base_src = None
        if "src" in suite_def:
            base_src = Path(substitute(suite_def["src"], variables))

        base_dest = None
        if "dest" in suite_def:
            base_dest = ctx.runner_root / \
                substitute(suite_def["dest"], variables)

        # Collect all copy operations
        operations = []
        if "paths" in suite_def:
            operations.extend(suite_def["paths"])
        # If src/dest are defined at top level AND there are no paths, or we want to include the top level as an op?
        # Usually if paths are defined, top level src/dest acts as base.
        # If paths are NOT defined, top level src/dest acts as the single
        # operation.
        if "paths" not in suite_def and "src" in suite_def and "dest" in suite_def:
            # Inherit top-level includes/excludes for the default operation
            op = {}
            if "includes" in suite_def:
                op["includes"] = suite_def["includes"]
            if "excludes" in suite_def:
                op["excludes"] = suite_def["excludes"]
            operations.append(op)

        post_install = suite_def.get("post_install", [])

        # Initialize loop variables to prevent leakage or unbound errors
        dest = None
        src = None

        for op in operations:
            src_raw = op.get("src")
            dest_raw = op.get("dest")
            includes = op.get("includes", [])
            excludes = op.get("excludes", [])

            # Resolve Source
            src = None
            if src_raw:
                sub_src = substitute(src_raw, variables)
                src_path = Path(sub_src)
                if src_path.is_absolute():
                    src = src_path
                elif base_src:
                    src = base_src / sub_src
                else:
                    ctx.console.error(
                        f"Relative src '{src_raw}' in suite {suite_name} but no base src defined.")
                    continue
            elif base_src:
                src = base_src
            else:
                # If no src is defined, but we have a base_src, we use it.
                # If neither, we might be in a case where we just want to create a directory or do nothing?
                # But for copy operations, we need a source.
                # However, if this is an operation in 'paths' and it has NO src, maybe it inherits base_src?
                # The logic above: elif base_src: src = base_src covers that.
                # So if we are here, we have no src_raw and no base_src.
                ctx.console.error(f"Missing src in suite {suite_name}")
                continue

            # Resolve Destination
            dest = None
            if dest_raw:
                sub_dest = substitute(dest_raw, variables)
                dest_path = Path(sub_dest)
                if dest_path.is_absolute():
                    dest = dest_path
                elif base_dest:
                    dest = base_dest / sub_dest
                else:
                    dest = ctx.runner_root / sub_dest
            elif base_dest:
                dest = base_dest
            else:
                # If no dest is defined, but we have a base_dest, we use it.
                # If neither, we default to runner_root? Or error?
                # If we have a src, maybe we copy to runner_root/src.name?
                # Let's assume if dest is missing, we mirror the src name in
                # runner_root if base_dest is missing.
                if src:
                    dest = ctx.runner_root / src.name
                else:
                    ctx.console.error(f"Missing dest in suite {suite_name}")
                    continue

            if not src.exists():
                ctx.console.error(f"Source not found: {src}")
                continue

            # Copy logic
            ctx.console.info(f"Copying {src} -> {dest}")

            # Note: We use dirs_exist_ok=True to allow merging multiple paths into the same destination
            # This means we don't automatically clean the destination.
            # If cleaning is required, it should be done explicitly or we need
            # a new config option.

            if ctx.console.dry_run:
                # Use runner to print dry run command if possible, or just log
                # Since shutil.copytree is python, we can't easily use runner.run() for it unless we wrap it in a shell command.
                # But we can simulate it.
                ctx.runner.run(["cp", "-r", str(src), str(dest)],
                               note=f"includes: {includes}, excludes: {excludes}")
            else:
                if src.is_file():
                    if includes:
                        ctx.console.error(
                            f"Cannot use 'includes' with a file source: {src}")
                        continue

                    # If dest is an existing directory, copy into it.
                    # Otherwise, treat dest as the target filename (renaming).
                    if dest.is_dir():
                        target = dest / src.name
                    else:
                        target = dest

                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists(follow_symlinks=False):
                        target.unlink()
                    shutil.copy2(src, target, follow_symlinks=False)

                elif not includes:
                    # Default: Copy everything
                    ignore = _create_ignore_func(
                        src, excludes) if excludes else None
                    force_copytree(
                        src, dest, ignore=ignore, dirs_exist_ok=True)
                else:
                    # Includes mode: Copy only matching paths
                    dest.mkdir(parents=True, exist_ok=True)

                    for pattern in includes:
                        # Use glob to find matches in src
                        for path in src.glob(pattern):
                            rel_path = path.relative_to(src)
                            target = dest / rel_path

                            # Check if excluded (for top-level matches)
                            # We check against the relative path from src, or just filename?
                            # If exclude is "foo/bar", and we matched "foo", we shouldn't exclude "foo".
                            # But if we matched "foo/bar", we should.

                            # Let's check if the path itself is excluded
                            # We need to check relative path from src?
                            # If includes=["foo"], excludes=["foo/bar"].
                            # We copy "foo". Inside "foo", "bar" will be
                            # excluded by ignore func.

                            # If includes=["foo"], excludes=["foo"].
                            # We should skip "foo".

                            # Check exclusion for the root of the copy
                            if excludes:
                                # Check filename
                                if any(fnmatch.fnmatch(path.name, excl)
                                       for excl in excludes):
                                    continue
                                # Check relative path from src
                                path_from_src = path.relative_to(src)
                                if any(
                                    fnmatch.fnmatch(
                                        str(path_from_src),
                                        excl) for excl in excludes):
                                    continue

                            if path.is_dir():
                                # For the ignore function, the root is 'path' (the directory being copied)
                                # But excludes might be relative to 'src' or 'path'?
                                # If excludes=["image/swapchain.txt"] and we copy "vk-default" (which contains image/).
                                # Then relative to "vk-default", it is "image/swapchain.txt".
                                # So if we copy 'path', the ignore func should
                                # be relative to 'path'.

                                # BUT, if excludes are defined relative to 'src'?
                                # In the config: src=".../mustpass/main", includes=["vk-default"], excludes=["image/..."].
                                # "image/..." is inside "vk-default".
                                # So relative to "vk-default", it is "image/...".
                                # Relative to "src", it is
                                # "vk-default/image/...".

                                # If the user wrote "image/...", they expect it to match inside the included dir?
                                # Or did they write "vk-default/image/..."?
                                # The config has: excludes = ["image/swapchain-mutable.txt"]
                                # The include is "vk-default".
                                # So it seems excludes are relative to the *included item*?
                                # Or relative to the *source root*?

                                # In rsync: --exclude-from=FILE.
                                # The file contains "image/swapchain-mutable.txt".
                                # rsync is run on ".../vk-default".
                                # So excludes are relative to "vk-default".

                                # So yes, excludes are relative to the root of
                                # the copy operation.
                                ignore = _create_ignore_func(
                                    path, excludes) if excludes else None
                                force_copytree(
                                    path, target, ignore=ignore, dirs_exist_ok=True)
                            else:
                                target.parent.mkdir(
                                    parents=True, exist_ok=True)
                                if target.exists(follow_symlinks=False):
                                    target.unlink()
                                shutil.copy2(
                                    path, target, follow_symlinks=False)

        # Run post-install hooks
        suite_vars = variables.copy()

        # Use base_dest/base_src for hooks if available, otherwise use the last
        # operation's paths
        hook_dest = base_dest if base_dest else dest
        hook_src = base_src if base_src else src

        if hook_dest:
            suite_vars["dest"] = str(hook_dest)
        if hook_src:
            suite_vars["src"] = str(hook_src)

        suite_vars["name"] = suite_name

        for hook_name in post_install:
            hook_cmd_tpl = hooks_cfg.get(hook_name)
            if not hook_cmd_tpl:
                ctx.console.error(f"Hook '{hook_name}' not found in [hooks]")
                continue

            cmd_str = substitute(hook_cmd_tpl, suite_vars)
            ctx.console.info(f"Running hook: {hook_name}")

            try:
                # Run in shell to support pipes/redirection as seen in config
                # Use hook_dest as cwd if available, else runner_root
                cwd = hook_dest if hook_dest else ctx.runner_root
                ctx.runner.run(["sh", "-c", cmd_str], cwd=cwd,
                               check=True, stream=True)
            except CommandError as e:
                ctx.console.error(f"Hook failed: {e}")


def force_copytree(src, dst, ignore=None, dirs_exist_ok=False):
    """
    Recursive copy that forces overwrite of destination files/symlinks.
    Fixes shutil.copytree failure when overwriting symlinks with dirs_exist_ok=True.
    """
    src = Path(src)
    dst = Path(dst)

    if not dirs_exist_ok and dst.exists():
        raise FileExistsError(f"{dst} exists")

    dst.mkdir(parents=True, exist_ok=True)

    names = [x.name for x in src.iterdir()]
    ignored_names = ignore(src, names) if ignore else set()

    for name in names:
        if name in ignored_names:
            continue

        s = src / name
        d = dst / name

        if s.is_symlink():
            if d.exists(follow_symlinks=False):
                d.unlink()
            linkto = os.readlink(s)
            os.symlink(linkto, d)
        elif s.is_dir():
            force_copytree(s, d, ignore=ignore, dirs_exist_ok=True)
        else:
            if d.exists(follow_symlinks=False):
                d.unlink()
            shutil.copy2(s, d, follow_symlinks=False)
