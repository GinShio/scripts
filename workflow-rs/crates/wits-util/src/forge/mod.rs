//! Talking to a git hosting platform's merge-request API.
//!
//! Five forges, one job: find an MR for a branch, create one, move its base,
//! rewrite its body. The temptation — and the mistake the earlier tooling made —
//! is to let each platform's REST quirks (`number` vs `iid`, `base.ref` vs
//! `target_branch`, draft-by-field vs draft-by-title-prefix) seep into the code
//! that drives the workflow. So the boundary here is deliberately hard: a
//! [`MergeRequest`] is normalized, the [`Forge`] trait is four small primitives,
//! and everything platform-specific is trapped inside one host module behind it.
//! Adding a forge is then a self-contained mapping exercise, never a change to
//! the verbs.
//!
//! Transport is plain blocking REST (`ureq`), which keeps every platform on the
//! same footing and avoids a dependency on whatever CLI the user did or didn't
//! install. The verbs decide whether a mutation may happen (dry-run lives at the
//! orchestration layer); when a primitive here is called, it calls the network.

pub mod gitea;
pub mod github;
pub mod gitlab;
pub mod review;

use serde_json::Value;

pub use review::{
    DiffVersion, FeedQuery, FeedStates, MrDetails, MrSummary, RemoteComment, RemotePlacement,
    RemoteThread, ReviewSubmission, Side, SubmitComment, SubmitPlacement, Verdict,
};

use crate::git::Repository;
use crate::remote::{Remotes, Service};

/// An MR's lifecycle, normalized across platforms that spell it differently
/// (GitHub folds "merged" into "closed"; GitLab keeps them apart).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MrState {
    Open,
    Merged,
    Closed,
}

/// Which lifecycle states a `find` should accept. Kept separate from [`MrState`]
/// because callers think in terms of intent ("is there one open?", "is there any
/// closed leftover?") rather than enumerating states.
#[derive(Debug, Clone, Copy)]
pub enum StateFilter {
    Open,
    NotOpen,
}

impl StateFilter {
    fn accepts(self, state: MrState) -> bool {
        match self {
            StateFilter::Open => state == MrState::Open,
            StateFilter::NotOpen => state != MrState::Open,
        }
    }
}

/// The platform-independent view of one merge request. `id` is whatever opaque
/// token the platform needs to address it again (a number, an iid); `display` is
/// the human form (`#123`, `!45`). Nothing above this struct ever sees raw JSON.
#[derive(Debug, Clone)]
pub struct MergeRequest {
    pub id: String,
    pub display: String,
    pub state: MrState,
    pub base: String,
    pub head_sha: Option<String>,
    pub body: String,
    pub web_url: String,
}

/// Everything needed to open a new MR. The forge turns `branch` (plus the fork
/// owner it already knows) into the right head reference; the caller never has to
/// reason about cross-fork head syntax.
#[derive(Debug, Clone)]
pub struct NewMr {
    pub branch: String,
    pub base: String,
    pub title: String,
    pub body: String,
    pub draft: bool,
}

/// Attributes layered onto an existing MR by `decorate`. Applied *additively*:
/// the platform adds what's listed and never removes anything, so a project's own
/// label/reviewer automation is never fought. The literal `@me` resolves to the
/// authenticated user.
#[derive(Debug, Clone, Default)]
pub struct Attributes {
    pub labels: Vec<String>,
    pub assignees: Vec<String>,
    pub reviewers: Vec<String>,
}

impl Attributes {
    pub fn is_empty(&self) -> bool {
        self.labels.is_empty() && self.assignees.is_empty() && self.reviewers.is_empty()
    }

    /// A short, human description for dry-run / log lines.
    pub fn summary(&self) -> String {
        let mut parts = Vec::new();
        if !self.labels.is_empty() {
            parts.push(format!("labels={:?}", self.labels));
        }
        if !self.assignees.is_empty() {
            parts.push(format!("assignees={:?}", self.assignees));
        }
        if !self.reviewers.is_empty() {
            parts.push(format!("reviewers={:?}", self.reviewers));
        }
        parts.join(" ")
    }
}

/// The four primitives every platform must provide. The workflow verbs are
/// written once against this trait; see the module note for why the surface is
/// this small.
pub trait Forge: Send + Sync {
    /// The user-facing noun for a merge request here — GitHub's "PR",
    /// GitLab's "MR", Gitea's "PR".
    fn noun(&self) -> &'static str;

