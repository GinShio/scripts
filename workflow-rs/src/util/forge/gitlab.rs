//! GitLab merge requests.
//!
//! GitLab is the platform whose vocabulary the rest of the tool borrows ("MR",
//! "!"). Two shape differences matter for the same-project case: a project is
//! addressed by a URL-encoded `group/sub/repo` id, and a draft is a `Draft:`
//! title prefix rather than a field.
//!
//! Cross-project (fork) MRs are the awkward part. Unlike GitHub/Gitea, GitLab has
//! no `owner:branch` head string: an MR from a fork is **created on the source
//! project** carrying a numeric `target_project_id`, but the MR itself then lives
//! in the **target** project (its iid belongs there), so reads and edits address
//! the target. Numeric project ids are required for the `target_project_id` body
//! field and the `source_project_id` list filter, so we resolve them once up
//! front. When source and target are the same project, none of this applies and
//! we stay on the cheap single-project path.

use serde_json::{json, Value};

use super::{
    encode, pick, request, Attributes, Auth, Forge, MergeRequest, MrState, NewMr, StateFilter,
    SELF_REF,
};
use crate::util::remote::RemoteInfo;

const DRAFT_PREFIX: &str = "Draft: ";

pub struct GitLab {
    api_base: String,
    /// Encoded path of the project where the MR resides — the target. Reads and
    /// edits always go here; for a same-project MR this is also where it's made.
    target_path: String,
    auth: Auth,
    /// Present only for a fork MR (source project differs from target).
    fork: Option<Fork>,
}

/// The extra coordinates a cross-project MR needs, resolved once at construction.
struct Fork {
    source_path: String,
    source_id: u64,
    target_id: u64,
}

impl GitLab {
    pub fn new(
        target: RemoteInfo,
        source: Option<RemoteInfo>,
        token: String,
        api_url_override: Option<String>,
    ) -> anyhow::Result<Self> {
        let api_base =
            api_url_override.unwrap_or_else(|| format!("https://{}/api/v4", target.host));
        let auth = Auth::PrivateToken(token);
        let target_path = encode(&target.project_path());

        // A fork MR only makes sense when the source is a *different* project on
        // the *same* GitLab instance; cross-instance forks don't exist.
        let fork = match source {
            Some(src) if src.host == target.host && src.project_path() != target.project_path() => {
                let source_path = encode(&src.project_path());
                let source_id = project_id(&api_base, &auth, &source_path)?;
                let target_id = project_id(&api_base, &auth, &target_path)?;
                Some(Fork {
                    source_path,
                    source_id,
                    target_id,
                })
            }
            _ => None,
        };

        Ok(Self {
            api_base,
            target_path,
            auth,
            fork,
        })
    }

    /// Where the MR lives — the endpoint for finding and editing it.
    fn mrs_url(&self) -> String {
        format!(
            "{}/projects/{}/merge_requests",
            self.api_base, self.target_path
        )
    }

    /// Resolve a username (or `@me`) to GitLab's numeric user id, which is what
    /// the `assignee_ids` / `reviewer_ids` fields require.
    fn user_id(&self, name: &str) -> anyhow::Result<Option<u64>> {
        if name == SELF_REF {
            let v = request("GET", &format!("{}/user", self.api_base), &self.auth, None)?;
            return Ok(v["id"].as_u64());
        }
        let url = format!("{}/users?username={}", self.api_base, encode(name));
        let v = request("GET", &url, &self.auth, None)?;
        Ok(v.as_array()
            .and_then(|a| a.first())
            .and_then(|u| u["id"].as_u64()))
    }

    /// Union the resolved ids of `names` into `ids` (additive, deduped).
    fn add_user_ids(&self, ids: &mut Vec<u64>, names: &[String]) {
        for name in names {
            match self.user_id(name) {
                Ok(Some(uid)) if !ids.contains(&uid) => ids.push(uid),
                Ok(Some(_)) => {}
                Ok(None) => log::warn!("user '{name}' not found"),
                Err(e) => log::warn!("resolving user '{name}': {e}"),
            }
        }
    }
}

