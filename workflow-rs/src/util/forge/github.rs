//! GitHub (and GitHub Enterprise) merge requests — "pull requests" in its words.
//!
//! This module is pure mapping: GitHub's REST shapes in, normalized
//! [`MergeRequest`]s out. The only judgement calls are the API base (the public
//! host has a dedicated `api.github.com`, Enterprise serves under `/api/v3`) and
//! the cross-fork head form (`owner:branch`).

use serde_json::{json, Value};

use super::{encode, pick, request, Auth, Forge, MergeRequest, MrState, NewMr, StateFilter};
use crate::util::remote::RemoteInfo;

pub struct GitHub {
    api_base: String,
    project: String,
    /// The repo we target — also the head owner for a same-repo MR.
    owner: String,
    /// Set only when the head lives in a different fork.
    head_owner: Option<String>,
    auth: Auth,
}

impl GitHub {
    pub fn new(
        target: RemoteInfo,
        head_owner: Option<String>,
        token: String,
        api_url_override: Option<String>,
    ) -> Self {
        let api_base = if matches!(target.host.as_str(), "github.com" | "www.github.com") {
            "https://api.github.com".to_owned()
        } else {
            api_url_override.unwrap_or_else(|| format!("https://{}/api/v3", target.host))
        };
        Self {
            api_base,
            owner: target.owner.clone(),
            project: target.project_path(),
            head_owner,
            auth: Auth::Bearer(token),
        }
    }

    /// The head reference for *creating*: `owner:branch` across a fork, plain
    /// `branch` within the same repo.
    fn head_ref(&self, branch: &str) -> String {
        match &self.head_owner {
            Some(owner) => format!("{owner}:{branch}"),
            None => branch.to_owned(),
        }
    }

    /// The head used to *filter* a search. GitHub's `head=` query only matches in
    /// the `owner:branch` form, so it must always be qualified — with the fork
    /// owner when forked, otherwise the repo's own owner.
    fn head_query(&self, branch: &str) -> String {
        let owner = self.head_owner.as_deref().unwrap_or(&self.owner);
        format!("{owner}:{branch}")
    }

    fn pulls_url(&self) -> String {
        format!("{}/repos/{}/pulls", self.api_base, self.project)
    }
}

fn parse_pull(v: &Value) -> Option<MergeRequest> {
    let number = v["number"].as_u64()?;
    // GitHub reports a merged PR as `state: "closed"` with a non-null
    // `merged_at`, so the two have to be read together to tell them apart.
    let merged = v.get("merged_at").is_some_and(|m| !m.is_null());
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

impl Forge for GitHub {
    fn labels(&self) -> (&'static str, &'static str) {
        ("PR", "#")
    }

    fn find(&self, branch: &str, state: StateFilter) -> anyhow::Result<Option<MergeRequest>> {
        let url = format!(
            "{}?head={}&state=all&per_page=50",
            self.pulls_url(),
            encode(&self.head_query(branch)),
        );
        let v = request("GET", &url, &self.auth, None)?;
        let candidates: Vec<MergeRequest> = v
            .as_array()
            .map(|arr| arr.iter().filter_map(parse_pull).collect())
            .unwrap_or_default();
        Ok(pick(&candidates, state))
    }

    fn create(&self, req: &NewMr) -> anyhow::Result<MergeRequest> {
        let body = json!({
            "title": req.title,
            "head": self.head_ref(&req.branch),
            "base": req.base,
            "body": req.body,
            "draft": req.draft,
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
