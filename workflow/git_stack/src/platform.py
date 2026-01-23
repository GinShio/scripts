"""Platform abstraction for git hosting services (GitHub/GitLab)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Union

from core.git_remotes import RemoteInfo, parse_remote_url

from .git import get_config, run_git


class PlatformInterface(Protocol):
    def sync_mr(self, branch: str, base_branch: str, draft: bool = False) -> None: ...

    def get_item_label(self) -> str: ...
    def check_auth(self) -> bool: ...
    def get_mr_description(self, number: str) -> Optional[str]: ...
    def update_mr_description(self, number: str, body: str) -> None: ...
    def get_mr(self, branch: str, state: str = "open") -> Optional[Dict]: ...


def get_remote_url() -> str:
    """Get the fetch URL of the 'origin' remote."""
    url = run_git(["config", "--get", "remote.origin.url"], check=False)
    if url:
        return url

    remotes = run_git(["remote"], check=False).splitlines()
    if remotes:
        return run_git(["config", "--get", f"remote.{remotes[0]}.url"], check=False)
    return ""


class GitHubPlatform:
    def __init__(self, info: RemoteInfo) -> None:
        self.info = info
        self.repo = info.project_path
        self.owner = info.owner

        # Priority:
        # 1. stack.<host>.token (Specific GHE host)
        # 2. stack.github.token (General GitHub)
        # 3. stack.token (Global)
        # 4. ENV vars

        self.token = (
            get_config(f"stack.{info.host}.token")
            or get_config("stack.github.token")
            or get_config("stack.token")
            or os.environ.get("GITHUB_TOKEN")
        )

    def get_item_label(self) -> str:
        return "PR"

    def check_auth(self) -> bool:
        return bool(self.repo and self.token)

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Any:
        # Determine API Base URL
        # Standard GitHub
        if self.info.host in (
            "github.com",
            "www.github.com",
            "api.github.com",
            "ssh.github.com",
        ):
            api_base = "https://api.github.com"
        else:
            # GitHub Enterprise (GHE) default
            # Check config for overrides: stack.<host>.api-url
            api_base = get_config(f"stack.{self.info.host}.api-url")
            if not api_base:
                # Default GHE API path: https://hostname/api/v3
                # Attempt to guess protocol? Default to https
                api_base = f"https://{self.info.host}/api/v3"

        url = f"{api_base}/repos/{self.repo}/{path}"
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "git-stack-tool",
        }

        body = json.dumps(data).encode("utf-8") if data else None

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            raise e

    def get_mr(self, branch: str, state: str = "open") -> Optional[Dict]:
        head_query = f"{self.owner}:{branch}"
        try:
            params = urllib.parse.urlencode({"head": head_query, "state": state})
            data = self._request("GET", f"pulls?{params}")
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
        except Exception:
            pass
        return None

    def create_mr(self, branch: str, base: str, draft: bool = True) -> Optional[Dict]:
        print(f"Creating PR for {branch} (base: {base})...")
        data = {
            "title": branch,
            "head": branch,
            "base": base,
            "body": "Stack PR managed by git-stack.",
            "draft": draft,
        }
        try:
            res = self._request("POST", "pulls", data)
            print(f"Created PR #{res['number']}: {res['html_url']}")
            return res
        except Exception as e:
            print(f"Failed to create PR: {e}")
            return None

    def update_mr_base(self, pr_number: int, new_base: str) -> None:
        print(f"Updating MR #{pr_number} base to {new_base}...")
        try:
            self._request("PATCH", f"pulls/{pr_number}", {"base": new_base})
        except Exception as e:
            print(f"Failed to update PR base: {e}")

    def sync_mr(self, branch: str, base_branch: str, draft: bool = False) -> None:
        if not self.check_auth():
            return

        # 1. Check for open PR
        pr = self.get_mr(branch, state="open")
        if pr:
            current_base = pr["base"]["ref"]
            if current_base != base_branch:
                self.update_mr_base(pr["number"], base_branch)
            return

        # 2. Check for ANY PR (merged/closed) to avoid duplication
        # GitHub API 'all' includes open, closed, merged
        pr_any = self.get_mr(branch, state="all")
        if pr_any:
            allow_create = False
            state = pr_any.get("state", "closed")

            # Check date if merged/closed
            # format: 2011-01-26T19:01:12Z
            closed_at_str = pr_any.get("closed_at")
            if closed_at_str:
                try:
                    closed_at = datetime.strptime(
                        closed_at_str, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                    days_diff = (datetime.now(timezone.utc) - closed_at).days
                    if days_diff > 180:
                        allow_create = True
                except Exception:
                    pass

            if not allow_create:
                print(
                    f"  [Info] Branch '{branch}' already has a {state} PR #{pr_any['number']}. Skipping creation."
                )
                return

        # 3. Create new if none exist
        self.create_mr(branch, base_branch, draft=draft)

    def get_mr_description(self, number: str) -> Optional[str]:
        try:
            data = self._request("GET", f"pulls/{number}")
            return data.get("body", "")
        except Exception:
            return None

    def update_mr_description(self, number: str, body: str) -> None:
        try:
            self._request("PATCH", f"pulls/{number}", {"body": body})
        except Exception as e:
            print(f"Failed to update MR description: {e}")


class GitLabPlatform:
    def __init__(self, info: RemoteInfo) -> None:
        self.host = info.host
        self.project_path = info.project_path
        # project_id needs to be URL encoded
        self.project_id = urllib.parse.quote(self.project_path, safe="")

        self.token = (
            get_config("stack.gitlab.token")
            or get_config("stack.token")
            or os.environ.get("GITLAB_TOKEN")
        )

    def get_item_label(self) -> str:
        return "MR"

    def check_auth(self) -> bool:
        return bool(self.project_path and self.token)

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Any:
        # If host is just domain name, prepend https://
        prefix = "https://" if "://" not in self.host else ""
        url = f"{prefix}{self.host}/api/v4/projects/{self.project_id}/{path}"
        headers = {"PRIVATE-TOKEN": self.token, "Content-Type": "application/json"}

        body = json.dumps(data).encode("utf-8") if data else None

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            raise e

    def get_mr(self, branch: str, state: str = "opened") -> Optional[Dict]:
        try:
            params = urllib.parse.urlencode({"source_branch": branch, "state": state})
            data = self._request("GET", f"merge_requests?{params}")
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
        except Exception:
            pass
        return None

    def create_mr(self, branch: str, base: str, draft: bool = True) -> Optional[Dict]:
        print(f"Creating MR for {branch} (base: {base})...")
        title = branch
        if draft:
            title = f"Draft: {title}"

        data = {
            "source_branch": branch,
            "target_branch": base,
            "title": title,
            "description": "Stack MR managed by git-stack.",
            "remove_source_branch": True,
        }
        try:
            res = self._request("POST", "merge_requests", data)
            print(f"Created MR !{res['iid']}: {res['web_url']}")
            return res
        except Exception as e:
            print(f"Failed to create MR: {e}")
            return None

    def update_mr_base(self, mr_iid: int, new_base: str) -> None:
        print(f"Updating MR !{mr_iid} base to {new_base}...")
        try:
            self._request(
                "PUT", f"merge_requests/{mr_iid}", {"target_branch": new_base}
            )
        except Exception as e:
            print(f"Failed to update MR base: {e}")

    def sync_mr(self, branch: str, base_branch: str, draft: bool = False) -> None:
        if not self.check_auth():
            return

        # 1. Check open
        mr = self.get_mr(branch, state="opened")
        if mr:
            current_target = mr["target_branch"]
            if current_target != base_branch:
                self.update_mr_base(mr["iid"], base_branch)
            return

        # 2. Check all/merged/closed
        # GitLab API state can be: opened, closed, locked, merged
        # We check merged and closed to be safe
        mr_any = self.get_mr(branch, state="merged") or self.get_mr(
            branch, state="closed"
        )
        if mr_any:
            allow_create = False
            state = mr_any.get("state", "closed")

            # Check date if merged/closed
            # format: 2011-01-26T19:01:12.123Z or similar ISO
            closed_at_str = mr_any.get("merged_at") or mr_any.get("closed_at")
            if closed_at_str:
                try:
                    # GitLab ISO strings can be tricky, often have .000Z
                    # Simple parse attempt
                    if "." in closed_at_str:
                        # Strip partial seconds for simplicity
                        closed_at_str = closed_at_str.split(".")[0]
                    closed_at = datetime.strptime(
                        closed_at_str, "%Y-%m-%dT%H:%M:%S"
                    ).replace(tzinfo=timezone.utc)
                    days_diff = (datetime.now(timezone.utc) - closed_at).days
                    if days_diff > 180:
                        allow_create = True
                except Exception:
                    # If date parsing fails, default to blocking to be safe
                    pass

            if not allow_create:
                print(
                    f"  [Info] Branch '{branch}' already has a {state} MR !{mr_any['iid']}. Skipping creation."
                )
                return

        # 3. Create
        self.create_mr(branch, base_branch, draft=draft)

    def get_mr_description(self, number: str) -> Optional[str]:
        try:
            data = self._request("GET", f"merge_requests/{number}")
            return data.get("description", "")
        except Exception:
            return None

    def update_mr_description(self, number: str, body: str) -> None:
        try:
            self._request("PUT", f"merge_requests/{number}", {"description": body})
        except Exception as e:
            print(f"Failed to update MR description: {e}")


class GiteaPlatform(GitHubPlatform):
    """
    Gitea/Codeberg platform support.
    Gitea API v1 is mostly compatible with GitHub API.
    """

    def __init__(self, info: RemoteInfo) -> None:
        self.info = info
        self.host = info.host
        self.repo = info.project_path
        self.owner = info.owner

        self.token = (
            get_config("stack.gitea.token")
            or get_config("stack.codeberg.token")
            or get_config("stack.token")
            or os.environ.get("GITEA_TOKEN")
            or os.environ.get("CODEBERG_TOKEN")
        )

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Any:
        # If host is just domain name, prepend https://
        prefix = "https://" if "://" not in self.host else ""
        # Gitea API v1 prefix
        url = f"{prefix}{self.host}/api/v1/repos/{self.repo}/{path}"

        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "git-stack-tool",
        }

        body = json.dumps(data).encode("utf-8") if data else None

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            raise e


class BitbucketPlatform:
    """Bitbucket Cloud support (API v2)."""

    def __init__(self, info: RemoteInfo) -> None:
        self.workspace = info.owner
        self.repo_slug = info.repo
        self.token = (
            get_config("stack.bitbucket.token")
            or get_config("stack.token")
            or os.environ.get("BITBUCKET_TOKEN")
        )
        # Bitbucket usually requires username if using App Password
        self.username = (
            get_config("stack.bitbucket.user")
            or get_config("user.name")
            or os.environ.get("BITBUCKET_USER")
        )

    def get_item_label(self) -> str:
        return "PR"

    def check_auth(self) -> bool:
        return bool(self.token)

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Any:
        # API: https://api.bitbucket.org/2.0/repositories/{workspace}/{repo_slug}/...
        url = f"https://api.bitbucket.org/2.0/repositories/{self.workspace}/{self.repo_slug}/{path}"

        # Basic Auth for App Password
        import base64

        auth_str = f"{self.username}:{self.token}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()

        headers = {
            "Authorization": f"Basic {b64_auth}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        body = json.dumps(data).encode("utf-8") if data else None

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            # Bitbucket specific error handling could go here
            raise e

    def get_mr(self, branch: str, state: str = "OPEN") -> Optional[Dict]:
        # Bitbucket filtering: q=source.branch.name="branch" AND state="OPEN"
        # state values: OPEN, MERGED, DECLINED
        bb_state = state.upper()
        if bb_state == "ALL":
            # No easy ALL in one query if filters are strict?
            # Actually we can just omit state or use OR?
            # Bitbucket API q parameter is powerful.
            query = f'source.branch.name="{branch}"'
        elif bb_state == "OPENED":
            bb_state = "OPEN"
            query = f'source.branch.name="{branch}" AND state="{bb_state}"'
        else:
            if bb_state == "CLOSED":
                bb_state = "DECLINED"  # Sort of mapping
            query = f'source.branch.name="{branch}" AND state="{bb_state}"'

        params = urllib.parse.urlencode({"q": query})
        try:
            data = self._request("GET", f"pullrequests?{params}")
            if data and "values" in data and len(data["values"]) > 0:
                return data["values"][0]
        except Exception:
            pass
        return None

    def create_mr(self, branch: str, base: str, draft: bool = False) -> Optional[Dict]:
        print(f"Creating PR for {branch} (base: {base})...")
        # Bitbucket doesn't strictly support 'draft' in the same way (no draft field in create),
        # but you can put [WIP] in title.
        title = branch
        if draft:
            title = f"[Draft] {title}"

        data = {
            "title": title,
            "source": {"branch": {"name": branch}},
            "destination": {"branch": {"name": base}},
        }
        try:
            res = self._request("POST", "pullrequests", data)
            print(f"Created PR #{res['id']}: {res['links']['html']['href']}")
            return res
        except Exception as e:
            print(f"Failed to create PR: {e}")
            return None

    def sync_mr(self, branch: str, base_branch: str, draft: bool = False) -> None:
        if not self.check_auth():
            return

        pr = self.get_mr(branch, state="OPEN")
        if pr:
            # Update base not always simple in BB?
            # PUT /pullrequests/{id}
            current_base = pr["destination"]["branch"]["name"]
            if current_base != base_branch:
                print(f"Updating PR #{pr['id']} base to {base_branch}...")
                try:
                    self._request(
                        "PUT",
                        f"pullrequests/{pr['id']}",
                        {
                            "destination": {"branch": {"name": base_branch}},
                            "title": pr["title"],  # Required field sometimes?
                        },
                    )
                except Exception as e:
                    print(f"dFailed to update PR base: {e}")
            return

        # Check merged/declined
        pr_any = self.get_mr(branch, state="MERGED") or self.get_mr(
            branch, state="DECLINED"
        )
        if pr_any:
            msg = f"  [Info] Branch '{branch}' already has a {pr_any['state']} PR #{pr_any['id']}. Skipping creation."
            print(msg)
            return

        self.create_mr(branch, base_branch, draft=draft)

    def get_mr_description(self, number: str) -> Optional[str]:
        try:
            data = self._request("GET", f"pullrequests/{number}")
            return data.get("description", "")
        except Exception:
            return None

    def update_mr_description(self, number: str, body: str) -> None:
        try:
            # Need to provide title to update?
            data = self._request("GET", f"pullrequests/{number}")
            title = data["title"]
            self._request(
                "PUT", f"pullrequests/{number}", {"title": title, "description": body}
            )
        except Exception as e:
            print(f"Failed to update MR description: {e}")


class AzurePlatform:
    """Azure DevOps support (API v7.0)."""

    def __init__(self, info: RemoteInfo) -> None:
        self.org = (
            info.host.split(
                # Heuristic
                "."
            )[0]
            if "dev.azure.com" in info.host
            else info.owner.split("/")[0]
        )
        # Correctly parsing Azure URLs is hard.
        # dev.azure.com/ORG/PROJECT/_git/REPO
        # visualstudio.com/DefaultCollection/_git/REPO

        # Let's rely on info.owner/repo from git_remotes logic?
        # git_remotes logic:
        # url: https://dev.azure.com/myorg/myproject/_git/myrepo
        # domain: dev.azure.com
        # path: myorg/myproject/_git/myrepo
        # repo: myrepo
        # owner: myorg/myproject/_git  <-- Wait, the split logic might be naive for Azure
        # Azure structure: Organization / Project / Repo

        # We need more robust Azure parsing in platform or assume clean inputs.
        # Assuming owner contains "org/project" or we parse it out.

        self.project = (
            info.owner
        )  # usually "org/project" or just "project" if using old style?
        self.repo = info.repo

        self.token = (
            get_config("stack.azure.token")
            or get_config("stack.token")
            or os.environ.get("AZURE_DEVOPS_TOKEN")
        )

    def get_item_label(self) -> str:
        return "PR"

    def check_auth(self) -> bool:
        return bool(self.token)

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Any:
        # url: https://dev.azure.com/{organization}/{project}/_apis/git/repositories/{repositoryId}/...
        # We need to construct base URL.
        # If Host found in git_remotes is 'dev.azure.com', we might need to handle the path prefix.

        # Simplified assumption for standard dev.azure.com structure:
        # info.owner = "org/project" (cleaned in git_remotes? "org/project/_git"?)

        # Let's try to detect if '_git' is in owner and strip it
        clean_owner = self.project.replace("/_git", "")
        if "/" in clean_owner:
            org, project = clean_owner.split("/", 1)
            base_url = f"https://dev.azure.com/{org}/{project}"
        else:
            # Fallback or maybe older visualstudio.com
            # https://{org}.visualstudio.com/{project}
            base_url = f"https://dev.azure.com/{clean_owner}"  # Very risky

        url = f"{base_url}/_apis/git/repositories/{self.repo}/{path}?api-version=7.0"

        import base64

        # PAT requires empty username
        auth_str = f":{self.token}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()

        headers = {
            "Authorization": f"Basic {b64_auth}",
            "Content-Type": "application/json",
        }

        body = json.dumps(data).encode("utf-8") if data else None

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            raise e

    def get_mr(self, branch: str, state: str = "active") -> Optional[Dict]:
        # state: active, completed, abandoned, all?
        az_state = "active"
        if state in ("merged", "closed"):
            az_state = "completed"  # or abandoned

        # searchCriteria.sourceRefName=refs/heads/{branch}
        # searchCriteria.status={state}
        query = urllib.parse.urlencode(
            {
                "searchCriteria.sourceRefName": f"refs/heads/{branch}",
                "searchCriteria.status": az_state,
            }
        )
        try:
            data = self._request("GET", f"pullrequests?{query}")
            if data and "value" in data and len(data["value"]) > 0:
                return data["value"][0]
        except Exception:
            pass
        return None

    def create_mr(self, branch: str, base: str, draft: bool = False) -> Optional[Dict]:
        print(f"Creating Azure PR for {branch}...")
        data = {
            "sourceRefName": f"refs/heads/{branch}",
            "targetRefName": f"refs/heads/{base}",
            "title": branch,
            "description": "Stack PR managed by git-stack.",
            "isDraft": draft,
        }
        try:
            res = self._request("POST", "pullrequests", data)
            # webUrl
            url = res.get("webUrl") or res.get("url")  # webUrl is browser link
            print(f"Created PR #{res['pullRequestId']}: {url}")
            return res
        except Exception as e:
            print(f"Failed to create PR: {e}")
            return None

    def sync_mr(self, branch: str, base_branch: str, draft: bool = False) -> None:
        if not self.check_auth():
            return

        pr = self.get_mr(branch, state="active")
        if pr:
            # Check target
            current_target = pr["targetRefName"]  # refs/heads/main
            target_ref = f"refs/heads/{base_branch}"
            if current_target != target_ref:
                self._request(
                    "PATCH",
                    f"pullrequests/{pr['pullRequestId']}",
                    {"targetRefName": target_ref},
                )
            return

        # Closed/Merged
        pr_any = self.get_mr(branch, state="completed")
        if pr_any:
            print(
                f"  [Info] Branch '{branch}' has completed PR #{pr_any['pullRequestId']}. Skipping."
            )
            return

        self.create_mr(branch, base_branch, draft=draft)

    # Descriptions for Azure are 'description' field
    def get_mr_description(self, number: str) -> Optional[str]:
        try:
            data = self._request("GET", f"pullrequests/{number}")
            return data.get("description", "")
        except Exception:
            return None

    def update_mr_description(self, number: str, body: str) -> None:
        try:
            self._request("PATCH", f"pullrequests/{number}", {"description": body})
        except Exception as e:
            print(f"Failed to update description: {e}")


def get_platform() -> Optional[PlatformInterface]:
    url = get_remote_url()
    if not url:
        return None

    info = parse_remote_url(url)
    if not info:
        return None

    # Determine service type based on RemoteInfo
    if info.is_gitlab:
        pt = GitLabPlatform(info)
        if pt.check_auth():
            return pt

    if info.is_gitea or info.is_codeberg:
        pt = GiteaPlatform(info)
        if pt.check_auth():
            return pt

    if info.is_bitbucket:
        pt = BitbucketPlatform(info)
        if pt.check_auth():
            return pt

    if info.service == info.service.AZURE:
        pt = AzurePlatform(info)
        if pt.check_auth():
            return pt

    # Default to GitHub
    pt = GitHubPlatform(info)
    if pt.check_auth():
        return pt

    return None
