//! The review model: the serde types that back both the on-disk store and the
//! `--json` contract.
//!
//! These deliberately double as the store *and* the editor payload. The
//! on-disk layout is a private implementation detail (the editor reads only
//! through `--json`), but keeping one set of types means the thing we persist
//! and the thing we emit can never drift. Everything is versioned by
//! [`SCHEMA`] so the payload can evolve.
//!
//! Two lifetimes, kept apart on purpose (see `docs/review/design.md` §7): the
//! [`RemoteCache`] is the forge's state as we last saw it — refetchable, safe to
//! overwrite whole — while the [`Draft`] is the precious, unsubmitted set of
//! actions the reviewer is building.

use serde::{Deserialize, Serialize};

use wits_util::forge::{
    DiffVersion, MrState, MrSummary, RemoteComment, RemotePlacement, RemoteThread, Side, Verdict,
};

/// The version of the store/JSON schema. Bumped when a payload shape changes so
/// a reader can refuse or migrate an older store.
pub const SCHEMA: u32 = 1;

/// The normalized state string used in JSON, folding draft-ness into the word a
/// reviewer thinks in.
pub fn state_word(state: MrState, draft: bool) -> &'static str {
    match state {
        MrState::Open if draft => "draft",
        MrState::Open => "open",
        MrState::Merged => "merged",
        MrState::Closed => "closed",
    }
}

/// The inbox/detail view of one MR's metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MrInfo {
    pub id: String,
    pub display: String,
    pub state: String,
    pub draft: bool,
    pub title: String,
    pub author: String,
    pub base: String,
    pub source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub head_sha: Option<String>,
    pub updated_at: String,
    pub labels: Vec<String>,
    pub web_url: String,
}

impl From<MrSummary> for MrInfo {
    fn from(s: MrSummary) -> Self {
        MrInfo {
            state: state_word(s.state, s.draft).to_owned(),
            id: s.id,
            display: s.display,
            draft: s.draft,
            title: s.title,
            author: s.author,
            base: s.base,
            source: s.source,
            head_sha: s.head_sha,
            updated_at: s.updated_at,
            labels: s.labels,
            web_url: s.web_url,
        }
    }
}

/// Where a comment sits. The `commit` is the reviewed SHA the anchor belongs to
/// — carried so an outdated comment submits against what was reviewed rather
/// than being silently re-based onto the tip.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Placement {
    Line {
        path: String,
        #[serde(skip_serializing_if = "Option::is_none", default)]
        old_path: Option<String>,
        side: Side,
        line: u32,
        #[serde(skip_serializing_if = "Option::is_none", default)]
        start_line: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none", default)]
        commit: Option<String>,
    },
    File {
        path: String,
        #[serde(skip_serializing_if = "Option::is_none", default)]
        commit: Option<String>,
    },
    /// The MR conversation, with no code anchor.
    Mr,
}

impl From<RemotePlacement> for Placement {
    fn from(p: RemotePlacement) -> Self {
        match p {
            RemotePlacement::Line {
                path,
                old_path,
                side,
                line,
                commit,
            } => Placement::Line {
                path,
                old_path,
                side,
                line,
                start_line: None,
                commit,
            },
            RemotePlacement::File { path } => Placement::File { path, commit: None },
            RemotePlacement::Mr => Placement::Mr,
        }
    }
}

/// One comment in a thread — origin-tagged (`local`/`remote`) and state-tagged
/// (`pending`/`published`) so the editor can render each appropriately.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Comment {
    pub id: String,
    pub author: String,
    pub origin: String,
    pub body: String,
    #[serde(skip_serializing_if = "String::is_empty", default)]
    pub created_at: String,
    pub state: String,
}

impl Comment {
    fn from_remote(c: RemoteComment) -> Self {
        Comment {
            id: format!("remote:{}", c.id),
            author: c.author,
            origin: "remote".into(),
            body: c.body,
            created_at: c.created_at,
            state: "published".into(),
        }
    }
}

