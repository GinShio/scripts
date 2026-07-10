//! The review model: the JSON types that back the three-file store and the
//! `--json` read contract.
//!
//! Every MR is described by three files (see `docs/review/store.md`), all JSON
//! because they are API-shaped data:
//!
//! - [`Info`]    — the MR's necessary metadata and diff state (drives the inbox).
//! - [`Comments`] — everything that happened on the forge (a refetchable cache).
//! - [`Local`]   — your unsubmitted actions, edited by hand or an editor; this
//!   is the sole *write* interface, so its shape is a public, versioned contract.
//!
//! There are no authoring commands: to review, you edit `local.json`, then
//! `submit` merges, posts, and clears it. Everything is versioned by [`SCHEMA`].

use serde::{Deserialize, Serialize};

use wits_util::forge::{
    DiffVersion, MrState, MrSummary, RemoteComment, RemotePlacement, RemoteThread, Side, Verdict,
};

/// The store/JSON schema version. Bumped on an incompatible shape change.
pub const SCHEMA: u32 = 1;

/// The normalized state word used in JSON, folding draft-ness in.
pub fn state_word(state: MrState, draft: bool) -> &'static str {
    match state {
        MrState::Open if draft => "draft",
        MrState::Open => "open",
        MrState::Merged => "merged",
        MrState::Closed => "closed",
    }
}

/// One MR's necessary metadata — the inbox row, and the header of the detail
/// view.
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
    #[serde(skip_serializing_if = "Option::is_none", default)]
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

/// A commit in the reviewed range.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoredCommit {
    pub sha: String,
    pub subject: String,
}

/// A file the MR touched.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StoredFile {
    pub path: String,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub old_path: Option<String>,
    pub status: String,
}

/// One fetched, pinned review point — a *snapshot*, distinct from an ad-hoc diff
/// *range*: a snapshot is a historical identity whose objects are held alive by a
/// `refs/wits/review/*` pin, whereas a range is a throwaway query.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Snapshot {
    pub base_sha: String,
    pub start_sha: String,
    pub head_sha: String,
    /// Unix timestamp of the fetch that first recorded this snapshot.
    pub fetched_at: String,
}

impl Snapshot {
    /// The forge diff-version SHAs for anchoring a comment on this snapshot.
    pub fn version(&self) -> DiffVersion {
        DiffVersion {
            base_sha: self.base_sha.clone(),
            start_sha: self.start_sha.clone(),
            head_sha: self.head_sha.clone(),
        }
    }
}

/// `info.json` — the MR's necessary metadata and its snapshot history. A pure
/// **cache**: `fetch` overwrites it, so it is not meant to be hand-edited. A feed
/// refresh fills only `mr`, leaving the snapshot history empty until a full
/// `fetch <mr>`. `commits`/`files` describe the current (latest) snapshot.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Info {
    pub schema: u32,
    pub mr: MrInfo,
    /// Every review point we have fetched and pinned, oldest first; the last is
    /// the current one.
    #[serde(default)]
    pub snapshots: Vec<Snapshot>,
    pub commits: Vec<StoredCommit>,
    pub files: Vec<StoredFile>,
}

impl Info {
    /// The current (latest) snapshot, if any has been fetched.
    pub fn current(&self) -> Option<&Snapshot> {
        self.snapshots.last()
    }

    /// The current snapshot's head SHA, or empty when none is fetched.
    pub fn head(&self) -> &str {
        self.current().map(|s| s.head_sha.as_str()).unwrap_or("")
    }

    /// Record a freshly-fetched snapshot, appending it only when the head moved
    /// (so a re-fetch of an unchanged MR doesn't grow the history).
    pub fn record_snapshot(&mut self, snapshot: Snapshot) {
        if self.snapshots.last().map(|s| &s.head_sha) != Some(&snapshot.head_sha) {
            self.snapshots.push(snapshot);
        }
    }
}

/// Where a comment sits, in the read/output view.
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

/// One comment in a thread, in the read/output view.
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

/// A discussion thread in the read/output view.
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

/// `comments.json` — the forge's discussion, as last fetched. A pure cache.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Comments {
    pub schema: u32,
    pub threads: Vec<Thread>,
}

impl Default for Comments {
    fn default() -> Self {
        Comments {
            schema: SCHEMA,
            threads: Vec::new(),
        }
    }
}

/// One recorded action in `local.json`. Flat on purpose, so it is pleasant to
/// hand-edit: a comment infers its placement from which fields are present —
/// `file`+`line` is a line comment, `file` alone is file-level, neither is an
/// MR-level conversation comment.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "kebab-case")]
pub enum Action {
    Comment {
        #[serde(skip_serializing_if = "Option::is_none", default)]
        file: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none", default)]
        line: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none", default)]
        side: Option<Side>,
        #[serde(skip_serializing_if = "Option::is_none", default)]
        start_line: Option<u32>,
        body: String,
    },
    Reply {
        thread: String,
        body: String,
    },
    Resolve {
        thread: String,
        resolved: bool,
    },
}

impl Action {
    /// The output placement for a comment action, stamped with the reviewed
    /// `head`. Non-comment actions have no placement.
    pub fn placement(&self, head: &str) -> Option<Placement> {
        let commit = (!head.is_empty()).then(|| head.to_owned());
        match self {
            Action::Comment {
                file: Some(path),
                line: Some(line),
                side,
                start_line,
                ..
            } => Some(Placement::Line {
                path: path.clone(),
                old_path: None,
                side: side.unwrap_or(Side::New),
                line: *line,
                start_line: *start_line,
                commit,
            }),
            Action::Comment {
                file: Some(path), ..
            } => Some(Placement::File {
                path: path.clone(),
                commit,
            }),
            Action::Comment { .. } => Some(Placement::Mr),
            _ => None,
        }
    }
}

/// `local.json` — your unsubmitted review: an optional verdict and summary, and
/// an append-style list of actions. Edited by hand or an editor; `submit` merges
/// and flushes it. This shape is the public write contract.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Local {
    pub schema: u32,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub verdict: Option<Verdict>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub summary: Option<String>,
    #[serde(default)]
    pub actions: Vec<Action>,
}

impl Default for Local {
    fn default() -> Self {
        Local {
            schema: SCHEMA,
            verdict: None,
            summary: None,
            actions: Vec::new(),
        }
    }
}

impl Local {
    pub fn is_empty(&self) -> bool {
        self.verdict.is_none() && self.summary.is_none() && self.actions.is_empty()
    }

    /// Merge and de-duplicate the recorded actions, in place: drop exact repeats
    /// (a comment written twice), and collapse repeated resolutions of one
    /// thread to the last stated intent. Order is otherwise preserved.
    pub fn normalize(&mut self) {
        // Last-wins per resolved thread: find the final resolve for each thread.
        let mut last_resolve: std::collections::HashMap<String, bool> =
            std::collections::HashMap::new();
        for a in &self.actions {
            if let Action::Resolve { thread, resolved } = a {
                last_resolve.insert(thread.clone(), *resolved);
            }
        }

        let mut seen: Vec<Action> = Vec::new();
        let mut resolved_emitted: std::collections::HashSet<String> =
            std::collections::HashSet::new();
        for a in self.actions.drain(..) {
            match &a {
                Action::Resolve { thread, .. } => {
                    if resolved_emitted.insert(thread.clone()) {
                        let resolved = last_resolve[thread];
                        seen.push(Action::Resolve {
                            thread: thread.clone(),
                            resolved,
                        });
                    }
                }
                _ if seen.contains(&a) => {}
                _ => seen.push(a),
            }
        }
        self.actions = seen;
    }
}
