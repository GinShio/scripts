//! GitLab merge requests.
//!
//! GitLab is the platform whose vocabulary the rest of the tool borrows ("MR",
//! "!"). Two shape differences matter: a project is addressed by a URL-encoded
//! `group/sub/repo` id rather than an `owner/repo` path, and a draft is a
//! `Draft:` title prefix, not a field.
//!
//! Cross-fork MRs (source branch living in a different project) need source/
//! target project ids and are intentionally out of scope here; we operate within
//! the target project, which covers the same-owner stacks this tool is built for.

use serde_json::{json, Value};

use super::{encode, pick, request, Auth, Forge, MergeRequest, MrState, NewMr, StateFilter};
use crate::util::remote::RemoteInfo;

const DRAFT_PREFIX: &str = "Draft: ";

pub struct GitLab {
    api_base: String,
    project_id: String,
    auth: Auth,
}

impl GitLab {
    pub fn new(target: RemoteInfo, token: String, api_url_override: Option<String>) -> Self {
        let api_base =
            api_url_override.unwrap_or_else(|| format!("https://{}/api/v4", target.host));
        Self {
            api_base,
            project_id: encode(&target.project_path()),
            auth: Auth::PrivateToken(token),
        }
    }

    fn mrs_url(&self) -> String {
        format!(
            "{}/projects/{}/merge_requests",
            self.api_base, self.project_id
        )
    }
}

fn parse_mr(v: &Value) -> Option<MergeRequest> {
    let iid = v["iid"].as_u64()?;
    let state = match v["state"].as_str().unwrap_or("opened") {
        "merged" => MrState::Merged,
        "opened" | "locked" => MrState::Open,
        _ => MrState::Closed,
    };
    Some(MergeRequest {
        id: iid.to_string(),
        display: format!("!{iid}"),
        state,
        base: v["target_branch"].as_str().unwrap_or_default().to_owned(),
        head_sha: v["sha"].as_str().map(str::to_owned),
        body: v["description"].as_str().unwrap_or_default().to_owned(),
        web_url: v["web_url"].as_str().unwrap_or_default().to_owned(),
    })
}

impl Forge for GitLab {
    fn labels(&self) -> (&'static str, &'static str) {
        ("MR", "!")
    }

    fn find(&self, branch: &str, state: StateFilter) -> anyhow::Result<Option<MergeRequest>> {
        // Match by source branch only — not target — so a drifted base is still
        // found and can be corrected.
        let url = format!(
            "{}?source_branch={}&state=all",
            self.mrs_url(),
            encode(branch),
        );
        let v = request("GET", &url, &self.auth, None)?;
        let candidates: Vec<MergeRequest> = v
            .as_array()
            .map(|arr| arr.iter().filter_map(parse_mr).collect())
            .unwrap_or_default();
        Ok(pick(&candidates, state))
    }

    fn create(&self, req: &NewMr) -> anyhow::Result<MergeRequest> {
        let title = if req.draft {
            format!("{DRAFT_PREFIX}{}", req.title)
        } else {
            req.title.clone()
        };
        let body = json!({
            "source_branch": req.branch,
            "target_branch": req.base,
            "title": title,
            "description": req.body,
            "remove_source_branch": true,
        });
        let v = request("POST", &self.mrs_url(), &self.auth, Some(&body))?;
        parse_mr(&v).ok_or_else(|| anyhow::anyhow!("unexpected create response: {v}"))
    }

    fn set_base(&self, id: &str, base: &str) -> anyhow::Result<()> {
        let url = format!("{}/{}", self.mrs_url(), id);
        request(
            "PUT",
            &url,
            &self.auth,
            Some(&json!({ "target_branch": base })),
        )?;
        Ok(())
    }

    fn set_body(&self, id: &str, body: &str) -> anyhow::Result<()> {
        let url = format!("{}/{}", self.mrs_url(), id);
        request(
            "PUT",
            &url,
            &self.auth,
            Some(&json!({ "description": body })),
        )?;
        Ok(())
    }
}
