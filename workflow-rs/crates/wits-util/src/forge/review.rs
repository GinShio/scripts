//! The review half of the forge boundary: the normalized types the `review`
//! command speaks in, kept free of any platform's JSON shape.
//!
//! Where the MR half (in `super`) is four small primitives, review touches
//! corners of the platforms that do *not* normalize cleanly — batched reviews,
//! approve-as-a-separate-call, GraphQL-only thread resolution. Those differences
//! are trapped inside each host module and surfaced honestly in
//! `docs/review/design.md`'s capability matrix; here we define only the shapes
//! that cross the boundary. Nothing above the [`Forge`](super::Forge) trait ever
//! sees raw JSON.

use serde::{Deserialize, Serialize};

/// Which side of a diff a line lives on. The `New` side is the post-image
/// (added and context lines); `Old` is the pre-image, used only for a line that
/// a change deleted. Shared with the local model so an anchor round-trips
/// without translation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Side {
    Old,
    New,
}

impl Side {
    pub fn as_str(self) -> &'static str {
        match self {
            Side::Old => "old",
            Side::New => "new",
        }
    }
}

/// One endpoint of a line anchor: a file line and the diff side it lives on.
/// Each endpoint carries its own side so a multi-line span can cross sides — a
/// comment starting on a deleted (old-side) line and ending on an added
/// (new-side) one. This is the single nested shape every placement speaks in;
/// each forge translates it to its native anchor form (GitHub `line`/`side`/
/// `start_line`/`start_side`; GitLab `position.line_range{start,end}`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct LineRef {
    pub line: u32,
    pub side: Side,
}

/// A reviewer's verdict on an MR. `RequestChanges` is a GitHub/Gitea concept;
/// GitLab has no native equivalent and maps it to "leave a comment review and do
/// not approve" (see the capability matrix).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum Verdict {
    Approve,
    RequestChanges,
    Comment,
}

impl Verdict {
    /// A short, stable display string for logs and dry-run output.
    pub fn display_str(self) -> &'static str {
        match self {
            Verdict::Approve => "approve",
            Verdict::RequestChanges => "request-changes",
            Verdict::Comment => "comment",
        }
    }
}

/// The lifecycle states a feed pulls. `merged`/`closed` are deliberately never
/// fetched — a review inbox is about live work.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FeedStates {
    pub open: bool,
    pub draft: bool,
}

impl Default for FeedStates {
    /// Open and draft together — the default a feed carries when unspecified.
    fn default() -> Self {
        Self {
            open: true,
            draft: true,
        }
    }
}

/// A feed's faceted filter, translated to each platform's list/search query and
/// pushed down server-side (never applied client-side after a full fetch). Fields
/// are AND-ed together; the multiple values within a field are OR-ed.
#[derive(Debug, Clone, Default)]
pub struct FeedQuery {
    pub states: FeedStates,
    pub labels: Vec<String>,
    pub exclude_labels: Vec<String>,
    pub author: Option<String>,
    pub assignee: Option<String>,
    pub reviewer: Option<String>,
    /// A raw platform search string, the escape hatch for the full-text case.
    pub search: Option<String>,
    /// Only MRs updated at or after this ISO-8601 instant (incremental sync).
    pub updated_after: Option<String>,
    /// A hard cap on how many MRs to pull, so a huge repo can't flood the inbox.
    pub limit: usize,
}

/// The rich per-MR view the inbox needs — more than the terse [`MergeRequest`]
/// the stack verbs use, because a reviewer scans by title/author/staleness.
///
/// [`MergeRequest`]: super::MergeRequest
#[derive(Debug, Clone)]
pub struct MrSummary {
    pub id: String,
    pub display: String,
    pub state: super::MrState,
    pub draft: bool,
    pub title: String,
    pub author: String,
    /// The MR's target branch — what it merges into.
    pub base: String,
    /// The MR's source branch, so a stack can be reconstructed by linking one
    /// MR's `base` to another's `source` (empty when the platform withholds it,
    /// e.g. a search result).
    pub source: String,
    pub head_sha: Option<String>,
    pub updated_at: String,
    pub labels: Vec<String>,
    pub web_url: String,
}

/// The diff coordinates a comment must carry to anchor on the forge. GitLab needs
/// all three (`base`, `start`, `head`); GitHub needs only `head_sha` as the
/// `commit_id`, so its `start_sha` mirrors `base_sha`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct DiffVersion {
    pub base_sha: String,
    pub start_sha: String,
    pub head_sha: String,
}

/// Everything the forge can tell us about one MR that local git cannot: its
/// metadata and the current diff-version SHAs. The commit list and file set are
/// derived locally from the fetched objects, not from here.
#[derive(Debug, Clone)]
pub struct MrDetails {
    pub summary: MrSummary,
    pub version: DiffVersion,
}