/// Numeric ids of the users currently in `field` (`assignees`/`reviewers`) on an
/// MR JSON object — the base for an additive update.
fn current_user_ids(mr: &Value, field: &str) -> Vec<u64> {
    mr[field]
        .as_array()
        .map(|arr| arr.iter().filter_map(|u| u["id"].as_u64()).collect())
        .unwrap_or_default()
}

/// Resolve a project's numeric id from its URL-encoded path. GitLab accepts the
/// path for addressing, but the `target_project_id`/`source_project_id` fields
/// insist on the numeric form, so a fork MR needs this lookup.
fn project_id(api_base: &str, auth: &Auth, encoded_path: &str) -> anyhow::Result<u64> {
    let url = format!("{api_base}/projects/{encoded_path}");
    let v = request("GET", &url, auth, None)?;
    v["id"]
        .as_u64()
        .ok_or_else(|| anyhow::anyhow!("could not read numeric id of project from {url}"))
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
        // Match by source branch, never target, so a drifted base is still found.
        // For a fork, the same branch name can exist in several source projects,
        // so we pin it to ours with source_project_id.
        let mut url = format!(
            "{}?source_branch={}&state=all",
            self.mrs_url(),
            encode(branch),
        );
        if let Some(fork) = &self.fork {
            url += &format!("&source_project_id={}", fork.source_id);
        }
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
        let mut body = json!({
            "source_branch": req.branch,
            "target_branch": req.base,
            "title": title,
            "description": req.body,
            "remove_source_branch": true,
        });

        // A fork MR is created on the source project, pointing at the target by
        // numeric id; a same-project MR is created where it lives.
        let url = match &self.fork {
            Some(fork) => {
                body["target_project_id"] = json!(fork.target_id);
                format!(
                    "{}/projects/{}/merge_requests",
                    self.api_base, fork.source_path
                )
            }
            None => self.mrs_url(),
        };

        let v = request("POST", &url, &self.auth, Some(&body))?;
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

    fn apply_attributes(&self, id: &str, attrs: &Attributes) -> anyhow::Result<()> {
        let mut body = serde_json::Map::new();

        // Labels have a native additive verb; users do not, so we read the
        // current ids and union ours in, then PUT the full set.
        if !attrs.labels.is_empty() {
            body.insert("add_labels".into(), json!(attrs.labels.join(",")));
        }
        if !attrs.assignees.is_empty() || !attrs.reviewers.is_empty() {
            let mr = request(
                "GET",
                &format!("{}/{}", self.mrs_url(), id),
                &self.auth,
                None,
            )?;
            if !attrs.assignees.is_empty() {
                let mut ids = current_user_ids(&mr, "assignees");
                self.add_user_ids(&mut ids, &attrs.assignees);
                body.insert("assignee_ids".into(), json!(ids));
            }
            if !attrs.reviewers.is_empty() {
                let mut ids = current_user_ids(&mr, "reviewers");
                self.add_user_ids(&mut ids, &attrs.reviewers);
                body.insert("reviewer_ids".into(), json!(ids));
            }
        }

        if body.is_empty() {
            return Ok(());
        }
        let url = format!("{}/{}", self.mrs_url(), id);
        request("PUT", &url, &self.auth, Some(&Value::Object(body)))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::util::remote::Service;

    fn info(host: &str, owner: &str, repo: &str) -> RemoteInfo {
        RemoteInfo {
            host: host.into(),
            owner: owner.into(),
            repo: repo.into(),
            service: Service::GitLab,
        }
    }

    #[test]
    fn same_project_needs_no_fork_and_no_network() {
        // No source, or a source equal to the target, stays on the single-project
        // path — constructible without any project-id lookups.
        let target = info("gitlab.com", "me", "widget");
        let gl = GitLab::new(target.clone(), None, "tok".into(), None).unwrap();
        assert!(gl.fork.is_none());
        assert!(gl
            .mrs_url()
            .ends_with("/projects/me%2Fwidget/merge_requests"));

        let same = GitLab::new(target.clone(), Some(target), "tok".into(), None).unwrap();
        assert!(same.fork.is_none());
    }
}
