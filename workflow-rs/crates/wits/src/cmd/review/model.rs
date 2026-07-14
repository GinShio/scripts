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
    Anchor, DiffVersion, LineRef, MrState, MrSummary, RemoteComment, RemoteThread, Side, Verdict,
};
use wits_util::git::Repository;

/// The store/JSON schema version. (Personal tooling — shapes change freely
/// without a bump; this stays `1`.)
pub const SCHEMA: u32 = 1;

/// The human/inbox state word, folding draft-ness into the lifecycle for a
/// one-glance label. `--json` keeps `state` and `draft` as separate typed fields
/// (below); this is only for the terse human line.
pub fn state_word(state: MrState, draft: bool) -> &'static str {
    match state {
        MrState::Open if draft => "draft",
        MrState::Open => "open",
        MrState::Merged => "merged",
        MrState::Closed => "closed",
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

/// A git range's commits (oldest first) and changed files, lifted into the
/// stored review shapes. The single place `repo.commits`/`changed_files` become
/// [`StoredCommit`]/[`StoredFile`] — shared by `fetch` (a snapshot's artifacts)
/// and `diff` (an ad-hoc range), so the mapping can't drift between them.
pub fn range_artifacts(repo: &Repository, range: &str) -> (Vec<StoredCommit>, Vec<StoredFile>) {
    let commits = repo
        .commits(range)
        .into_iter()
        .map(|c| StoredCommit {
            sha: c.hash,
            subject: c.subject,
        })
        .collect();
    let files = repo
        .changed_files(range)
        .into_iter()
        .map(|f| StoredFile {
            path: f.path,
            old_path: f.old_path,
            status: f.status.to_string(),
        })
        .collect();
    (commits, files)
}

/// `info.json` — the MR's necessary metadata and its snapshot history. A pure
/// **cache**: `fetch` overwrites it, so it is not meant to be hand-edited. A feed
/// refresh fills only `mr`, leaving the snapshot history empty until a full
/// `fetch <mr>`. `commits`/`files` describe the current (latest) snapshot.
///
/// A *snapshot* in the history is exactly a [`DiffVersion`] — the `base/start/
/// head` triple we diff against and pin (`refs/wits/review/*`), distinct from an
/// ad-hoc diff *range* (a throwaway query). When each was first seen is
/// deliberately **not** stored per-snapshot: dormancy is about the last time the
/// MR was synced, so that lives once on [`Info::fetched_at`] and is refreshed on
/// every fetch — a re-fetch of an unchanged head must not look dormant.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Info {
    pub schema: u32,
    pub mr: MrSummary,
    /// Every review point we have fetched and pinned, oldest first; the last is
    /// the current one. Each is a `base/start/head` diff version.
    #[serde(default)]
    pub snapshots: Vec<DiffVersion>,
    /// Unix time (seconds) of the last `fetch` that synced this MR — updated on
    /// *every* fetch, even when the head hasn't moved, so `prune`'s dormancy
    /// reflects real staleness rather than when a snapshot first appeared. `0`
    /// means never fully fetched (a feed-only entry).
    #[serde(default)]
    pub fetched_at: i64,
    pub commits: Vec<StoredCommit>,
    pub files: Vec<StoredFile>,
}

impl Info {
    /// The current (latest) snapshot, if any has been fetched.
    pub fn current(&self) -> Option<&DiffVersion> {
        self.snapshots.last()
    }

    /// The current snapshot's head SHA — `None` until a full `fetch <mr>` has
    /// recorded a snapshot (a feed refresh leaves the history empty).
    pub fn head(&self) -> Option<&str> {
        self.current().map(|s| s.head_sha.as_str())
    }

    /// Record a freshly-fetched snapshot, appending it only when the head moved
    /// (so a re-fetch of an unchanged MR doesn't grow the history).
    pub fn record_snapshot(&mut self, version: DiffVersion) {
        if self.snapshots.last().map(|s| &s.head_sha) != Some(&version.head_sha) {
            self.snapshots.push(version);
        }
    }
}

