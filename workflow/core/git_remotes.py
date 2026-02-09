"""Remote URL parsing and normalization."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GitService(Enum):
    """Supported git hosting services."""

    AUTO = "auto"
    GITHUB = "github"
    GITLAB = "gitlab"
    GITEA = "gitea"
    CODEBERG = "codeberg"
    BITBUCKET = "bitbucket"
    AZURE = "azure"


@dataclass(frozen=True)
class RemoteInfo:
    host: str
    owner: str
    repo: str
    service: GitService

    @property
    def project_path(self) -> str:
        """Return 'owner/repo' string."""
        return f"{self.owner}/{self.repo}"

    @property
    def is_github(self) -> bool:
        return self.service == GitService.GITHUB

    @property
    def is_gitlab(self) -> bool:
        return self.service == GitService.GITLAB

    @property
    def is_gitea(self) -> bool:
        return self.service == GitService.GITEA

    @property
    def is_codeberg(self) -> bool:
        return self.service == GitService.CODEBERG

    @property
    def is_bitbucket(self) -> bool:
        return self.service == GitService.BITBUCKET

    @property
    def is_azure(self) -> bool:
        return self.service == GitService.AZURE


def resolve_ssh_alias(host: str) -> str:
    """Resolve SSH alias using 'ssh -G'."""
    try:
        # ssh -G <host> prints config. We look for 'hostname' key.
        # Use a short timeout to avoid hanging if SSH is misconfigured.
        proc = subprocess.run(
            ["ssh", "-G", host], capture_output=True, text=True, timeout=2
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if line.lower().startswith("hostname "):
                    return line.split(" ", 1)[1].strip()
    except Exception:
        pass
    return host


def normalize_domain(domain: str) -> str:
    """Normalize specialized domains to their main service domain."""
    domain = domain.lower()
    mapping = {
        "ssh.github.com": "github.com",
        "altssh.gitlab.com": "gitlab.com",
        "ssh.dev.azure.com": "dev.azure.com",
        "vs-ssh.visualstudio.com": "visualstudio.com",
        "altssh.bitbucket.org": "bitbucket.org",
    }
    return mapping.get(domain, domain)


def parse_remote_url(url: str) -> Optional[RemoteInfo]:
    """
    Parse a Git remote URL into (host, owner, repo).
    Handles SSH aliases, scp-syntax, and standard URIs.
    """
    if not url:
        return None

    domain = ""
    path = ""

    # 1. Check for standard SCP-like SSH syntax: user@host:path/to/repo.git
    # Regex: ^(Optional(user)@)?(host):(path)
    # We explicitly exclude strings starting with protocols (http:, https:, ssh:, git:)
    # because `ssh://user@host:port/path` is a URI, not SCP syntax.
    sc_match = re.match(r"^(?:[^@]+@)?([^:]+):(.+)$", url)

    is_uri = any(url.startswith(p) for p in ["http:", "https:", "ssh:", "git:"])

    if sc_match and not is_uri:
        raw_host = sc_match.group(1)
        path = sc_match.group(2)
        resolved_host = resolve_ssh_alias(raw_host)
        domain = normalize_domain(resolved_host)
    else:
        # 2. Generic URI matching
        # Regex: protocol://[user@]host[:port]/path
        match = re.search(
            r"^(?:ssh|git|https?)://(?:[^@/]+@)?([^:/]+)(?::\d+)?/(.+)$", url
        )
        if match:
            raw_host = match.group(1)
            path = match.group(2)
            resolved_host = resolve_ssh_alias(raw_host)
            domain = normalize_domain(resolved_host)

    if domain and path:
        # Cleanup path
        if path.endswith(".git"):
            path = path[:-4]
        path = path.strip("/")

        # Split org/repo
        # We assume the last component is repo, and everything before is "owner" (group/subgroup).
        parts = path.split("/")
        if len(parts) >= 2:
            repo = parts[-1]
            owner = "/".join(parts[:-1])

            service = GitService.AUTO
            if domain in ("github.com", "www.github.com"):
                service = GitService.GITHUB
            elif domain in ("gitlab.com", "www.gitlab.com"):
                service = GitService.GITLAB
            elif "gitlab" in domain:
                service = GitService.GITLAB
            elif domain in ("codeberg.org", "www.codeberg.org"):
                service = GitService.CODEBERG
            elif "gitea" in domain:
                service = GitService.GITEA
            elif domain in ("bitbucket.org", "www.bitbucket.org"):
                service = GitService.BITBUCKET
            elif domain in ("dev.azure.com", "visualstudio.com"):
                service = GitService.AZURE

            return RemoteInfo(host=domain, owner=owner, repo=repo, service=service)

    return None


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        info = parse_remote_url(sys.argv[1])
        if info:
            print(f"{info.host} {info.owner} {info.repo}")


def get_git_config(key: str) -> Optional[str]:
    """Read a git config value."""
    try:
        # Use subprocess directly to avoid dependencies on other modules in core
        res = subprocess.run(
            ["git", "config", "--get", key], capture_output=True, text=True
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return None


def get_platform_host(service_name: str) -> str:
    """Get configured host for a platform, with fallback to built-ins."""
    # 1. Try config
    host = get_git_config(f"platform.{service_name}.host")
    if host:
        return host

    # 2. Fallback
    defaults = {
        "github": "github.com",
        "gitlab": "gitlab.com",
        "gitea": "gitea.com",
        "codeberg": "codeberg.org",
        "bitbucket": "bitbucket.org",
        "azure": "dev.azure.com",
    }
    return defaults.get(service_name, "")


def get_platform_user(service_name: str) -> Optional[str]:
    """Get current username for a specific platform."""
    user = get_git_config(f"platform.{service_name}.user")
    if not user:
        user = get_git_config("user.name")
    return user


def get_platform_page_suffix(service_name: str) -> str:
    """Get platform page suffix (for .io/.page repos)."""
    suffix = get_git_config(f"platform.{service_name}.page-suffix")
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


def construct_remote_url(service_name: str, user: str, repo: str) -> Optional[str]:
    """Construct a remote URL for a given platform."""
    # 1. Check for custom URL format override
    custom_url = get_git_config(f"platform.{service_name}.url")
    if custom_url:
        return custom_url.replace("{user}", user).replace("{repo}", repo)

    # 2. Resolve Host
    host = get_platform_host(service_name)
    if not host:
        # If looks like a domain, use it
        if "." in service_name:
            host = service_name
        else:
            return None

    # 3. Check for SSH Alias override
    ssh_alias = get_git_config(f"platform.{service_name}.ssh-alias")

    # 4. Standard Construction
    if ssh_alias:
        return f"{ssh_alias}:{user}/{repo}.git"

    return f"git@{host}:{user}/{repo}.git"
