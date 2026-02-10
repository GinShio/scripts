#!/usr/bin/env python3
"""
Python replacement for git-setup-remotes.
Configures git remotes for mirroring or contributing.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

# Add workflow root to path to verify imports if run standalone
current_dir = Path(__file__).parent
workflow_root = current_dir.parent
if str(workflow_root) not in sys.path:
    sys.path.insert(0, str(workflow_root))

from core.git_api import GitRepository, parse_remote_url, resolve_ssh_alias

# ---------------------------------------------------------------------------
# Platform helpers (config keys under ``workflow.platform.<service>.*``)
# ---------------------------------------------------------------------------


def _get_repo() -> GitRepository:
    """Return a :class:`GitRepository` for the CWD repository."""
    return GitRepository(Path.cwd())


def _get_platform_host(service_name: str) -> str:
    """Get configured host for a platform, with fallback to built-ins."""
    repo = _get_repo()
    host = repo.get_config(f"workflow.platform.{service_name}.host")
    if host:
        return host

    defaults = {
        "github": "github.com",
        "gitlab": "gitlab.com",
        "gitea": "gitea.com",
        "codeberg": "codeberg.org",
        "bitbucket": "bitbucket.org",
        "azure": "dev.azure.com",
    }
    return defaults.get(service_name, "")


def _get_platform_user(service_name: str) -> Optional[str]:
    """Get current username for a specific platform."""
    repo = _get_repo()
    user = repo.get_config(f"workflow.platform.{service_name}.user")
    if not user:
        user = repo.get_config("user.name")
    return user


def _get_platform_page_suffix(service_name: str) -> str:
    """Get platform page suffix (for .io/.page repos)."""
    repo = _get_repo()
    suffix = repo.get_config(f"workflow.platform.{service_name}.page-suffix")
    if suffix:
        return suffix

    defaults = {
        "github": "github.io",
        "gitlab": "gitlab.io",
        "gitea": "gitea.io",
        "codeberg": "codeberg.page",
        "bitbucket": "bitbucket.io",
    }
    return defaults.get(service_name, "")


def _construct_remote_url(
    service_name: str, user: str, repo_name: str
) -> Optional[str]:
    """Construct a remote URL for a given platform."""
    repo = _get_repo()

    # 1. Check for custom URL format override
    custom_url = repo.get_config(f"workflow.platform.{service_name}.url")
    if custom_url:
        return custom_url.replace("{user}", user).replace("{repo}", repo_name)

    # 2. Resolve Host
    host = _get_platform_host(service_name)
    if not host:
        if "." in service_name:
            host = service_name
        else:
            return None

    # 3. Check for SSH Alias override
    ssh_alias = repo.get_config(f"workflow.platform.{service_name}.ssh-alias")

    # 4. Standard Construction
    if ssh_alias:
        return f"{ssh_alias}:{user}/{repo_name}.git"

    return f"git@{host}:{user}/{repo_name}.git"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_first_remote_url(remote_name):
    repo = _get_repo()
    url = repo.get_remote_url(remote_name)
    return url or ""


def get_all_push_urls(remote_name):
    repo = _get_repo()
    return repo.get_remote_urls(remote_name, push=True)


def get_repo_name():
    url = get_first_remote_url("origin")
    if url:
        return os.path.splitext(os.path.basename(url))[0]
    return os.path.basename(os.getcwd())


def get_page_base_name(repo_name):
    # Check against known suffixes
    for p in ["github", "gitlab", "gitea", "codeberg", "bitbucket"]:
        suffix = _get_platform_page_suffix(p)
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


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def setup_mirroring():
    repo = _get_repo()
    current_repo = get_repo_name()

    mirrors = (
        repo.get_config("workflow.platform.mirrors")
        or "github,gitlab,codeberg,bitbucket"
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
        user = _get_platform_user(platform)
        if not user:
            continue

        target_repo = current_repo
        if page_base:
            suffix = _get_platform_page_suffix(platform)
            if suffix:
                target_repo = f"{page_base}.{suffix}"

        url = _construct_remote_url(platform, user, target_repo)
        if not url:
            continue

        # Check coverage
        info = parse_remote_url(url)
        if info:
            constructed_real_host = resolve_ssh_alias(info.host)
            if origin_real_host and constructed_real_host == origin_real_host:
                origin_covered = True

        mirror_urls.append(url)

    if not mirror_urls:
        return

    existing_push_urls = get_all_push_urls("origin")

    print(f"Configuring {len(mirror_urls)} mirrors for repo: {current_repo}")

    first_url = True
    if not origin_covered and origin_url:
        if not existing_push_urls or existing_push_urls[0] != origin_url:
            repo.set_remote_url("origin", origin_url, push=True)
            if not existing_push_urls:
                existing_push_urls = [origin_url]
            else:
                existing_push_urls[0] = origin_url

        first_url = False

    for url in mirror_urls:
        if first_url:
            if not existing_push_urls or existing_push_urls[0] != url:
                print(f"  + Primary: {url}")
                repo.set_remote_url("origin", url, push=True)
                if not existing_push_urls:
                    existing_push_urls = [url]
                else:
                    existing_push_urls[0] = url
            first_url = False
        else:
            info = parse_remote_url(url)
            if info:
                url_real = resolve_ssh_alias(info.host)
                if (
                    not origin_covered
                    and origin_real_host
                    and url_real == origin_real_host
                ):
                    continue

            if url in existing_push_urls:
                continue

            print(f"  + Mirror:  {url}")
            repo.set_remote_url("origin", url, push=True, add=True)


def setup_contributor():
    repo = _get_repo()
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

    # 2. Determine Platform & User
    platform = identify_platform_by_host(origin_host)
    if not platform:
        platform = origin_host

    user = _get_platform_user(platform)
    if not user:
        print(
            f"Could not determine user for platform '{platform}'. "
            f"Please configure 'workflow.platform.{platform}.user'.",
            file=sys.stderr,
        )
        sys.exit(1)

    # 3. Check if Origin is owned by User (Fork Detection)
    is_origin_fork = info.owner == user

    if is_origin_fork:
        print(f"Detected 'origin' points to your user '{user}'.")

        upstream_url = get_first_remote_url("upstream")
        if upstream_url:
            pass
        else:
            print("WARNING: 'upstream' remote is missing.")
            print("Please run: git remote add upstream <original-repo-url>")

        target_repo = info.repo
        new_origin_url = _construct_remote_url(platform, user, target_repo)

        if origin_url != new_origin_url:
            print(f"Updating 'origin' to: {new_origin_url}")
            repo.set_remote_url("origin", new_origin_url)

    else:
        # Origin is Upstream
        print(f"Detected 'origin' likely belongs to upstream.")

        target_repo = info.repo
        new_origin_url = _construct_remote_url(platform, user, target_repo)

        existing_upstream = get_first_remote_url("upstream")
        if existing_upstream == origin_url:
            print("  - 'upstream' is already configured correctly.")
            existing_origin = get_first_remote_url("origin")
            if existing_origin == new_origin_url:
                print("  - 'origin' (fork) is already configured correctly.")
                return

        if existing_upstream:
            print("'upstream' already exists.")
        else:
            print("Renaming 'origin' to 'upstream'...")
            repo.rename_remote("origin", "upstream")

        # After rename, 'origin' is gone. We need to create it.
        origin_exists = get_first_remote_url("origin")
        if origin_exists:
            if origin_exists == new_origin_url:
                pass  # Idempotent
            else:
                print(
                    "'origin' exists but points to something else. "
                    "Skipping fork creation."
                )
        else:
            print(f"Adding 'origin' (fork): {new_origin_url}")
            repo.add_remote("origin", new_origin_url)


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
