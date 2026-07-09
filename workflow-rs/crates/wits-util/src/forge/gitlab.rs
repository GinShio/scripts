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
    encode, pick, request, Attributes, Auth, DiffVersion, FeedQuery, Forge, MergeRequest,
    MrDetails, MrState, MrSummary, NewMr, RemoteComment, RemotePlacement, RemoteThread,
    ReviewSubmission, Side, StateFilter, SubmitPlacement, Verdict, SELF_REF,
};
use crate::remote::RemoteInfo;

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

    /// The authenticated user's username, for resolving `@me` in a feed filter.
    fn current_username(&self) -> anyhow::Result<String> {
        let v = request("GET", &format!("{}/user", self.api_base), &self.auth, None)?;
        v["username"]
            .as_str()
            .map(str::to_owned)
            .ok_or_else(|| anyhow::anyhow!("could not read the authenticated user"))
    }

    /// A feed filter value, with `@me` expanded to the authenticated username.
    fn filter_user(&self, name: &str) -> anyhow::Result<String> {
        if name == SELF_REF {
            self.current_username()
        } else {
            Ok(name.to_owned())
        }
    }

    /// The MR-scoped draft-notes endpoint (always on the target project).
    fn draft_notes_url(&self, id: &str) -> String {
        format!("{}/{id}/draft_notes", self.mrs_url())
    }
}

