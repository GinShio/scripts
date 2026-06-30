//! Gitea / Forgejo / Codeberg merge requests.
//!
//! The API is GitHub-shaped but not identical, and the two differences are the
//! ones that bite: a personal token authenticates with the `token` scheme (not
//! bearer), and there is no draft *field* — a draft is signalled by a `WIP:`
//! title prefix. Listing-then-filtering replaces GitHub's head query because the
//! list endpoint is the dependable one across Gitea versions.

use serde_json::{json, Value};

use super::{pick, request, Auth, Forge, MergeRequest, MrState, NewMr, StateFilter};
use crate::util::remote::RemoteInfo;

const WIP_PREFIX: &str = "WIP: ";

pub struct Gitea {
    api_base: String,
    project: String,
    head_owner: Option<String>,
    auth: Auth,
}

impl Gitea {
    pub fn new(
        target: RemoteInfo,
        head_owner: Option<String>,
        token: String,
        api_url_override: Option<String>,
    ) -> Self {
        let api_base =
            api_url_override.unwrap_or_else(|| format!("https://{}/api/v1", target.host));
        Self {
            api_base,
            project: target.project_path(),
            head_owner,
            auth: Auth::Token(token),
        }
    }

    fn head_ref(&self, branch: &str) -> String {
        match &self.head_owner {
            Some(owner) => format!("{owner}:{branch}"),
            None => branch.to_owned(),
        }
    }

    fn pulls_url(&self) -> String {
        format!("{}/repos/{}/pulls", self.api_base, self.project)
    }
}

fn parse_pull(v: &Value) -> Option<MergeRequest> {
    let number = v["number"].as_u64()?;
    let merged = v["merged"].as_bool().unwrap_or(false);
    let state = match v["state"].as_str().unwrap_or("open") {
        _ if merged => MrState::Merged,
        "closed" => MrState::Closed,
        _ => MrState::Open,
    };
    Some(MergeRequest {
        id: number.to_string(),
        display: format!("#{number}"),
        state,
        base: v["base"]["ref"].as_str().unwrap_or_default().to_owned(),
        head_sha: v["head"]["sha"].as_str().map(str::to_owned),
        body: v["body"].as_str().unwrap_or_default().to_owned(),
        web_url: v["html_url"].as_str().unwrap_or_default().to_owned(),
    })
}

impl Forge for Gitea {
    fn labels(&self) -> (&'static str, &'static str) {
        ("PR", "#")
    }

    fn find(&self, branch: &str, state: StateFilter) -> anyhow::Result<Option<MergeRequest>> {
        let url = format!("{}?state=all&limit=50", self.pulls_url());
        let v = request("GET", &url, &self.auth, None)?;
        // Gitea's list doesn't filter by head, so we narrow to our branch here;
        // the head's owner lives in a separate field, so matching `head.ref`
        // against the bare branch name is correct for both same-repo and forks.
        let candidates: Vec<MergeRequest> = v
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter(|item| item["head"]["ref"].as_str() == Some(branch))
                    .filter_map(parse_pull)
                    .collect()
            })
            .unwrap_or_default();
        Ok(pick(&candidates, state))
    }

    fn create(&self, req: &NewMr) -> anyhow::Result<MergeRequest> {
        let title = if req.draft {
            format!("{WIP_PREFIX}{}", req.title)
        } else {
            req.title.clone()
        };
        let body = json!({
            "title": title,
            "head": self.head_ref(&req.branch),
            "base": req.base,
            "body": req.body,
        });
        let v = request("POST", &self.pulls_url(), &self.auth, Some(&body))?;
        parse_pull(&v).ok_or_else(|| anyhow::anyhow!("unexpected create response: {v}"))
    }

    fn set_base(&self, id: &str, base: &str) -> anyhow::Result<()> {
        let url = format!("{}/{}", self.pulls_url(), id);
        request("PATCH", &url, &self.auth, Some(&json!({ "base": base })))?;
        Ok(())
    }

    fn set_body(&self, id: &str, body: &str) -> anyhow::Result<()> {
        let url = format!("{}/{}", self.pulls_url(), id);
        request("PATCH", &url, &self.auth, Some(&json!({ "body": body })))?;
        Ok(())
    }
}
