//! GitHub (and GitHub Enterprise) merge requests — "pull requests" in its words.
//!
//! This module is pure mapping: GitHub's REST shapes in, normalized
//! [`MergeRequest`]s out. The only judgement calls are the API base (the public
//! host has a dedicated `api.github.com`, Enterprise serves under `/api/v3`) and
//! the cross-fork head form (`owner:branch`).

use std::collections::HashMap;

use serde_json::{json, Value};

use super::{
    current_user, encode, pick, request, resolve_self, Attributes, Auth, DiffVersion, FeedQuery,
    Forge, MergeRequest, MrDetails, MrState, MrSummary, NewMr, RemoteComment, RemotePlacement,
    RemoteThread, ReviewSubmission, Side, StateFilter, SubmitPlacement, Verdict, SELF_REF,
};
use crate::remote::RemoteInfo;

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

    /// Expand `@me` to the authenticated login, only paying for the lookup when
    /// the marker is actually present.
    fn resolve_users(&self, items: &[String]) -> anyhow::Result<Vec<String>> {
        if items.iter().any(|i| i == SELF_REF) {
            let me = current_user(&self.api_base, &self.auth, "login")?;
            Ok(resolve_self(items, &me))
        } else {
            Ok(items.to_vec())
        }
    }

    /// Build a GitHub search query string from a feed's filter. `@me` in a user
    /// qualifier is passed through — GitHub resolves it server-side.
    ///
    /// One deviation from the tool's within-field-OR model: GitHub search has no
    /// label-OR qualifier, so multiple labels are AND-ed here (each `label:` is a
    /// separate, conjunctive term). GitLab honours OR. This is noted in the
    /// review docs.
    fn search_query(&self, q: &FeedQuery) -> String {
        let mut parts = vec![format!("repo:{}", self.project), "is:pr".to_owned()];
        // merged/closed are never fetched; drafts are open PRs flagged draft.
        parts.push("is:open".to_owned());
        match (q.states.open, q.states.draft) {
            (true, false) => parts.push("draft:false".to_owned()),
            (false, true) => parts.push("draft:true".to_owned()),
            _ => {}
        }
        for l in &q.labels {
            parts.push(format!("label:{}", search_quote(l)));
        }
        for l in &q.exclude_labels {
            parts.push(format!("-label:{}", search_quote(l)));
        }
        if let Some(a) = &q.author {
            parts.push(format!("author:{a}"));
        }
        if let Some(a) = &q.assignee {
            parts.push(format!("assignee:{a}"));
        }
        if let Some(r) = &q.reviewer {
            parts.push(format!("review-requested:{r}"));
        }
        if let Some(u) = &q.updated_after {
            parts.push(format!("updated:>={u}"));
        }
        if let Some(s) = &q.search {
            parts.push(s.clone());
        }
        parts.join(" ")
    }
}

/// A summary from either a full pull object or a search `items[]` entry — they
/// share the fields the inbox needs; only a full pull carries `base`/`head`.
fn parse_summary(v: &Value) -> Option<MrSummary> {
    let number = v["number"].as_u64()?;
    let merged = v.get("merged_at").is_some_and(|m| !m.is_null());
    let state = match v["state"].as_str().unwrap_or("open") {
        _ if merged => MrState::Merged,
        "closed" => MrState::Closed,
        _ => MrState::Open,
    };
    let labels = v["labels"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|l| l["name"].as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default();
    Some(MrSummary {
        id: number.to_string(),
        display: format!("#{number}"),
        state,
        draft: v["draft"].as_bool().unwrap_or(false),
        title: v["title"].as_str().unwrap_or_default().to_owned(),
        author: v["user"]["login"].as_str().unwrap_or_default().to_owned(),
        base: v["base"]["ref"].as_str().unwrap_or_default().to_owned(),
        source: v["head"]["ref"].as_str().unwrap_or_default().to_owned(),
        head_sha: v["head"]["sha"].as_str().map(str::to_owned),
        updated_at: v["updated_at"].as_str().unwrap_or_default().to_owned(),
        labels,
        web_url: v["html_url"].as_str().unwrap_or_default().to_owned(),
    })
}

/// The `event` string GitHub's review API expects for each verdict.
fn verdict_event(v: Verdict) -> &'static str {
    match v {
        Verdict::Approve => "APPROVE",
        Verdict::RequestChanges => "REQUEST_CHANGES",
        Verdict::Comment => "COMMENT",
    }
}

