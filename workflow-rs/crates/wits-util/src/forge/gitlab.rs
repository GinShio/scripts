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
    encode, pick, request, Attributes, Auth, DiffVersion, FeedQuery, Forge, LineRef, MergeRequest,
    MrDetails, MrState, MrSummary, NewMr, RemoteComment, RemotePlacement, RemoteThread,
    ReviewOutcome, ReviewSubmission, Side, StateFilter, SubmitPlacement, Verdict, SELF_REF,
};
use crate::remote::RemoteInfo;

const DRAFT_PREFIX: &str = "Draft: ";

pub struct GitLab {
    api_base: String,
    /// The web root of the target project (`https://host/group/repo`), for blob
    /// permalinks — distinct from `api_base`.
    web_base: String,
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

        let web_base = format!("https://{}/{}", target.host, target.project_path());
        Ok(Self {
            api_base,
            web_base,
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
    end: LineRef,
    start: Option<LineRef>,
) -> Value {
    let mut pos = file_position(version, path, old_path);
    pos["position_type"] = json!("text");
    match end.side {
        Side::New => pos["new_line"] = json!(end.line),
        Side::Old => pos["old_line"] = json!(end.line),
    }
    if let Some(s) = start {
        pos["line_range"] = json!({
            "start": range_endpoint(s),
            "end": range_endpoint(end),
        });
    }
    pos
}

/// The `position` object a GitLab *file-level* diff note carries — the same
/// three version SHAs and paths as a line note, but `position_type: "file"` and
/// no line. This is what makes a file comment anchored to the file (so
/// `list_threads` reads it back as `position_type:"file"`, and `show --file`
/// finds it); a plain position-less note would degrade to an MR-level remark.
fn file_position(version: &DiffVersion, path: &str, old_path: Option<&str>) -> Value {
    json!({
        "position_type": "file",
        "base_sha": version.base_sha,
        "start_sha": version.start_sha,
        "head_sha": version.head_sha,
        "new_path": path,
        "old_path": old_path.unwrap_or(path),
    })
}

/// One endpoint of a GitLab `position.line_range`: the side as `type`, plus the
/// side-appropriate line. GitLab's schema also allows a `line_code` per endpoint;
/// it is omitted pending a live probe — `type` + the line may suffice, and a
/// wrongly-computed `line_code` would mis-anchor where omitting fails cleanly.
fn range_endpoint(r: LineRef) -> Value {
    let mut o = json!({ "type": match r.side { Side::New => "new", Side::Old => "old" } });
    match r.side {
        Side::New => o["new_line"] = json!(r.line),
        Side::Old => o["old_line"] = json!(r.line),
    }
    o
}

/// Read one `line_range` endpoint back into a [`LineRef`]. `type` selects the
/// side; the matching `new_line`/`old_line` gives the line.
fn parse_range_endpoint(v: &Value) -> Option<LineRef> {
    let side = match v["type"].as_str() {
        Some("old") => Side::Old,
        Some("new") => Side::New,
        _ => return None,
    };
    let line = v[if side == Side::New {
        "new_line"
    } else {
        "old_line"
    }]
    .as_u64()
    .map(|l| l as u32)
    // A side endpoint on a line that only exists on the other side (e.g. a
    // pure addition viewed from the old side) carries no line for that side;
    // fall back to whichever the object does carry so the span round-trips.
    .or_else(|| v["new_line"].as_u64().map(|l| l as u32))
    .or_else(|| v["old_line"].as_u64().map(|l| l as u32))?;
    Some(LineRef { line, side })
}

/// Best-effort delete the draft notes posted in this attempt, so a retry
/// starts clean. Called when the batch must abort — a draft note POST failed,
/// or `bulk_publish` failed after the drafts landed. Each DELETE is
/// independent; a failure here only warns: a leftover orphan would surface as a
/// duplicate only if a *later* `bulk_publish` runs, the rare double-failure
/// edge documented in the design.
fn rollback_drafts(
    id: &str,
    draft_url: &str,
    posted_ids: &[u64],
    summary_draft_id: Option<u64>,
    auth: &Auth,
) {
    for did in posted_ids {
        if let Err(e) = request("DELETE", &format!("{draft_url}/{did}"), auth, None) {
            log::warn!("MR {id}: rollback of draft {did} failed: {e}");
        }
    }
    if let Some(sid) = summary_draft_id {
        if let Err(e) = request("DELETE", &format!("{draft_url}/{sid}"), auth, None) {
            log::warn!("MR {id}: rollback of summary draft {sid} failed: {e}");
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
        // Drafts are open MRs; `draft` narrows within that. (The legacy `wip`
        // parameter was deprecated in GitLab 19.0 in favour of `draft`, which
        // takes a boolean.)
        match (query.states.open, query.states.draft) {
            (true, false) => url += "&draft=false",
            (false, true) => url += "&draft=true",
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
        let limit = query.limit;
        let mut collected = 0usize;
        super::request_paginated(&url, &self.auth, 10, |v, headers| {
            if collected >= limit {
                return (Vec::new(), None);
            }
            let items: Vec<MrSummary> = v
                .as_array()
                .map(|arr| arr.iter().filter_map(parse_summary).collect())
                .unwrap_or_default();
            collected += items.len();
            let next = if collected >= limit {
                None
            } else {
                headers
                    .iter()
                    .find(|(name, _)| name == "x-next-page")
                    .and_then(|(_, value)| {
                        let v = value.trim();
                        if v.is_empty() {
                            None
                        } else {
                            Some(format!("{url}&page={v}"))
                        }
                    })
            };
            (items, next)
        })
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
        let discussions: Vec<Value> =
            super::request_paginated(&url, &self.auth, 10, |v, headers| {
                let items: Vec<Value> = v.as_array().map(|arr| arr.to_vec()).unwrap_or_default();
                // GitLab signals the next page via X-Next-Page; absent or empty
                // means this is the last page.
                let next = headers
                    .iter()
                    .find(|(name, _)| name == "x-next-page")
                    .and_then(|(_, value)| {
                        let v = value.trim();
                        if v.is_empty() {
                            None
                        } else {
                            Some(format!("{url}&page={v}"))
                        }
                    });
                (items, next)
            })?;

        let mut threads = Vec::new();
        for d in &discussions {
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
                let path = p["new_path"]
                    .as_str()
                    .or_else(|| p["old_path"].as_str())
                    .unwrap_or_default()
                    .to_owned();
                let old_path = p["old_path"].as_str().map(str::to_owned);
                let commit = p["head_sha"].as_str().map(str::to_owned);
                // `position_type: "file"` is a file-level anchor; a `line_range`
                // is a multi-line span; otherwise a single-line note carries
                // `new_line` or `old_line`. (A position with neither line nor
                // range nor `file` type is a degenerate note we surface as a
                // line-0 anchor rather than dropping.)
                if p["position_type"].as_str() == Some("file") {
                    RemotePlacement::File { path }
                } else if let Some(lr) = p["line_range"].as_object() {
                    let end = parse_range_endpoint(&lr["end"]).unwrap_or(LineRef {
                        line: 0,
                        side: Side::New,
                    });
                    let start = parse_range_endpoint(&lr["start"]);
                    RemotePlacement::Line {
                        path,
                        old_path,
                        end,
                        start,
                        commit,
                    }
                } else {
                    let (side, line) = if let Some(l) = p["new_line"].as_u64() {
                        (Side::New, l as u32)
                    } else if let Some(l) = p["old_line"].as_u64() {
                        (Side::Old, l as u32)
                    } else {
                        (Side::New, 0)
                    };
                    RemotePlacement::Line {
                        path,
                        old_path,
                        end: LineRef { line, side },
                        start: None,
                        commit,
                    }
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

    fn submit_review(&self, id: &str, review: &ReviewSubmission) -> anyhow::Result<ReviewOutcome> {
        if review.is_empty() {
            return Ok(ReviewOutcome::default());
        }
        let draft_url = self.draft_notes_url(id);

        // GitLab has no batched review API: each comment is an individual draft
        // note POST, then `bulk_publish` makes them visible as one notification.
        // That two-phase shape puts atomicity on us, so this is all-or-nothing
        // *per attempt*: any draft failure aborts the publish and rolls back
        // what did post, so a retry is clean (no orphans, no duplicates). The
        // verdict rides with the batch — it is attempted only after the batch
        // landed, so an approve failure can never cause comment duplication.

        // --- Step 1: summary as a draft note. A summary failure does not block
        // the comments (it is tracked as its own outcome, not a draft-comment
        // failure that aborts the publish). ---
        let mut summary_ok = review.summary.is_none();
        let mut summary_draft_id: Option<u64> = None;
        if let Some(s) = &review.summary {
            match request("POST", &draft_url, &self.auth, Some(&json!({ "note": s }))) {
                Ok(v) => {
                    summary_draft_id = v["id"].as_u64();
                    summary_ok = true;
                }
                Err(e) => log::warn!(
                    "MR {id}: summary draft failed (comments will still be attempted): {e}"
                ),
            }
        }

        // --- Step 2: comment draft notes, posted in parallel, each with its own
        // result. `draft_ids` tracks what landed for the rollback path. ---
        let draft_ids: std::sync::Mutex<Vec<(usize, u64)>> = std::sync::Mutex::new(Vec::new());
        let results: std::sync::Mutex<Vec<(usize, bool)>> = std::sync::Mutex::new(Vec::new());
        let draft_url_ref = &draft_url;
        let auth = &self.auth;
        let draft_ids_ref = &draft_ids;
        let results_ref = &results;
        std::thread::scope(|scope| {
            for (i, c) in review.comments.iter().enumerate() {
                scope.spawn(move || {
                    // Each comment's position anchors to its own snapshot
                    // version (resolved at build time) — the heart of
                    // cross-snapshot drafting. GitLab needs all three SHAs per
                    // diff note, so this can't share the review-level version.
                    let body = match &c.placement {
                        SubmitPlacement::Line {
                            path,
                            old_path,
                            end,
                            start,
                            version,
                        } => json!({
                            "note": c.body,
                            "position": diff_position(
                                version, path, old_path.as_deref(), *end, *start,
                            ),
                        }),
                        SubmitPlacement::File { path, version, .. } => json!({
                            "note": c.body,
                            "position": file_position(version, path, None),
                        }),
                    };
                    match request("POST", draft_url_ref, auth, Some(&body)) {
                        Ok(v) => {
                            results_ref.lock().unwrap().push((i, true));
                            if let Some(did) = v["id"].as_u64() {
                                draft_ids_ref.lock().unwrap().push((i, did));
                            }
                        }
                        Err(e) => {
                            log::warn!("MR {id}: draft note {i} failed: {e}");
                            results_ref.lock().unwrap().push((i, false));
                        }
                    }
                });
            }
        });

        let mut results = results.into_inner().unwrap();
        results.sort_by_key(|(i, _)| *i);
        let mut comment_results: Vec<bool> = results.into_iter().map(|(_, ok)| ok).collect();
        let posted_ids: Vec<u64> = draft_ids
            .into_inner()
            .unwrap()
            .into_iter()
            .map(|(_, d)| d)
            .collect();

        let comments_all_posted = comment_results.iter().all(|&ok| ok);
        let has_drafts = !posted_ids.is_empty() || summary_draft_id.is_some();

        // --- Step 3: bulk_publish — only when every comment draft posted. A
        // single draft failure aborts the publish and rolls back what did post.
        // `bulk_publish` takes no body: it publishes all of the user's pending
        // drafts on the MR as one review (the documented model). ---
        let batch_ok = if comments_all_posted && has_drafts {
            match request(
                "POST",
                &format!("{draft_url}/bulk_publish"),
                &self.auth,
                None,
            ) {
                Ok(_) => true,
                Err(e) => {
                    log::warn!("MR {id}: bulk_publish failed, rolling back draft notes: {e}");
                    rollback_drafts(id, &draft_url, &posted_ids, summary_draft_id, &self.auth);
                    comment_results.iter_mut().for_each(|r| *r = false);
                    summary_ok = false;
                    false
                }
            }
        } else if !comments_all_posted {
            log::warn!(
                "MR {id}: a draft note failed to post; rolling back {} posted draft(s) and skipping publish",
                posted_ids.len()
            );
            rollback_drafts(id, &draft_url, &posted_ids, summary_draft_id, &self.auth);
            comment_results.iter_mut().for_each(|r| *r = false);
            summary_ok = false;
            false
        } else {
            // No drafts to publish (a verdict-only submission, or comments that
            // all posted but resolved to no draft ids). Nothing to roll back;
            // the batch is "ok" for the verdict step's purposes.
            comments_all_posted
        };

        // --- Step 4: verdict — only after the batch landed. Approve → POST
        // approve; RequestChanges → unapprove (GitLab has no native equivalent,
        // so we withdraw any prior approval of ours); Comment → nothing. A
        // failure here leaves visible comments and keeps only the verdict for
        // retry — never a duplicate. ---
        let verdict_ok = if batch_ok {
            match review.verdict {
                Some(Verdict::Approve) => {
                    let url = format!("{}/{id}/approve", self.mrs_url());
                    match request("POST", &url, &self.auth, None) {
                        Ok(_) => Some(true),
                        Err(e) => {
                            log::warn!("MR {id}: approve failed: {e}");
                            Some(false)
                        }
                    }
                }
                Some(Verdict::RequestChanges) => {
                    let url = format!("{}/{id}/approve", self.mrs_url());
                    match request("DELETE", &url, &self.auth, None) {
                        Ok(_) => Some(true),
                        Err(e) => {
                            log::warn!("MR {id}: unapprove failed: {e}");
                            Some(false)
                        }
                    }
                }
                Some(Verdict::Comment) | None => None,
            }
        } else {
            // The batch didn't land — the verdict rides with it (the same
            // all-or-nothing logic as bulk_publish): not attempted, stays in
            // the draft for retry.
            review.verdict.is_some().then_some(false)
        };

        Ok(ReviewOutcome {
            comment_results,
            summary_ok,
            verdict_ok,
        })
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
        let url = format!("{}/{id}/discussions/{thread}", self.mrs_url());
        request(
            "PUT",
            &url,
            &self.auth,
            Some(&json!({ "resolved": resolved })),
        )?;
        Ok(())
    }

    fn permalink(&self, r#ref: &str, path: &str, lines: Option<(u32, Option<u32>)>) -> String {
        let frag = match lines {
            Some((a, Some(b))) => format!("#L{a}-{b}"),
            Some((a, None)) => format!("#L{a}"),
            None => String::new(),
        };
        format!("{}/-/blob/{ref}/{path}{frag}", self.web_base)
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