    /// The MR for `branch` matching the state filter, regardless of its current
    /// base. Base is deliberately *not* a match criterion: a caller fixing a
    /// drifted base needs to find the MR precisely when its base no longer
    /// matches, then compare and retarget it.
    fn find(&self, branch: &str, state: StateFilter) -> anyhow::Result<Option<MergeRequest>>;

    fn create(&self, req: &NewMr) -> anyhow::Result<MergeRequest>;
    fn set_base(&self, id: &str, base: &str) -> anyhow::Result<()>;
    fn set_body(&self, id: &str, body: &str) -> anyhow::Result<()>;

    /// Add labels/assignees/reviewers to an existing MR, additively and
    /// best-effort: a sub-item that fails (an unknown label, a self-review the
    /// platform forbids) is logged and skipped rather than aborting the rest.
    fn apply_attributes(&self, id: &str, attrs: &Attributes) -> anyhow::Result<()>;

    // -- Review half ---------------------------------------------------------
    //
    // These carry default `bail` bodies so a forge without a review backend
    // (Gitea today) keeps compiling and fails loudly only when review is
    // actually asked of it. GitHub and GitLab override them.

    /// The MRs matching a feed's filter, pushed down to the platform's
    /// list/search query and paginated server-side.
    fn list_mrs(&self, _query: &FeedQuery) -> anyhow::Result<Vec<MrSummary>> {
        anyhow::bail!("`wits review` has no backend for this forge yet")
    }

    /// One MR's metadata and current diff-version SHAs, addressed by its number.
    fn mr_details(&self, _id: &str) -> anyhow::Result<MrDetails> {
        anyhow::bail!("`wits review` has no backend for this forge yet")
    }

    /// The fetchable ref that exposes an MR's head on the target remote (e.g.
    /// `pull/<n>/head`), so its objects can be pulled even across a fork.
    fn mr_ref(&self, _id: &str) -> anyhow::Result<String> {
        anyhow::bail!("`wits review` has no backend for this forge yet")
    }

    /// The review discussion currently on the MR, with each thread's resolved
    /// and outdated flags.
    fn list_threads(&self, _id: &str) -> anyhow::Result<Vec<RemoteThread>> {
        anyhow::bail!("`wits review` has no backend for this forge yet")
    }

    /// Flush a batched review — verdict, summary, and new inline/file comments —
    /// as one review where the platform allows it.
    fn submit_review(&self, _id: &str, _review: &ReviewSubmission) -> anyhow::Result<()> {
        anyhow::bail!("`wits review` has no backend for this forge yet")
    }

    /// Post an MR-level conversation comment (no code anchor).
    fn comment_mr(&self, _id: &str, _body: &str) -> anyhow::Result<()> {
        anyhow::bail!("`wits review` has no backend for this forge yet")
    }

    /// Reply to an existing thread.
    fn reply(&self, _id: &str, _thread: &str, _body: &str) -> anyhow::Result<()> {
        anyhow::bail!("`wits review` has no backend for this forge yet")
    }

    /// Resolve or unresolve a thread. Deferred to future on GitHub (GraphQL-only,
    /// see the design doc); v1 supports it on GitLab.
    fn resolve(&self, _id: &str, _thread: &str, _resolved: bool) -> anyhow::Result<()> {
        anyhow::bail!("resolving threads is not supported for this forge yet")
    }

    /// A web permalink to a file (optionally a line or line range) at a ref, for
    /// expanding a `[[path:line]]` reference in a comment body. The default has
    /// no web URL and degrades to a readable `path:line@ref`; GitHub and GitLab
    /// override it with real blob URLs.
    fn permalink(&self, r#ref: &str, path: &str, lines: Option<(u32, Option<u32>)>) -> String {
        match lines {
            Some((a, Some(b))) => format!("{path}:{a}-{b}@{ref}"),
            Some((a, None)) => format!("{path}:{a}@{ref}"),
            None => format!("{path}@{ref}"),
        }
    }
}

// ----------------------------------------------------------------------------
// Transport — shared HTTP/JSON plumbing the host modules build on.
// ----------------------------------------------------------------------------

