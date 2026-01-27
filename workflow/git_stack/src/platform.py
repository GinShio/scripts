"""Platform abstraction for git hosting services (GitHub/GitLab/Gitea/Bitbucket/Azure)."""

from __future__ import annotations

import abc
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol

from core.git_remotes import RemoteInfo, parse_remote_url

from .git import get_config, run_git


class PlatformInterface(Protocol):
    def sync_mr(
        self,
        branch: str,
        base_branch: str,
        draft: bool,
        title: str,
        body: str,
        local_sha: Optional[str] = None,
    ) -> None: ...

    def get_item_label(self) -> str: ...
    def get_item_char(self) -> str: ...
    def check_auth(self) -> bool: ...
    def get_mr_description(self, number: str) -> Optional[str]: ...
    def update_mr_description(self, number: str, body: str) -> None: ...
    def get_mr(
        self, branch: str, state: str = "open", base: Optional[str] = None
    ) -> Optional[Dict]: ...


class BasePlatform(abc.ABC):
    """Base class for all platforms containing common logic."""

    def __init__(self, info: RemoteInfo) -> None:
        self.info = info
        self.token: Optional[str] = None

    @abc.abstractmethod
    def get_item_label(self) -> str:
        return "PR"

    @abc.abstractmethod
    def get_item_char(self) -> str:
        return "#"

    def check_auth(self) -> bool:
        return bool(self.token)

    def _make_request(
        self,
        url: str,
        method: str = "GET",
        data: Optional[Dict] = None,
        headers: Optional[Dict] = None,
    ) -> Any:
        if not headers:
            headers = {}

        if self.token:
            # Default to Bearer/Token auth, override in subclasses if needed
            if "Authorization" not in headers:
                headers["Authorization"] = f"token {self.token}"

        if "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        if "Accept" not in headers:
            headers["Accept"] = "application/json"
        if "User-Agent" not in headers:
            headers["User-Agent"] = "git-stack-tool"

        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            # print(f"HTTP Error {e.code}: {e.reason} for {url}")
            raise e


