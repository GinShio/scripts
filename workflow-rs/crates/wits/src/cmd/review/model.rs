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
    DiffVersion, LineRef, MrState, MrSummary, RemoteComment, RemotePlacement, RemoteThread, Side,
    Verdict,
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

/// Where a comment sits, in the read/output view. The line placement uses the
/// nested [`LineRef`] shape so a multi-line span can cross diff sides.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Placement {
    Line {
        path: String,
        #[serde(skip_serializing_if = "Option::is_none", default)]
        old_path: Option<String>,
        /// The anchor (end) line.
        end: LineRef,
        /// The start line of a multi-line span, when set.
        #[serde(skip_serializing_if = "Option::is_none", default)]
        start: Option<LineRef>,
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
                end,
                start,
                commit,
            } => Placement::Line {
                path,
                old_path,
                end,
                start,
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
///
/// A comment's `commit` records the snapshot head SHA its line anchors were
/// written against. Set by `draft <mr> -` at ingest time; a hand-editor may set
/// it explicitly. At submit, the comment is anchored to its own commit, so
/// different actions in one draft can target different snapshots.
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
        /// First line of a multi-line span (with `line` as the end). Defaults to
        /// the same side as `side` unless `start_side` overrides it.
        #[serde(skip_serializing_if = "Option::is_none", default)]
        start_line: Option<u32>,
        /// Side of the multi-line `start_line`; defaults to `side` when absent.
        #[serde(skip_serializing_if = "Option::is_none", default)]
        start_side: Option<Side>,
        body: String,
        /// The snapshot head SHA this comment's line anchors were written
        /// against. When set, `submit` anchors the comment to this commit
        /// (the forge may mark it outdated if the head has moved). When unset,
        /// `submit` falls back to the current snapshot's head.
        #[serde(skip_serializing_if = "Option::is_none", default)]
        commit: Option<String>,
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
    /// The output placement for a comment action. The commit comes from the
    /// action itself (per-comment snapshot); `head` is the fallback for actions
    /// that predate per-comment stamping. Non-comment actions have no placement.
    pub fn placement(&self, head: &str) -> Option<Placement> {
        match self {
            Action::Comment {
                file: Some(path),
                line: Some(line),
                side,
                start_line,
                start_side,
                commit,
                ..
            } => {
                let c = commit
                    .clone()
                    .or_else(|| (!head.is_empty()).then(|| head.to_owned()));
                let s = side.unwrap_or(Side::New);
                let end = LineRef {
                    line: *line,
                    side: s,
                };
                let start = start_line.map(|sl| LineRef {
                    line: sl,
                    side: start_side.unwrap_or(s),
                });
                Some(Placement::Line {
                    path: path.clone(),
                    old_path: None,
                    end,
                    start,
                    commit: c,
                })
            }
            Action::Comment {
                file: Some(path),
                commit,
                ..
            } => {
                let c = commit
                    .clone()
                    .or_else(|| (!head.is_empty()).then(|| head.to_owned()));
                Some(Placement::File {
                    path: path.clone(),
                    commit: c,
                })
            }
            Action::Comment { .. } => Some(Placement::Mr),
            _ => None,
        }
    }
}

/// `local.json` — your unsubmitted review: an optional verdict and summary, and
/// an append-style list of actions. Edited by hand or an editor; `submit` merges
/// and flushes it. This shape is the public write contract.
///
/// Each `Comment` action carries its own `commit` — the snapshot head SHA its
/// line anchors were written against. This makes cross-snapshot drafting safe:
/// different comments in one draft can target different snapshots, and `submit`
/// anchors each to its own commit.
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