/// Where a remote thread is anchored, normalized across platforms.
#[derive(Debug, Clone)]
pub enum RemotePlacement {
    /// On a code line of a changed file. `end` is the anchor line; `start`, when
    /// present, makes it a multi-line span (and may sit on the other side).
    Line {
        path: String,
        old_path: Option<String>,
        end: LineRef,
        start: Option<LineRef>,
        commit: Option<String>,
    },
    /// On a changed file, no specific line.
    File { path: String },
    /// On the MR conversation, with no code anchor.
    Mr,
}

/// One comment inside a remote thread.
#[derive(Debug, Clone)]
pub struct RemoteComment {
    pub id: String,
    pub author: String,
    pub body: String,
    pub created_at: String,
}

/// A discussion thread as it currently exists on the forge.
#[derive(Debug, Clone)]
pub struct RemoteThread {
    pub id: String,
    pub resolved: bool,
    pub outdated: bool,
    pub placement: RemotePlacement,
    pub comments: Vec<RemoteComment>,
}

/// Where a new comment being submitted should land. Carries the reviewed
/// [`DiffVersion`] so an outdated anchor is honoured rather than silently
/// re-based — and so GitLab, whose diff-note `position` needs all three SHAs, can
/// anchor a comment to the snapshot it was written on. The line anchor is the
/// nested [`LineRef`] shape each forge translates to its own anchor form.
#[derive(Debug, Clone)]
pub enum SubmitPlacement {
    Line {
        path: String,
        old_path: Option<String>,
        /// The anchor (end) line; a single-line comment when `start` is `None`.
        end: LineRef,
        /// The start line of a multi-line span, when set. May carry a different
        /// side from `end` (a cross-side span).
        start: Option<LineRef>,
        /// The snapshot version the comment's lines were written against
        /// (resolved from the snapshot history at build time).
        version: DiffVersion,
    },
    File {
        path: String,
        version: DiffVersion,
    },
}

/// A single new comment in a review submission.
#[derive(Debug, Clone)]
pub struct SubmitComment {
    pub placement: SubmitPlacement,
    pub body: String,
}

/// The batched review a `submit` flushes: a verdict, an optional summary body,
/// and the new inline/file comments — all landing as one review where the
/// platform allows (one notification). MR-level conversation comments, replies,
/// and resolves are *not* here; they are separate primitives the orchestration
/// layer calls, because no platform folds them into the review batch.
#[derive(Debug, Clone)]
pub struct ReviewSubmission {
    pub verdict: Option<Verdict>,
    pub summary: Option<String>,
    pub comments: Vec<SubmitComment>,
    pub version: DiffVersion,
}

impl ReviewSubmission {
    /// A submission that would post nothing — no verdict, no body, no comments.
    /// The orchestration layer skips the review call entirely in this case.
    pub fn is_empty(&self) -> bool {
        self.verdict.is_none() && self.summary.is_none() && self.comments.is_empty()
    }
}

/// The granular result of a `submit_review` — never an `Err` for a *partial*
/// success, only for a total one (the MR was unreachable, or the whole batch
/// was rolled back to nothing). Every step that can partially succeed reports
/// its own outcome here, so the orchestration layer reconciles each action
/// independently: a landed comment is cleared, a failed one stays for retry, and
/// a verdict failure never poisons the comments already posted.
///
/// `Err` from `submit_review` means *nothing* landed (atomic backends on a hard
/// failure, or a total transport failure); the caller keeps the whole draft.
#[derive(Debug, Clone, Default)]
pub struct ReviewOutcome {
    /// One result per `review.comments[i]`, in order. `true` = the comment is
    /// visible on the forge; `false` = it failed (or was rolled back) and stays
    /// in the draft. An atomic backend (GitHub) still emits one entry per
    /// comment — all `true` on success — so the caller's index walk is uniform.
    pub comment_results: Vec<bool>,
    /// Whether the summary body (if any) landed.
    pub summary_ok: bool,
    /// `None` when no verdict was in the submission; `Some(true)` when it
    /// landed; `Some(false)` when it was present but failed (stays for retry).
    pub verdict_ok: Option<bool>,
}

impl ReviewOutcome {
    /// A fully-successful outcome for `n` comments — every comment, the
    /// summary, and the verdict (if present) landed.
    pub fn all_ok(n: usize, has_verdict: bool) -> Self {
        ReviewOutcome {
            comment_results: vec![true; n],
            summary_ok: true,
            verdict_ok: has_verdict.then_some(true),
        }
    }

    /// Whether every comment in the batch landed (vacuously true when there are
    /// none). Used to decide whether the review batch as a whole flushed.
    pub fn comments_ok(&self) -> bool {
        self.comment_results.iter().all(|&ok| ok)
    }

    /// Whether the whole submission flushed — comments, summary, and verdict
    /// (when present). Only a fully-flushed draft triggers a re-fetch.
    pub fn fully_ok(&self) -> bool {
        self.comments_ok() && self.summary_ok && self.verdict_ok.unwrap_or(true)
    }
}