/// GitHub spells the diff sides `LEFT` (pre-image) and `RIGHT` (post-image).
fn gh_side(side: Side) -> &'static str {
    match side {
        Side::Old => "LEFT",
        Side::New => "RIGHT",
    }
}

/// Quote a search term when it contains whitespace, so `label:needs review`
/// reaches GitHub as `label:"needs review"` rather than two qualifiers.
fn search_quote(term: &str) -> String {
    if term.contains(char::is_whitespace) {
        format!("\"{term}\"")
    } else {
        term.to_owned()
    }
}

/// One inline review comment parsed into a fresh thread root.
fn parse_review_comment(c: &Value) -> RemoteComment {
    RemoteComment {
        id: c["id"].as_u64().unwrap_or(0).to_string(),
        author: c["user"]["login"].as_str().unwrap_or_default().to_owned(),
        body: c["body"].as_str().unwrap_or_default().to_owned(),
        created_at: c["created_at"].as_str().unwrap_or_default().to_owned(),
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
    fn noun(&self) -> &'static str {
        "PR"
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

    fn apply_attributes(&self, id: &str, attrs: &Attributes) -> anyhow::Result<()> {
        // A PR is an issue, and all three of GitHub's endpoints here *add* rather
        // than replace, so each call is naturally additive — no read-merge needed.
        let issue = format!("{}/repos/{}/issues/{}", self.api_base, self.project, id);
        let pull = format!("{}/repos/{}/pulls/{}", self.api_base, self.project, id);

        if !attrs.labels.is_empty() {
            let body = json!({ "labels": attrs.labels });
            if let Err(e) = request("POST", &format!("{issue}/labels"), &self.auth, Some(&body)) {
                log::warn!("labels: {e}");
            }
        }
        if !attrs.assignees.is_empty() {
            let body = json!({ "assignees": self.resolve_users(&attrs.assignees)? });
            if let Err(e) = request(
                "POST",
                &format!("{issue}/assignees"),
                &self.auth,
                Some(&body),
            ) {
                log::warn!("assignees: {e}");
            }
        }
        if !attrs.reviewers.is_empty() {
            let body = json!({ "reviewers": self.resolve_users(&attrs.reviewers)? });
            let url = format!("{pull}/requested_reviewers");
            if let Err(e) = request("POST", &url, &self.auth, Some(&body)) {
                log::warn!("reviewers: {e}");
            }
        }
        Ok(())
    }

    fn list_mrs(&self, query: &FeedQuery) -> anyhow::Result<Vec<MrSummary>> {
        let per_page = query.limit.clamp(1, 100);
        let url = format!(
            "{}/search/issues?q={}&per_page={per_page}&sort=updated&order=desc",
            self.api_base,
            encode(&self.search_query(query)),
        );
        let v = request("GET", &url, &self.auth, None)?;
        Ok(v["items"]
            .as_array()
            .map(|arr| arr.iter().filter_map(parse_summary).collect())
            .unwrap_or_default())
    }

    fn mr_details(&self, id: &str) -> anyhow::Result<MrDetails> {
        let url = format!("{}/{id}", self.pulls_url());
        let v = request("GET", &url, &self.auth, None)?;
        let summary =
            parse_summary(&v).ok_or_else(|| anyhow::anyhow!("unexpected PR response: {v}"))?;
        // GitHub anchors a review at a single commit_id (the head being
        // reviewed); it has no per-comment start SHA, so start mirrors base.
        let base_sha = v["base"]["sha"].as_str().unwrap_or_default().to_owned();
        let head_sha = v["head"]["sha"].as_str().unwrap_or_default().to_owned();
        let version = DiffVersion {
            base_sha: base_sha.clone(),
            start_sha: base_sha,
            head_sha,
        };
        Ok(MrDetails { summary, version })
    }

    fn mr_ref(&self, id: &str) -> anyhow::Result<String> {
        Ok(format!("refs/pull/{id}/head"))
    }

    fn list_threads(&self, id: &str) -> anyhow::Result<Vec<RemoteThread>> {
        // Inline review comments arrive flat; a reply carries `in_reply_to_id`
        // pointing at its thread's root, so we group on that. GitHub reports a
        // comment whose anchored line has fallen out of the diff with a null
        // `position` — that is our outdated signal. Thread *resolution* is
        // GraphQL-only and unreadable here, so `resolved` stays false (a
        // documented v1 limitation).
        let url = format!("{}/{id}/comments?per_page=100", self.pulls_url());
        let v = request("GET", &url, &self.auth, None)?;
        let mut threads: Vec<RemoteThread> = Vec::new();
        let mut root_index: HashMap<u64, usize> = HashMap::new();
        if let Some(arr) = v.as_array() {
            for c in arr {
                let comment = parse_review_comment(c);
                if let Some(root) = c["in_reply_to_id"].as_u64() {
                    if let Some(&i) = root_index.get(&root) {
                        threads[i].comments.push(comment);
                        continue;
                    }
                }
                let cid = c["id"].as_u64().unwrap_or(0);
                let placement = if c["subject_type"].as_str() == Some("file") {
                    RemotePlacement::File {
                        path: c["path"].as_str().unwrap_or_default().to_owned(),
                    }
                } else {
                    let side = match c["side"].as_str() {
                        Some("LEFT") => Side::Old,
                        _ => Side::New,
                    };
                    let line = c["line"]
                        .as_u64()
                        .or_else(|| c["original_line"].as_u64())
                        .unwrap_or(0) as u32;
                    RemotePlacement::Line {
                        path: c["path"].as_str().unwrap_or_default().to_owned(),
                        old_path: None,
                        side,
                        line,
                        commit: c["original_commit_id"]
                            .as_str()
                            .or_else(|| c["commit_id"].as_str())
                            .map(str::to_owned),
                    }
                };
                root_index.insert(cid, threads.len());
                threads.push(RemoteThread {
                    id: cid.to_string(),
                    resolved: false,
                    outdated: c["position"].is_null(),
                    placement,
                    comments: vec![comment],
                });
            }
        }

        // Conversation (issue) comments — each an MR-level thread.
        let iurl = format!(
            "{}/repos/{}/issues/{id}/comments?per_page=100",
            self.api_base, self.project
        );
        let iv = request("GET", &iurl, &self.auth, None)?;
        if let Some(arr) = iv.as_array() {
            for c in arr {
                threads.push(RemoteThread {
                    id: c["id"].as_u64().unwrap_or(0).to_string(),
                    resolved: false,
                    outdated: false,
                    placement: RemotePlacement::Mr,
                    comments: vec![parse_review_comment(c)],
                });
            }
        }
        Ok(threads)
    }

    fn submit_review(&self, id: &str, review: &ReviewSubmission) -> anyhow::Result<()> {
        if review.is_empty() {
            return Ok(());
        }
        let comments: Vec<Value> = review
            .comments
            .iter()
            .map(|c| match &c.placement {
                SubmitPlacement::Line {
                    path,
                    side,
                    line,
                    start_line,
                    ..
                } => {
                    let mut o = json!({
                        "path": path,
                        "line": line,
                        "side": gh_side(*side),
                        "body": c.body,
                    });
                    if let Some(sl) = start_line {
                        o["start_line"] = json!(sl);
                        o["start_side"] = json!(gh_side(*side));
                    }
                    o
                }
                SubmitPlacement::File { path, .. } => json!({
                    "path": path,
                    "subject_type": "file",
                    "body": c.body,
                }),
            })
            .collect();

        // All comments ride one commit_id — the reviewed snapshot's head. When
        // that is behind the MR's current head, GitHub marks them outdated, which
        // is exactly the intent (we anchor to what was reviewed).
        let mut body = json!({ "commit_id": review.version.head_sha, "comments": comments });
        if let Some(v) = review.verdict {
            body["event"] = json!(verdict_event(v));
        }
        if let Some(s) = &review.summary {
            body["body"] = json!(s);
        }
        let url = format!("{}/{id}/reviews", self.pulls_url());
        request("POST", &url, &self.auth, Some(&body))?;
        Ok(())
    }

    fn comment_mr(&self, id: &str, body: &str) -> anyhow::Result<()> {
        let url = format!(
            "{}/repos/{}/issues/{id}/comments",
            self.api_base, self.project
        );
        request("POST", &url, &self.auth, Some(&json!({ "body": body })))?;
        Ok(())
    }

    fn reply(&self, id: &str, thread: &str, body: &str) -> anyhow::Result<()> {
        let url = format!("{}/{id}/comments/{thread}/replies", self.pulls_url());
        request("POST", &url, &self.auth, Some(&json!({ "body": body })))?;
        Ok(())
    }
}