/// The canonical, forge-facing form of a thread id: the bare forge id, with any
/// `remote:` prefix (`show`/`local.json` print ids in that form) stripped. The
/// draft's `thread` field on `reply`/`resolve` accepts either the bare id or the
/// `remote:` form and treats the two as the same thread, so the prefix must not
/// survive into the forge URL or into the read-fold's id match — `remote:9987`
/// stamped verbatim would become `remote:remote:9987` against a thread whose id
/// is `remote:9987`, matching nothing (fold) or 404ing (submit).
pub fn bare_thread_id(thread: &str) -> &str {
    thread.strip_prefix("remote:").unwrap_or(thread)
}

/// Truncate a SHA to a display-friendly prefix (11 hex chars, like `git log`).
pub fn short(sha: &str) -> &str {
    &sha[..sha.len().min(11)]
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
    ///
    /// Also stamps each `Comment` action's `commit` with `head` when it is not
    /// already set, so hand-edited or pre-commit drafts get anchored to the
    /// current snapshot, and canonicalizes a `reply`/`resolve` `thread` to the
    /// bare forge id so a `remote:<id>` written into the draft can't leak into a
    /// forge URL (a 404) or the read-fold's id match (a miss).
    pub fn normalize(&mut self, head: &str) {
        // Last-wins per resolved thread: find the final resolve for each thread,
        // keyed on the bare forge id so `remote:9987` and `9987` collapse together.
        // The stored form is the bare id (canonical) so neither the read-fold nor
        // submit ever has to deal with a stray prefix.
        let mut last_resolve: std::collections::HashMap<String, bool> =
            std::collections::HashMap::new();
        for a in &self.actions {
            if let Action::Resolve { thread, resolved } = a {
                last_resolve.insert(bare_thread_id(thread).to_owned(), *resolved);
            }
        }

        let mut seen: Vec<Action> = Vec::new();
        let mut resolved_emitted: std::collections::HashSet<String> =
            std::collections::HashSet::new();
        for mut a in self.actions.drain(..) {
            // Stamp unstamped comments with the current snapshot head, so
            // hand-edited drafts get anchored before dedup and submission.
            if let Action::Comment { ref mut commit, .. } = a {
                if commit.is_none() && !head.is_empty() {
                    *commit = Some(head.to_owned());
                }
            }
            // Canonicalize thread-targeting actions to the bare forge id, so the
            // stored draft never carries a `remote:` prefix that the read-fold or
            // submit would have to strip.
            match &mut a {
                Action::Reply { thread, .. } | Action::Resolve { thread, .. } => {
                    *thread = bare_thread_id(thread).to_owned();
                }
                _ => {}
            }
            match &a {
                Action::Resolve { thread, .. } => {
                    let bare = thread.clone();
                    if resolved_emitted.insert(bare.clone()) {
                        let resolved = last_resolve[bare.as_str()];
                        seen.push(Action::Resolve {
                            thread: bare,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn comment(file: &str, line: u32, commit: Option<&str>) -> Action {
        Action::Comment {
            file: Some(file.into()),
            line: Some(line),
            side: None,
            start_line: None,
            start_side: None,
            body: "x".into(),
            commit: commit.map(str::to_owned),
        }
    }

    #[test]
    fn normalize_stamps_unstamped_comments_with_current_head() {
        let mut local = Local {
            schema: SCHEMA,
            verdict: None,
            summary: None,
            actions: vec![comment("a.c", 1, None)],
        };
        local.normalize("deadbeef");
        match &local.actions[0] {
            Action::Comment { commit, .. } => assert_eq!(commit.as_deref(), Some("deadbeef")),
            _ => panic!("expected a comment"),
        }
    }

    #[test]
    fn normalize_leaves_explicit_commit_intact() {
        let mut local = Local {
            schema: SCHEMA,
            verdict: None,
            summary: None,
            actions: vec![
                comment("a.c", 1, Some("older")),
                comment("a.c", 1, Some("older")),
            ],
        };
        local.normalize("deadbeef");
        // Dedup drops the exact repeat, and the survivor keeps its own commit,
        // not the current head.
        assert_eq!(local.actions.len(), 1);
        match &local.actions[0] {
            Action::Comment { commit, .. } => assert_eq!(commit.as_deref(), Some("older")),
            _ => panic!("expected a comment"),
        }
    }

    #[test]
    fn normalize_dedup_keeps_distinct_commits() {
        // Same file/line but different snapshots are two distinct intents —
        // both survive dedup (cross-snapshot drafting).
        let mut local = Local {
            schema: SCHEMA,
            verdict: None,
            summary: None,
            actions: vec![
                comment("a.c", 1, Some("snapA")),
                comment("a.c", 1, Some("snapB")),
            ],
        };
        local.normalize("deadbeef");
        assert_eq!(local.actions.len(), 2);
    }

    #[test]
    fn normalize_collapses_repeated_resolves_to_the_last() {
        let mut local = Local {
            schema: SCHEMA,
            verdict: None,
            summary: None,
            actions: vec![
                Action::Resolve {
                    thread: "42".into(),
                    resolved: true,
                },
                Action::Resolve {
                    thread: "42".into(),
                    resolved: false,
                },
                Action::Resolve {
                    thread: "42".into(),
                    resolved: true,
                },
            ],
        };
        local.normalize("deadbeef");
        assert_eq!(local.actions.len(), 1);
        match &local.actions[0] {
            Action::Resolve { thread, resolved } => {
                assert_eq!(thread, "42");
                assert!(*resolved, "last write wins");
            }
            _ => panic!("expected a resolve"),
        }
    }

    #[test]
    fn normalize_collapses_remote_prefix_to_bare_thread_id() {
        // A draft written with `remote:42` and one written with the bare `42`
        // refer to the same thread; normalize keys them together (last write
        // wins) and stores the canonical bare form, so neither show's fold nor
        // submit ever sees a `remote:` prefix leak into the forge URL.
        let mut local = Local {
            schema: SCHEMA,
            verdict: None,
            summary: None,
            actions: vec![
                Action::Resolve {
                    thread: "remote:42".into(),
                    resolved: false,
                },
                Action::Resolve {
                    thread: "42".into(),
                    resolved: true,
                },
                Action::Reply {
                    thread: "remote:42".into(),
                    body: "x".into(),
                },
            ],
        };
        local.normalize("deadbeef");
        // The two resolves collapse to one bare-id action, last value wins.
        let resolves: Vec<_> = local
            .actions
            .iter()
            .filter(|a| matches!(a, Action::Resolve { .. }))
            .collect();
        assert_eq!(resolves.len(), 1);
        match &resolves[0] {
            Action::Resolve { thread, resolved } => {
                assert_eq!(thread, "42");
                assert!(*resolved, "last write wins");
            }
            _ => unreachable!(),
        }
        // The reply keeps its body; its thread is normalized to bare too.
        match &local
            .actions
            .iter()
            .find(|a| matches!(a, Action::Reply { .. }))
            .unwrap()
        {
            Action::Reply { thread, .. } => assert_eq!(thread, "42"),
            _ => unreachable!(),
        }
    }

    #[test]
    fn bare_thread_id_strips_the_remote_prefix() {
        assert_eq!(bare_thread_id("remote:9987"), "9987");
        assert_eq!(bare_thread_id("9987"), "9987");
    }

    #[test]
    fn normalize_is_idempotent() {
        let mut local = Local {
            schema: SCHEMA,
            verdict: None,
            summary: None,
            actions: vec![
                comment("a.c", 1, Some("snap")),
                comment("a.c", 1, Some("snap")),
                Action::Resolve {
                    thread: "42".into(),
                    resolved: true,
                },
                Action::Resolve {
                    thread: "42".into(),
                    resolved: false,
                },
            ],
        };
        local.normalize("deadbeef");
        let once = local.actions.clone();
        local.normalize("deadbeef");
        assert_eq!(local.actions, once, "a second normalize is a no-op");
    }
}
