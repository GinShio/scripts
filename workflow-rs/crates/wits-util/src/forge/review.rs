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
    /// On a code line of a changed file.
    Line {
        path: String,
        old_path: Option<String>,
        side: Side,
        line: u32,
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
/// `commit` so an outdated anchor is honoured rather than silently re-based.
#[derive(Debug, Clone)]
pub enum SubmitPlacement {
    Line {
        path: String,
        old_path: Option<String>,
        side: Side,
        /// The end line of the comment (a single line when `start_line` is None).
        line: u32,
        /// The first line of a multi-line comment, if any.
        start_line: Option<u32>,
        commit: String,
    },
    File {
        path: String,
        commit: String,
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
