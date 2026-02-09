#!/usr/bin/env python3
"""
Python replacement for git-setup-remotes.
Configures git remotes for mirroring or contributing.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Add workflow root to path to verify imports if run standalone
current_dir = Path(__file__).parent
workflow_root = current_dir.parent
if str(workflow_root) not in sys.path:
    sys.path.insert(0, str(workflow_root))

try:
    from core.git_remotes import (
        RemoteInfo,
        construct_remote_url,
        get_git_config,
        get_platform_page_suffix,
        get_platform_user,
        parse_remote_url,
        resolve_ssh_alias,
    )
except ImportError:
    # Fallback if running fro scripts/workflow
    sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
    from core.git_remotes import (
        RemoteInfo,
        construct_remote_url,
        get_git_config,
        get_platform_page_suffix,
        get_platform_user,
        parse_remote_url,
        resolve_ssh_alias,
    )


def run_git(args, check=True):
    result = subprocess.run(["git"] + args, capture_output=True, text=True, check=check)
    return result.stdout.strip()


def get_first_remote_url(remote_name):
    # git remote get-url fails if there are multiple URLs (push URLs).
    # We want the distinct single fetch URL usually, or just the first one.
    # --all returns all lines.
    res = run_git(["remote", "get-url", "--all", remote_name], check=False)
    if res:
        return res.splitlines()[0]
    return ""


def get_all_push_urls(remote_name):
    res = run_git(["remote", "get-url", "--all", "--push", remote_name], check=False)
    if res:
        return [u.strip() for u in res.splitlines()]
    return []


def get_repo_name():
    url = get_first_remote_url("origin")
    if url:
        return os.path.splitext(os.path.basename(url))[0]
    return os.path.basename(os.getcwd())


def get_page_base_name(repo_name):
    # Check against known suffixes
    for p in ["github", "gitlab", "gitea", "codeberg", "bitbucket"]:
        suffix = get_platform_page_suffix(p)
        if not suffix:
            continue
        if repo_name.endswith(f".{suffix}"):
            return repo_name[: -(len(suffix) + 1)]
    return None


def identify_platform_by_host(host):
    defaults = {
        "github.com": "github",
        "gitlab.com": "gitlab",
        "gitea.com": "gitea",
        "codeberg.org": "codeberg",
        "bitbucket.org": "bitbucket",
        "dev.azure.com": "azure",
        "visualstudio.com": "azure",
    }
    return defaults.get(host, host)


def setup_mirroring():
    current_repo = get_repo_name()

    mirrors = (
        get_git_config("ginshio.remotes.mirrors") or "github,gitlab,codeberg,bitbucket"
    )
    mirror_list = [m.strip() for m in mirrors.split(",") if m.strip()]

    origin_url = get_first_remote_url("origin")
    origin_real_host = ""
    if origin_url:
        info = parse_remote_url(origin_url)
        if info:
            origin_real_host = resolve_ssh_alias(info.host)

    page_base = get_page_base_name(current_repo)
    if page_base:
        print(f"Detected Page Repository. Base name: {page_base}")

    origin_covered = False
    mirror_urls = []

    for platform in mirror_list:
        user = get_platform_user(platform)
        if not user:
            # Silent skip is better than noise
            continue

        target_repo = current_repo
        if page_base:
            suffix = get_platform_page_suffix(platform)
            if suffix:
                target_repo = f"{page_base}.{suffix}"

        url = construct_remote_url(platform, user, target_repo)
        if not url:
            continue

        # Check coverage
        info = parse_remote_url(url)
        if info:
            constructed_real_host = resolve_ssh_alias(info.host)
            if origin_real_host and constructed_real_host == origin_real_host:
                origin_covered = True

        mirror_urls.append(url)

    # Logic:
    # If we have mirror URLs valid, we want to enforce them as push URLs.
    if not mirror_urls:
        return

    existing_push_urls = get_all_push_urls("origin")

    print(f"Configuring {len(mirror_urls)} mirrors for repo: {current_repo}")

    first_url = True
    if not origin_covered and origin_url:
        # Keep original origin as first push URL if not covered
        # Only update if it's different to avoid output noise or redudant writes
        if not existing_push_urls or existing_push_urls[0] != origin_url:
            run_git(["remote", "set-url", "--push", "origin", origin_url])
            # Update local list to track state
            if not existing_push_urls:
                existing_push_urls = [origin_url]
            else:
                existing_push_urls[0] = origin_url

        first_url = False

    for url in mirror_urls:
        if first_url:
            if not existing_push_urls or existing_push_urls[0] != url:
                print(f"  + Primary: {url}")
                run_git(["remote", "set-url", "--push", "origin", url])
                if not existing_push_urls:
                    existing_push_urls = [url]
                else:
                    existing_push_urls[0] = url
            first_url = False
        else:
            # Check duplicates (logic form before)
            info = parse_remote_url(url)
            if info:
                url_real = resolve_ssh_alias(info.host)
                if (
                    not origin_covered
                    and origin_real_host
                    and url_real == origin_real_host
                ):
                    continue

            # Check if strictly already exists in config
            if url in existing_push_urls:
                continue

            print(f"  + Mirror:  {url}")
            run_git(["remote", "set-url", "--add", "--push", "origin", url])


def setup_contributor():
    print("Configuring contributor mode...")

    origin_url = get_first_remote_url("origin")
    if not origin_url:
        print("Error: No 'origin' remote found.", file=sys.stderr)
        sys.exit(1)

    info = parse_remote_url(origin_url)
    if not info:
        print(f"Error: Could not parse origin URL: {origin_url}", file=sys.stderr)
        sys.exit(1)

    origin_host = info.host
    origin_path = f"{info.owner}/{info.repo}"

    # 2. Determine Platform & User
    platform = identify_platform_by_host(origin_host)
    if not platform:
        platform = origin_host

    user = get_platform_user(platform)
    if not user:
        print(
            f"Could not determine user for platform '{platform}'. Please configure 'platform.{platform}.user'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 3. Check if Origin is owned by User (Fork Detection)
    # Origin path is owner/repo.
    # Check if owner == user
    is_origin_fork = info.owner == user

    if is_origin_fork:
        print(f"Detected 'origin' points to your user '{user}'.")

        upstream_url = get_first_remote_url("upstream")
        if upstream_url:
            # Already set, quiet success
            pass
        else:
            print("WARNING: 'upstream' remote is missing.")
            print("Please run: git remote add upstream <original-repo-url>")

        target_repo = info.repo
        new_origin_url = construct_remote_url(platform, user, target_repo)

        if origin_url != new_origin_url:
            print(f"Updating 'origin' to: {new_origin_url}")
            run_git(["remote", "set-url", "origin", new_origin_url])

    else:
        # Origin is Upstream
        print(f"Detected 'origin' likely belongs to upstream.")

        target_repo = info.repo
        new_origin_url = construct_remote_url(platform, user, target_repo)

        # Check idempotency:
        # Have we already swapped them?
        # i.e., is 'upstream' == current 'origin_url'?

        existing_upstream = get_first_remote_url("upstream")
        if existing_upstream == origin_url:
            print("  - 'upstream' is already configured correctly.")
            # Check if origin is set to fork
            existing_origin = get_first_remote_url("origin")
            if existing_origin == new_origin_url:
                print("  - 'origin' (fork) is already configured correctly.")
                return
            # If origin is upstream, and upstream is upstream... wait, that's impossible if we assume git logic.
            # But if origin == upstream_url and upstream == upstream_url.

        if existing_upstream:
            print("'upstream' already exists.")
        else:
            print("Renaming 'origin' to 'upstream'...")
            run_git(["remote", "rename", "origin", "upstream"])

        # After rename, 'origin' is gone. We need to create it.
        origin_exists = get_first_remote_url("origin")
        if origin_exists:
            if origin_exists == new_origin_url:
                pass  # Idempotent
            else:
                print(
                    "'origin' exists but points to something else. Skipping fork creation."
                )
        else:
            print(f"Adding 'origin' (fork): {new_origin_url}")
            run_git(["remote", "add", "origin", new_origin_url])


def main():
    parser = argparse.ArgumentParser(description="Git Remote Setup Tool")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--mirror",
        "-m",
        action="store_true",
        default=True,
        help="Setup mirroring remotes (default)",
    )
    group.add_argument(
        "--contribute",
        "-c",
        action="store_true",
        help="Setup contributor remotes (fork logic)",
    )

    args = parser.parse_args()

    if args.contribute:
        setup_contributor()
    else:
        setup_mirroring()


if __name__ == "__main__":
    main()