/// How a platform expects credentials presented. The differences are small but
/// real: GitHub takes a bearer/token header, GitLab a `PRIVATE-TOKEN`.
#[derive(Debug, Clone)]
pub(crate) enum Auth {
    /// `Authorization: Bearer <t>` — GitHub accepts this for every token kind.
    Bearer(String),
    /// `Authorization: token <t>` — what Gitea/Forgejo personal tokens expect.
    Token(String),
    /// `PRIVATE-TOKEN: <t>` — GitLab's own header.
    PrivateToken(String),
}

/// Issue one request and decode the JSON reply. A non-2xx status is turned into
/// an error carrying the platform's own message body, because that text is
/// usually the only thing that explains *why* (a stale token, a base that
/// doesn't exist) far better than a bare status code would.
pub(crate) fn request(
    method: &str,
    url: &str,
    auth: &Auth,
    body: Option<&Value>,
) -> anyhow::Result<Value> {
    let mut req = ureq::request(method, url)
        .set("Accept", "application/json")
        .set("User-Agent", "wits-stack");
    req = match auth {
        Auth::Bearer(t) => req.set("Authorization", &format!("Bearer {t}")),
        Auth::Token(t) => req.set("Authorization", &format!("token {t}")),
        Auth::PrivateToken(t) => req.set("PRIVATE-TOKEN", t),
    };

    let response = match body {
        Some(b) => req.send_json(b.clone()),
        None => req.call(),
    };

    match response {
        Ok(r) => Ok(r.into_json().unwrap_or(Value::Null)),
        Err(ureq::Error::Status(code, r)) => {
            let detail = r.into_string().unwrap_or_default();
            anyhow::bail!("HTTP {code}: {}", detail.trim());
        }
        Err(e) => Err(anyhow::anyhow!("request to {url} failed: {e}")),
    }
}

/// The literal a caller passes for "the authenticated user".
pub(crate) const SELF_REF: &str = "@me";

/// Replace any `@me` in `items` with the resolved name. Used by hosts that take
/// usernames (GitHub, Gitea); GitLab resolves to a numeric id separately.
pub(crate) fn resolve_self(items: &[String], me: &str) -> Vec<String> {
    items
        .iter()
        .map(|item| {
            if item == SELF_REF {
                me.to_owned()
            } else {
                item.clone()
            }
        })
        .collect()
}

/// Read one string field off `GET {api_base}/user`. The field name differs by
/// platform (`login` on GitHub/Gitea), so it is passed in.
pub(crate) fn current_user(api_base: &str, auth: &Auth, field: &str) -> anyhow::Result<String> {
    let v = request("GET", &format!("{api_base}/user"), auth, None)?;
    v[field]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| anyhow::anyhow!("could not read the authenticated user"))
}

/// Percent-encode one URL component. Branch names carry `/`, cross-fork heads
/// carry `:`, GitLab project ids are a whole `group/sub/repo` path — all of which
/// must survive intact inside a query string or path segment.
pub(crate) fn encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

// ----------------------------------------------------------------------------
// Detection & credentials.
// ----------------------------------------------------------------------------