/// Build the `position` object a GitLab diff note needs. The three version SHAs
/// pin the comment to the exact diff it was written against; when that is behind
/// the MR's current state the note simply lands on that older version.
fn diff_position(
    version: &DiffVersion,
    path: &str,
    old_path: Option<&str>,
    side: Side,
    line: u32,
) -> Value {
    let mut pos = json!({
        "position_type": "text",
        "base_sha": version.base_sha,
        "start_sha": version.start_sha,
        "head_sha": version.head_sha,
        "new_path": path,
        "old_path": old_path.unwrap_or(path),
    });
    match side {
        Side::New => pos["new_line"] = json!(line),
        Side::Old => pos["old_line"] = json!(line),
    }
    pos
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

fn parse_summary(v: &Value) -> Option<MrSummary> {
    let iid = v["iid"].as_u64()?;
    let state = match v["state"].as_str().unwrap_or("opened") {
        "merged" => MrState::Merged,
        "opened" | "locked" => MrState::Open,
        _ => MrState::Closed,
    };
    let draft = v["draft"]
        .as_bool()
        .or_else(|| v["work_in_progress"].as_bool())
        .unwrap_or(false);
    let labels = v["labels"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|l| l.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    Some(MrSummary {
        id: iid.to_string(),
        display: format!("!{iid}"),
        state,
        draft,
        title: v["title"].as_str().unwrap_or_default().to_owned(),
        author: v["author"]["username"]
            .as_str()
            .unwrap_or_default()
            .to_owned(),
        base: v["target_branch"].as_str().unwrap_or_default().to_owned(),
        source: v["source_branch"].as_str().unwrap_or_default().to_owned(),
        head_sha: v["sha"].as_str().map(str::to_owned),
        updated_at: v["updated_at"].as_str().unwrap_or_default().to_owned(),
        labels,
        web_url: v["web_url"].as_str().unwrap_or_default().to_owned(),
    })
}

fn parse_note(n: &Value) -> RemoteComment {
    RemoteComment {
        id: n["id"].as_u64().unwrap_or(0).to_string(),
        author: n["author"]["username"]
            .as_str()
            .unwrap_or_default()
            .to_owned(),
        body: n["body"].as_str().unwrap_or_default().to_owned(),
        created_at: n["created_at"].as_str().unwrap_or_default().to_owned(),
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
    fn noun(&self) -> &'static str {
        "MR"
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

    fn list_mrs(&self, query: &FeedQuery) -> anyhow::Result<Vec<MrSummary>> {
        let per_page = query.limit.clamp(1, 100);
        let mut url = format!(
            "{}?state=opened&order_by=updated_at&sort=desc&per_page={per_page}",
            self.mrs_url()
        );
        // Drafts are opened MRs; `wip` narrows within that.
        match (query.states.open, query.states.draft) {
            (true, false) => url += "&wip=no",
            (false, true) => url += "&wip=yes",
            _ => {}
        }
        // Multiple labels are AND on GitLab too (all must be present).
        if !query.labels.is_empty() {
            url += &format!("&labels={}", encode(&query.labels.join(",")));
        }
        if !query.exclude_labels.is_empty() {
            url += &format!("&not[labels]={}", encode(&query.exclude_labels.join(",")));
        }
        if let Some(a) = &query.author {
            url += &format!("&author_username={}", encode(&self.filter_user(a)?));
        }
        if let Some(a) = &query.assignee {
            url += &format!("&assignee_username={}", encode(&self.filter_user(a)?));
        }
        if let Some(r) = &query.reviewer {
            url += &format!("&reviewer_username={}", encode(&self.filter_user(r)?));
        }
        if let Some(u) = &query.updated_after {
            url += &format!("&updated_after={}", encode(u));
        }
        if let Some(s) = &query.search {
            url += &format!("&search={}", encode(s));
        }
        let v = request("GET", &url, &self.auth, None)?;
        Ok(v.as_array()
            .map(|arr| arr.iter().filter_map(parse_summary).collect())
            .unwrap_or_default())
    }

    fn mr_details(&self, id: &str) -> anyhow::Result<MrDetails> {
        let v = request("GET", &format!("{}/{id}", self.mrs_url()), &self.auth, None)?;
        let summary =
            parse_summary(&v).ok_or_else(|| anyhow::anyhow!("unexpected MR response: {v}"))?;
        let dr = &v["diff_refs"];
        let version = DiffVersion {
            base_sha: dr["base_sha"].as_str().unwrap_or_default().to_owned(),
            start_sha: dr["start_sha"].as_str().unwrap_or_default().to_owned(),
            head_sha: dr["head_sha"].as_str().unwrap_or_default().to_owned(),
        };
        Ok(MrDetails { summary, version })
    }

    fn mr_ref(&self, id: &str) -> anyhow::Result<String> {
        Ok(format!("refs/merge-requests/{id}/head"))
    }

    fn list_threads(&self, id: &str) -> anyhow::Result<Vec<RemoteThread>> {
        let url = format!("{}/{id}/discussions?per_page=100", self.mrs_url());
        let v = request("GET", &url, &self.auth, None)?;
        let mut threads = Vec::new();
        let Some(arr) = v.as_array() else {
            return Ok(threads);
        };
        for d in arr {
            let notes = d["notes"].as_array().cloned().unwrap_or_default();
            // System notes (label/state events) are not review discussion.
            let real: Vec<&Value> = notes
                .iter()
                .filter(|n| !n["system"].as_bool().unwrap_or(false))
                .collect();
            let Some(first) = real.first() else {
                continue;
            };
            let placement = if first["position"].is_object() {
                let p = &first["position"];
                let (side, line) = if let Some(l) = p["new_line"].as_u64() {
                    (Side::New, l as u32)
                } else if let Some(l) = p["old_line"].as_u64() {
                    (Side::Old, l as u32)
                } else {
                    (Side::New, 0)
                };
                RemotePlacement::Line {
                    path: p["new_path"]
                        .as_str()
                        .or_else(|| p["old_path"].as_str())
                        .unwrap_or_default()
                        .to_owned(),
                    old_path: p["old_path"].as_str().map(str::to_owned),
                    side,
                    line,
                    commit: p["head_sha"].as_str().map(str::to_owned),
                }
            } else {
                RemotePlacement::Mr
            };
            let resolvable: Vec<&&Value> = real
                .iter()
                .filter(|n| n["resolvable"].as_bool().unwrap_or(false))
                .collect();
            let resolved = !resolvable.is_empty()
                && resolvable
                    .iter()
                    .all(|n| n["resolved"].as_bool().unwrap_or(false));
            threads.push(RemoteThread {
                id: d["id"].as_str().unwrap_or_default().to_owned(),
                resolved,
                // GitLab exposes no cheap per-note outdated flag; left false in
                // v1 (see the review docs' capability matrix).
                outdated: false,
                placement,
                comments: real.iter().map(|&n| parse_note(n)).collect(),
            });
        }
        Ok(threads)
    }

    fn submit_review(&self, id: &str, review: &ReviewSubmission) -> anyhow::Result<()> {
        if review.is_empty() {
            return Ok(());
        }
        let draft_url = self.draft_notes_url(id);
        // The summary rides as a general draft note.
        if let Some(s) = &review.summary {
            request("POST", &draft_url, &self.auth, Some(&json!({ "note": s })))?;
        }
        for c in &review.comments {
            let body = match &c.placement {
                SubmitPlacement::Line {
                    path,
                    old_path,
                    side,
                    line,
                    ..
                } => json!({
                    "note": c.body,
                    "position": diff_position(&review.version, path, old_path.as_deref(), *side, *line),
                }),
                // GitLab has no file-level anchor; fall back to a general note
                // that names the file (documented).
                SubmitPlacement::File { path, .. } => {
                    json!({ "note": format!("`{path}`:\n\n{}", c.body) })
                }
            };
            request("POST", &draft_url, &self.auth, Some(&body))?;
        }
        // Publish every draft note at once — one notification.
        request(
            "POST",
            &format!("{draft_url}/bulk_publish"),
            &self.auth,
            None,
        )?;
        // Approve is a distinct endpoint; request-changes/comment have no native
        // action beyond the notes just published.
        if review.verdict == Some(Verdict::Approve) {
            let url = format!("{}/{id}/approve", self.mrs_url());
            request("POST", &url, &self.auth, None)?;
        }
        Ok(())
    }

    fn comment_mr(&self, id: &str, body: &str) -> anyhow::Result<()> {
        let url = format!("{}/{id}/notes", self.mrs_url());
        request("POST", &url, &self.auth, Some(&json!({ "body": body })))?;
        Ok(())
    }

    fn reply(&self, id: &str, thread: &str, body: &str) -> anyhow::Result<()> {
        let url = format!("{}/{id}/discussions/{thread}/notes", self.mrs_url());
        request("POST", &url, &self.auth, Some(&json!({ "body": body })))?;
        Ok(())
    }

    fn resolve(&self, id: &str, thread: &str, resolved: bool) -> anyhow::Result<()> {
        let url = format!(
            "{}/{id}/discussions/{thread}?resolved={resolved}",
            self.mrs_url()
        );
        request("PUT", &url, &self.auth, None)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::remote::Service;

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