class GitHubPlatform(BasePlatform):
    def __init__(self, info: RemoteInfo) -> None:
        super().__init__(info)
        self.repo = info.project_path
        self.owner = info.owner
        self.token = (
            get_config(f"stack.{info.host}.token")
            or get_config("stack.github.token")
            or get_config("stack.token")
            or os.environ.get("GITHUB_TOKEN")
        )

    def get_item_label(self) -> str:
        return "PR"

    def get_item_char(self) -> str:
        return "#"

    def check_auth(self) -> bool:
        return bool(self.repo and self.token)

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Any:
        # Determine API Base URL
        if self.info.host in (
            "github.com",
            "www.github.com",
            "api.github.com",
            "ssh.github.com",
        ):
            api_base = "https://api.github.com"
        else:
            # GitHub Enterprise (GHE)
            api_base = get_config(f"stack.{self.info.host}.api-url")
            if not api_base:
                api_base = f"https://{self.info.host}/api/v3"

        url = f"{api_base}/repos/{self.repo}/{path}"

        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        return self._make_request(url, method, data, headers)

    def get_mr(
        self, branch: str, state: str = "open", base: Optional[str] = None
    ) -> Optional[Dict]:
        head_query = f"{self.owner}:{branch}"
        try:
            params = urllib.parse.urlencode({"head": head_query, "state": state})
            data = self._request("GET", f"pulls?{params}")
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
        except Exception:
            pass
        return None

    def create_mr(
        self,
        branch: str,
        base: str,
        draft: bool,
        title: str,
        body: str,
    ) -> Optional[Dict]:
        print(f"Creating PR for {branch} (base: {base})...")
        data = {
            "title": title,
            "head": branch,
            "base": base,
            "body": body,
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

    def sync_mr(
        self,
        branch: str,
        base_branch: str,
        draft: bool,
        title: str,
        body: str,
        local_sha: Optional[str] = None,
    ) -> None:
        if not self.check_auth():
            return

        # 1. Check for open PR
        pr = self.get_mr(branch, state="open", base=base_branch)
        if pr:
            current_base = pr["base"]["ref"]
            if current_base != base_branch:
                self.update_mr_base(pr["number"], base_branch)
            return

        # 2. Check for ANY PR (merged/closed)
        pr_any = self.get_mr(branch, state="all", base=base_branch)
        if pr_any:
            allow_create = False
            state = pr_any.get("state", "closed")

            # SHA Check
            pr_head_sha = pr_any.get("head", {}).get("sha")
            if local_sha and pr_head_sha and local_sha != pr_head_sha:
                allow_create = True
            else:
                # Date Check
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

        # 3. Create new
        self.create_mr(branch, base_branch, draft=draft, title=title, body=body)

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


class GiteaPlatform(BasePlatform):
    """
    Gitea/Codeberg platform support.
    Explicit implementation to avoid GitHub subclassing issues.
    """

    def __init__(self, info: RemoteInfo) -> None:
        super().__init__(info)
        self.repo = info.project_path
        self.owner = info.owner
        self.host = info.host
        self.token = (
            get_config("stack.gitea.token")
            or get_config("stack.codeberg.token")
            or get_config("stack.token")
            or os.environ.get("GITEA_TOKEN")
            or os.environ.get("CODEBERG_TOKEN")
        )

    def get_item_label(self) -> str:
        return "PR"

    def get_item_char(self) -> str:
        return "#"

    def check_auth(self) -> bool:
        return bool(self.repo and self.token)

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Any:
        prefix = "https://" if "://" not in self.host else ""
        url = f"{prefix}{self.host}/api/v1/repos/{self.repo}/{path}"
        return self._make_request(url, method, data)

    def get_mr(
        self, branch: str, state: str = "open", base: Optional[str] = None
    ) -> Optional[Dict]:
        # Gitea API v1: GET /repos/{owner}/{repo}/pulls
        # If base is provided, use the new optimized endpoint if possible
        # GET /repos/{owner}/{repo}/pulls/{base}/{head}
        if base:
            try:
                # The endpoint returns a single PR object if found
                # We assume head is just the branch name in the same repo
                # Note: The endpoint might return 404 if not found
                path = f"pulls/{base}/{branch}"
                # If state is specified, we might need to check it after fetching,
                # or pass it as query param if supported (docs said ?state=open proposed)
                # Let's try passing state if it's not 'all'
                if state != "all":
                    # Note: urllib.parse.quote might be needed for branch names with slashes
                    # But Gitea usually handles them in path if properly encoded?
                    # Let's encode components
                    safe_base = urllib.parse.quote(base, safe="")
                    safe_head = urllib.parse.quote(branch, safe="")
                    path = f"pulls/{safe_base}/{safe_head}"

                    # Add state param just in case
                    params = urllib.parse.urlencode({"state": state})
                    path += f"?{params}"
                else:
                    safe_base = urllib.parse.quote(base, safe="")
                    safe_head = urllib.parse.quote(branch, safe="")
                    path = f"pulls/{safe_base}/{safe_head}"

                data = self._request("GET", path)
                if data and isinstance(data, dict):
                    # Verify state if needed (though API might have filtered)
                    if state == "all" or data.get("state") == state:
                        return data
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    # If it's not 404, it might be a real error, or 404 just means no PR
                    pass
            except Exception:
                pass

        # Fallback to list filtering
        try:
            params = urllib.parse.urlencode({"state": state})
            data = self._request("GET", f"pulls?{params}")
            if data and isinstance(data, list):
                for pr in data:
                    if pr.get("head", {}).get("ref") == branch:
                        if base and pr.get("base", {}).get("ref") != base:
                            continue
                        return pr
        except Exception:
            pass
        return None

    def create_mr(
        self,
        branch: str,
        base: str,
        draft: bool,
        title: str,
        body: str,
    ) -> Optional[Dict]:
        print(f"Creating PR for {branch} (base: {base})...")
        data = {
            "title": title,
            "head": branch,
            "base": base,
            "body": body,
        }

        try:
            res = self._request("POST", "pulls", data)
            print(f"Created PR #{res['number']}: {res['html_url']}")
            return res
        except Exception as e:
            print(f"Failed to create PR: {e}")
            return None

    def update_mr_base(self, pr_number: int, new_base: str) -> None:
        print(f"Updating PR #{pr_number} base to {new_base}...")
        try:
            self._request("PATCH", f"pulls/{pr_number}", {"base": new_base})
        except Exception as e:
            print(f"Failed to update PR base: {e}")

    def sync_mr(
        self,
        branch: str,
        base_branch: str,
        draft: bool,
        title: str,
        body: str,
        local_sha: Optional[str] = None,
    ) -> None:
        if not self.check_auth():
            return

        # 1. Check Open
        pr = self.get_mr(branch, state="open", base=base_branch)
        if pr:
            current_base = pr["base"]["ref"]
            if current_base != base_branch:
                self.update_mr_base(pr["number"], base_branch)
            return

        # 2. Check Closed
        pr_any = self.get_mr(branch, state="closed", base=base_branch)
        if pr_any:
            # Logic similar to GitHub: check SHA or Date
            allow_create = False
            pr_head_sha = pr_any.get("head", {}).get("sha")

            if local_sha and pr_head_sha and local_sha != pr_head_sha:
                allow_create = True
            else:
                closed_at_str = pr_any.get("closed_at")
                if closed_at_str:
                    try:
                        # Gitea ISO format
                        closed_at = datetime.strptime(
                            closed_at_str, "%Y-%m-%dT%H:%M:%S%z"
                        )
                        days_diff = (datetime.now(timezone.utc) - closed_at).days
                        if days_diff > 180:
                            allow_create = True
                    except Exception:
                        pass

            if not allow_create:
                print(
                    f"  [Info] Branch '{branch}' already has a closed PR #{pr_any['number']}. Skipping."
                )
                return

        # 3. Create
        self.create_mr(branch, base_branch, draft=draft, title=title, body=body)

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


class GitLabPlatform(BasePlatform):
    def __init__(self, info: RemoteInfo) -> None:
        super().__init__(info)
        self.host = info.host
        self.project_path = info.project_path
        self.project_id = urllib.parse.quote(self.project_path, safe="")
        self.token = (
            get_config("stack.gitlab.token")
            or get_config("stack.token")
            or os.environ.get("GITLAB_TOKEN")
        )

    def get_item_label(self) -> str:
        return "MR"

    def get_item_char(self) -> str:
        return "!"

    def check_auth(self) -> bool:
        return bool(self.project_path and self.token)

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Any:
        prefix = "https://" if "://" not in self.host else ""
        url = f"{prefix}{self.host}/api/v4/projects/{self.project_id}/{path}"
        headers = {"PRIVATE-TOKEN": self.token}
        return self._make_request(url, method, data, headers)

    def get_mr(
        self, branch: str, state: str = "opened", base: Optional[str] = None
    ) -> Optional[Dict]:
        try:
            params = urllib.parse.urlencode({"source_branch": branch, "state": state})
            data = self._request("GET", f"merge_requests?{params}")
            if data and isinstance(data, list) and len(data) > 0:
                return data[0]
        except Exception:
            pass
        return None

    def create_mr(
        self,
        branch: str,
        base: str,
        draft: bool,
        title: str,
        body: str,
    ) -> Optional[Dict]:
        print(f"Creating MR for {branch} (base: {base})...")
        chosen_title = f"Draft: {title}" if draft else title
        data = {
            "source_branch": branch,
            "target_branch": base,
            "title": chosen_title,
            "description": body,
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

    def sync_mr(
        self,
        branch: str,
        base_branch: str,
        draft: bool,
        title: str,
        body: str,
        local_sha: Optional[str] = None,
    ) -> None:
        if not self.check_auth():
            return

        # 1. Check open
        mr = self.get_mr(branch, state="opened", base=base_branch)
        if mr:
            current_target = mr["target_branch"]
            if current_target != base_branch:
                self.update_mr_base(mr["iid"], base_branch)
            return

        # 2. Check merged/closed
        mr_any = self.get_mr(branch, state="merged", base=base_branch) or self.get_mr(
            branch, state="closed", base=base_branch
        )
        if mr_any:
            allow_create = False
            state = mr_any.get("state", "closed")

            mr_sha = mr_any.get("sha")
            if local_sha and mr_sha and local_sha != mr_sha:
                allow_create = True
            else:
                closed_at_str = mr_any.get("merged_at") or mr_any.get("closed_at")
                if closed_at_str:
                    try:
                        if "." in closed_at_str:
                            closed_at_str = closed_at_str.split(".")[0]
                        closed_at = datetime.strptime(
                            closed_at_str, "%Y-%m-%dT%H:%M:%S"
                        ).replace(tzinfo=timezone.utc)
                        days_diff = (datetime.now(timezone.utc) - closed_at).days
                        if days_diff > 180:
                            allow_create = True
                    except Exception:
                        pass

            if not allow_create:
                print(
                    f"  [Info] Branch '{branch}' already has a {state} MR !{mr_any['iid']}. Skipping."
                )
                return

        # 3. Create
        self.create_mr(branch, base_branch, draft=draft, title=title, body=body)

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


class BitbucketPlatform(BasePlatform):
    def __init__(self, info: RemoteInfo) -> None:
        super().__init__(info)
        self.workspace = info.owner
        self.repo_slug = info.repo
        self.token = (
            get_config("stack.bitbucket.token")
            or get_config("stack.token")
            or os.environ.get("BITBUCKET_TOKEN")
        )
        self.username = (
            get_config("stack.bitbucket.user")
            or get_config("user.name")
            or os.environ.get("BITBUCKET_USER")
        )

    def get_item_label(self) -> str:
        return "PR"

    def get_item_char(self) -> str:
        return "#"

    def check_auth(self) -> bool:
        return bool(self.token)

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Any:
        url = f"https://api.bitbucket.org/2.0/repositories/{self.workspace}/{self.repo_slug}/{path}"
        auth_str = f"{self.username}:{self.token}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()
        headers = {"Authorization": f"Basic {b64_auth}"}
        return self._make_request(url, method, data, headers)

    def get_mr(
        self, branch: str, state: str = "OPEN", base: Optional[str] = None
    ) -> Optional[Dict]:
        bb_state = state.upper()
        if bb_state == "OPENED":
            bb_state = "OPEN"
        elif bb_state == "CLOSED":
            bb_state = "DECLINED"

        query = f'source.branch.name="{branch}"'
        if bb_state != "ALL":
            query += f' AND state="{bb_state}"'

        params = urllib.parse.urlencode({"q": query})
        try:
            data = self._request("GET", f"pullrequests?{params}")
            if data and "values" in data and len(data["values"]) > 0:
                return data["values"][0]
        except Exception:
            pass
        return None

    def create_mr(
        self, branch: str, base: str, title: str, body: str, draft: bool = False
    ) -> Optional[Dict]:
        print(f"Creating PR for {branch} (base: {base})...")
        final_title = f"[Draft] {title}" if draft else title
        data = {
            "title": final_title,
            "description": body,
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

    def sync_mr(
        self,
        branch: str,
        base_branch: str,
        draft: bool,
        title: str,
        body: str,
        local_sha: Optional[str] = None,
    ) -> None:
        if not self.check_auth():
            return

        pr = self.get_mr(branch, state="OPEN", base=base_branch)
        if pr:
            current_base = pr["destination"]["branch"]["name"]
            if current_base != base_branch:
                print(f"Updating PR #{pr['id']} base to {base_branch}...")
                try:
                    self._request(
                        "PUT",
                        f"pullrequests/{pr['id']}",
                        {
                            "destination": {"branch": {"name": base_branch}},
                            "title": pr["title"],
                        },
                    )
                except Exception as e:
                    print(f"Failed to update PR base: {e}")
            return

        # Check merged/declined
        pr_any = self.get_mr(branch, state="MERGED", base=base_branch) or self.get_mr(
            branch, state="DECLINED", base=base_branch
        )
        if pr_any:
            # TODO: Implement SHA check for Bitbucket
            print(
                f"  [Info] Branch '{branch}' already has a {pr_any['state']} PR #{pr_any['id']}. Skipping."
            )
            return

        self.create_mr(branch, base_branch, title=title, body=body, draft=draft)

    def get_mr_description(self, number: str) -> Optional[str]:
        try:
            data = self._request("GET", f"pullrequests/{number}")
            return data.get("description", "")
        except Exception:
            return None

    def update_mr_description(self, number: str, body: str) -> None:
        try:
            # Need to provide title to update
            data = self._request("GET", f"pullrequests/{number}")
            title = data["title"]
            self._request(
                "PUT", f"pullrequests/{number}", {"title": title, "description": body}
            )
        except Exception as e:
            print(f"Failed to update MR description: {e}")


class AzurePlatform(BasePlatform):
    def __init__(self, info: RemoteInfo) -> None:
        super().__init__(info)
        self.project = info.owner
        self.repo = info.repo
        self.token = (
            get_config("stack.azure.token")
            or get_config("stack.token")
            or os.environ.get("AZURE_DEVOPS_TOKEN")
        )

    def get_item_label(self) -> str:
        return "PR"

    def get_item_char(self) -> str:
        return "#"

    def check_auth(self) -> bool:
        return bool(self.token)

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Any:
        # Simplified URL construction
        clean_owner = self.project.replace("/_git", "")
        if "/" in clean_owner:
            org, project = clean_owner.split("/", 1)
            base_url = f"https://dev.azure.com/{org}/{project}"
        else:
            base_url = f"https://dev.azure.com/{clean_owner}"

        url = f"{base_url}/_apis/git/repositories/{self.repo}/{path}?api-version=7.0"

        auth_str = f":{self.token}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()
        headers = {"Authorization": f"Basic {b64_auth}"}

        return self._make_request(url, method, data, headers)

    def get_mr(
        self, branch: str, state: str = "active", base: Optional[str] = None
    ) -> Optional[Dict]:
        az_state = "active"
        if state in ("merged", "closed"):
            az_state = "completed"

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

    def create_mr(
        self, branch: str, base: str, title: str, body: str, draft: bool = False
    ) -> Optional[Dict]:
        print(f"Creating Azure PR for {branch}...")
        data = {
            "sourceRefName": f"refs/heads/{branch}",
            "targetRefName": f"refs/heads/{base}",
            "title": title,
            "description": body,
            "isDraft": draft,
        }
        try:
            res = self._request("POST", "pullrequests", data)
            url = res.get("webUrl") or res.get("url")
            print(f"Created PR #{res['pullRequestId']}: {url}")
            return res
        except Exception as e:
            print(f"Failed to create PR: {e}")
            return None

    def sync_mr(
        self,
        branch: str,
        base_branch: str,
        draft: bool,
        title: str,
        body: str,
        local_sha: Optional[str] = None,
    ) -> None:
        if not self.check_auth():
            return

        pr = self.get_mr(branch, state="active", base=base_branch)
        if pr:
            current_target = pr["targetRefName"]
            target_ref = f"refs/heads/{base_branch}"
            if current_target != target_ref:
                self._request(
                    "PATCH",
                    f"pullrequests/{pr['pullRequestId']}",
                    {"targetRefName": target_ref},
                )
            return

        # Closed/Merged
        pr_any = self.get_mr(branch, state="completed", base=base_branch)
        if pr_any:
            # TODO: Implement SHA check for Azure
            print(
                f"  [Info] Branch '{branch}' has completed PR #{pr_any['pullRequestId']}. Skipping."
            )
            return

        self.create_mr(branch, base_branch, title=title, body=body, draft=draft)

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


def get_remote_url() -> str:
    url = run_git(["config", "--get", "remote.origin.url"], check=False)
    if url:
        return url
    remotes = run_git(["remote"], check=False).splitlines()
    if remotes:
        return run_git(["config", "--get", f"remote.{remotes[0]}.url"], check=False)
    return ""


def get_platform() -> Optional[PlatformInterface]:
    url = get_remote_url()
    if not url:
        return None

    info = parse_remote_url(url)
    if not info:
        return None

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