/// Pick and configure the forge for this checkout, or explain why we can't.
///
/// The merge target decides everything: the platform we talk to is the one
/// hosting `upstream` (or `origin` when there is no fork). Service detection from
/// the hostname can be overridden per host for self-hosted instances, and a
/// token must resolve or there is nothing to authenticate with.
pub fn detect(repo: &Repository, remotes: &Remotes) -> anyhow::Result<Box<dyn Forge>> {
    let target = remotes
        .target()
        .ok_or_else(|| anyhow::anyhow!("no 'origin' or 'upstream' remote to derive a forge from"))?
        .clone();

    // A host override lets a self-hosted GitLab/Gitea behind a custom domain
    // declare itself when the hostname gives nothing away.
    let service = repo
        .get_config(&format!("workflow.platform.{}.service", target.host))
        .ok()
        .flatten()
        .and_then(|s| Service::parse(&s))
        .unwrap_or(target.service);

    // Turn away a recognized-but-unsupported host *before* hunting for a token,
    // so the failure names the real reason instead of masquerading as a missing
    // token. Bitbucket is the live case: we still parse its remotes and
    // `wits stack sync` pushes to it, but the MR verbs have no backend for it.
    match service {
        Service::GitHub
        | Service::GitLab
        | Service::Gitea
        | Service::Forgejo
        | Service::Codeberg => {}
        Service::Bitbucket => anyhow::bail!(
            "`wits stack` speaks to GitHub, GitLab and Gitea; Bitbucket has no MR backend here \
             (`wits stack sync` still pushes to it)"
        ),
        Service::Unknown => anyhow::bail!(
            "could not detect the forge for host '{}'; set workflow.platform.{}.service",
            target.host,
            target.host
        ),
    }

    let token = resolve_token(repo, &target.host, service).ok_or_else(|| {
        anyhow::anyhow!(
            "no API token for {} ({}); set workflow.platform.{}.token or the platform's *_TOKEN env var",
            target.host,
            service.as_str(),
            target.host
        )
    })?;

    // Cross-fork MRs express the head as `origin_owner:branch`; same-repo MRs
    // just use the branch name. We compute the owner once here.
    let head_owner = if remotes.is_cross_fork() {
        remotes.head_owner().map(str::to_owned)
    } else {
        None
    };

    let api_url_override = repo
        .get_config(&format!("workflow.platform.{}.api-url", target.host))
        .ok()
        .flatten();

    match service {
        Service::GitHub => Ok(Box::new(github::GitHub::new(
            target,
            head_owner,
            token,
            api_url_override,
        ))),
        // One API family, one impl: only their identities (token env, config key,
        // detection) set Gitea, Forgejo and Codeberg apart.
        Service::Gitea | Service::Forgejo | Service::Codeberg => Ok(Box::new(gitea::Gitea::new(
            target,
            head_owner,
            token,
            api_url_override,
        ))),
        Service::GitLab => Ok(Box::new(gitlab::GitLab::new(
            target,
            remotes.origin.clone(),
            token,
            api_url_override,
        )?)),
        // The unsupported services were already rejected above.
        Service::Bitbucket | Service::Unknown => unreachable!(),
    }
}

/// Find a token, most specific first: per-host config, then per-service config,
/// then a blanket config key, then the platform's conventional env var. Config
/// before env would invert the codebase's rule that the environment is the
/// deliberate, throwaway override — so env comes last only among *config*, but a
/// set env var still wins via the resolver elsewhere; here a token is a single
/// secret and the explicit per-host config is the most precise answer.
fn resolve_token(repo: &Repository, host: &str, service: Service) -> Option<String> {
    let config_keys = [
        format!("workflow.platform.{host}.token"),
        format!("workflow.platform.{}.token", service.as_str()),
        "workflow.platform.token".to_owned(),
    ];
    for key in &config_keys {
        if let Some(v) = repo.get_config(key).ok().flatten() {
            return Some(v);
        }
    }

    let env_vars: &[&str] = match service {
        Service::GitHub => &["GITHUB_TOKEN"],
        Service::GitLab => &["GITLAB_TOKEN"],
        Service::Gitea => &["GITEA_TOKEN"],
        Service::Forgejo => &["FORGEJO_TOKEN", "GITEA_TOKEN"],
        // Codeberg runs Forgejo, so it falls back to Forgejo's env.
        Service::Codeberg => &["CODEBERG_TOKEN", "FORGEJO_TOKEN"],
        Service::Bitbucket => &["BITBUCKET_TOKEN"],
        Service::Unknown => &[],
    };
    env_vars.iter().find_map(|v| std::env::var(v).ok())
}

/// Shared helper for host modules: from a list of candidate MRs (already parsed),
/// return the first that satisfies the state filter. A branch has at most one MR
/// in a given state for our purposes, so "first" is unambiguous.
pub(crate) fn pick<'a>(
    candidates: impl IntoIterator<Item = &'a MergeRequest>,
    state: StateFilter,
) -> Option<MergeRequest> {
    candidates
        .into_iter()
        .find(|mr| state.accepts(mr.state))
        .cloned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn attributes_emptiness_and_summary() {
        assert!(Attributes::default().is_empty());
        let a = Attributes {
            labels: vec!["bug".into()],
            assignees: vec!["@me".into()],
            reviewers: vec![],
        };
        assert!(!a.is_empty());
        let s = a.summary();
        assert!(s.contains("labels") && s.contains("assignees") && !s.contains("reviewers"));
    }

    #[test]
    fn resolve_self_replaces_only_the_marker() {
        let out = resolve_self(&["@me".into(), "alice".into()], "russell");
        assert_eq!(out, ["russell", "alice"]);
    }
}