/// A discussion thread as presented to the editor: remote threads carry their
/// forge id and flags; a purely local pending thread carries a `local:` id.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Thread {
    pub id: String,
    pub origin: String,
    pub resolved: bool,
    pub outdated: bool,
    pub placement: Placement,
    pub comments: Vec<Comment>,
}

impl From<RemoteThread> for Thread {
    fn from(t: RemoteThread) -> Self {
        Thread {
            id: format!("remote:{}", t.id),
            origin: "remote".into(),
            resolved: t.resolved,
            outdated: t.outdated,
            placement: t.placement.into(),
            comments: t.comments.into_iter().map(Comment::from_remote).collect(),
        }
    }
}

/// One commit in the reviewed range.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoredCommit {
    pub sha: String,
    pub subject: String,
}

/// One file the MR touched.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoredFile {
    pub path: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub old_path: Option<String>,
    pub status: String,
}

/// The forge's state for one MR as we last observed it — a cache, refetchable at
/// will and safe to overwrite whole.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RemoteCache {
    pub schema: u32,
    pub mr: MrInfo,
    pub version: DiffVersion,
    pub fetched_at: String,
    pub commits: Vec<StoredCommit>,
    pub files: Vec<StoredFile>,
    pub threads: Vec<Thread>,
}

/// One recorded, not-yet-submitted review action.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "kebab-case")]
pub enum Action {
    /// A new thread (line/file/mr).
    Comment {
        id: String,
        placement: Placement,
        body: String,
    },
    /// A reply to an existing remote thread.
    Reply {
        id: String,
        /// The remote thread id (bare, without the `remote:` prefix).
        thread: String,
        body: String,
    },
    /// Resolve or unresolve a remote thread (GitLab in v1).
    Resolve { thread: String, resolved: bool },
}

impl Action {
    /// The addressable id of an action that has one (comments and replies).
    pub fn id(&self) -> Option<&str> {
        match self {
            Action::Comment { id, .. } | Action::Reply { id, .. } => Some(id),
            Action::Resolve { .. } => None,
        }
    }
}

/// The one mutable local document: the unsubmitted review for a single MR.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Draft {
    pub schema: u32,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub verdict: Option<Verdict>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub summary: Option<String>,
    pub actions: Vec<Action>,
    /// Monotonic local-id counter; never reused after a drop, reset when the
    /// draft is cleared.
    pub seq: u64,
}

impl Default for Draft {
    fn default() -> Self {
        Draft {
            schema: SCHEMA,
            verdict: None,
            summary: None,
            actions: Vec::new(),
            seq: 0,
        }
    }
}

impl Draft {
    /// Allocate the next local id (`local:N`).
    pub fn next_id(&mut self) -> String {
        self.seq += 1;
        format!("local:{}", self.seq)
    }

    pub fn is_empty(&self) -> bool {
        self.verdict.is_none() && self.summary.is_none() && self.actions.is_empty()
    }

    /// Remove the action with `id`; returns whether one was found.
    pub fn remove(&mut self, id: &str) -> bool {
        let before = self.actions.len();
        self.actions.retain(|a| a.id() != Some(id));
        self.actions.len() != before
    }

    /// Set the body of the comment/reply action with `id`; returns whether found.
    pub fn edit_body(&mut self, id: &str, new_body: String) -> bool {
        for action in &mut self.actions {
            match action {
                Action::Comment { id: aid, body, .. } | Action::Reply { id: aid, body, .. }
                    if aid == id =>
                {
                    *body = new_body;
                    return true;
                }
                _ => {}
            }
        }
        false
    }

    /// Record a resolve/unresolve, collapsing repeats on the same thread to the
    /// latest intent.
    pub fn set_resolved(&mut self, thread: String, resolved: bool) {
        self.actions
            .retain(|a| !matches!(a, Action::Resolve { thread: t, .. } if *t == thread));
        self.actions.push(Action::Resolve { thread, resolved });
    }
}