/// The single source of the anchor-inference rule, shared by the read view
/// ([`Action::read_anchor`]) and submit (`build_batch`): `file`+`line` ⇒ a
/// line anchor, `file` alone ⇒ a file anchor, neither ⇒ `None` (an MR-level
/// conversation comment, which carries no code anchor). `side` defaults to
/// `New`; a multi-line `start`'s side defaults to `end`'s unless overridden.
/// `old_path` is looked up by the caller (submit knows the changed-file set;
/// the read view of a pending local comment does not, and passes `None`).
pub fn comment_anchor(
    file: Option<&str>,
    line: Option<u32>,
    side: Option<Side>,
    start_line: Option<u32>,
    start_side: Option<Side>,
    old_path: Option<String>,
) -> Option<Anchor> {
    let path = file?.to_owned();
    Some(match line {
        Some(line) => {
            let s = side.unwrap_or(Side::New);
            let end = LineRef { line, side: s };
            let start = start_line.map(|sl| LineRef {
                line: sl,
                side: start_side.unwrap_or(s),
            });
            Anchor::Line {
                path,
                old_path,
                end,
                start,
            }
        }
        None => Anchor::File { path },
    })
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

/// A discussion thread in the read/output view. `anchor` is the code anchor
/// (absent for an MR-level conversation), serialized directly as `{"kind":…}`;
/// `commit` is the snapshot SHA the anchor was written against, and drives local
/// outdate computation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Thread {
    pub id: String,
    pub origin: String,
    pub resolved: bool,
    pub outdated: bool,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub anchor: Option<Anchor>,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub commit: Option<String>,
    pub comments: Vec<Comment>,
}

impl From<RemoteThread> for Thread {
    fn from(t: RemoteThread) -> Self {
        Thread {
            id: format!("remote:{}", t.id),
            origin: "remote".into(),
            resolved: t.resolved,
            outdated: t.outdated,
            anchor: t.anchor,
            commit: t.commit,
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
    /// The read-view anchor and the commit it was written against, for a comment
    /// action, via the shared [`comment_anchor`] inference. The commit is the
    /// action's own (per-comment snapshot), falling back to `head` for actions
    /// that predate per-comment stamping. Returns `(None, None)` for a non-comment
    /// action and for an MR-level comment (no code anchor).
    pub fn read_anchor(&self, head: Option<&str>) -> (Option<Anchor>, Option<String>) {
        match self {
            Action::Comment {
                file,
                line,
                side,
                start_line,
                start_side,
                commit,
                ..
            } => {
                let commit = commit.clone().or_else(|| head.map(str::to_owned));
                let anchor = comment_anchor(
                    file.as_deref(),
                    *line,
                    *side,
                    *start_line,
                    *start_side,
                    None,
                );
                (anchor, commit)
            }
            _ => (None, None),
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
        self.stamp_comments(head);
        self.canonicalize_threads();
        self.collapse();
    }

    /// Anchor any unstamped comment to the current snapshot head, so hand-edited
    /// or pre-commit drafts get a `commit` before dedup and submission.
    fn stamp_comments(&mut self, head: &str) {
        if head.is_empty() {
            return;
        }
        for a in &mut self.actions {
            if let Action::Comment { commit, .. } = a {
                if commit.is_none() {
                    *commit = Some(head.to_owned());
                }
            }
        }
    }

    /// Rewrite every `reply`/`resolve` thread to the bare forge id, so a draft
    /// written with a `remote:` prefix can't leak that prefix into a forge URL (a
    /// 404) or the read-fold's id match (a miss). After this pass, thread ids are
    /// canonical, so later passes can key on them directly.
    fn canonicalize_threads(&mut self) {
        for a in &mut self.actions {
            if let Action::Reply { thread, .. } | Action::Resolve { thread, .. } = a {
                *thread = bare_thread_id(thread).to_owned();
            }
        }
    }

    /// Drop exact-duplicate actions and collapse repeated resolutions of one
    /// thread to the last stated intent, preserving first-seen order. Assumes
    /// thread ids are already canonical ([`canonicalize_threads`]).
    fn collapse(&mut self) {
        use std::collections::{HashMap, HashSet};
        let last_resolve: HashMap<String, bool> = self
            .actions
            .iter()
            .filter_map(|a| match a {
                Action::Resolve { thread, resolved } => Some((thread.clone(), *resolved)),
                _ => None,
            })
            .collect();

        let mut out: Vec<Action> = Vec::new();
        let mut resolved_emitted: HashSet<String> = HashSet::new();
        for a in self.actions.drain(..) {
            match &a {
                Action::Resolve { thread, .. } => {
                    if resolved_emitted.insert(thread.clone()) {
                        let resolved = last_resolve[thread];
                        out.push(Action::Resolve {
                            thread: thread.clone(),
                            resolved,
                        });
                    }
                }
                _ if out.contains(&a) => {}
                _ => out.push(a),
            }
        }
        self.actions = out;
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
