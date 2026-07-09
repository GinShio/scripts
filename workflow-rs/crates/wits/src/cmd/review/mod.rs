//! `wits review` — local-first, forge-agnostic code review.
//!
//! The mirror image of `stack`: where `stack` owns the *existence and structure*
//! of a set of MRs, `review` owns their *review content* — the diff a reviewer
//! reads, the threads they leave, the verdict they render. It never touches the
//! code or the branches; `git` and `stack` do that.
//!
//! Two principles shape the verb set. Acquisition is **forge-first**: an MR is
//! addressed by number, and its objects are fetched and pinned locally, so any
//! MR in the repo can be reviewed without a local branch. And **only two verbs
//! touch the network** — `fetch` (read) and `submit` (write); everything a
//! reviewer does in between is recorded into a local draft and flushed as one
//! batch. See `docs/review/design.md` for the full reasoning.

mod checkout;
mod comment;
mod config;
mod diff;
mod fetch;
mod model;
mod show;
mod store;
mod submit;

use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::{Args, Subcommand, ValueEnum};

use wits_util::forge::{self, Forge, Verdict};
use wits_util::git::Repository;
use wits_util::remote::{RemoteInfo, Remotes};

use store::Store;

#[derive(Debug, Args)]
pub struct ReviewArgs {
    #[command(subcommand)]
    pub action: ReviewAction,
}

#[derive(Debug, Subcommand)]
pub enum ReviewAction {
    /// Fetch an MR (or a feed of MRs) from the forge into the local store.
    Fetch(FetchArgs),
    /// Show the inbox, or one MR's review state (`--json` for editors).
    Show(ShowArgs),
    /// Show a diff's coordinates and anchors for an MR (`--patch` for text).
    Diff(DiffArgs),
    /// Record a comment, reply, or edit into the local draft.
    Comment(CommentArgs),
    /// Set the pending verdict (and optional summary) for an MR.
    Verdict(VerdictArgs),
    /// Drop a pending draft action by its local id.
    Drop(DropArgs),
    /// Record a thread resolution into the draft (GitLab in v1).
    Resolve(ThreadArgs),
    /// Record a thread un-resolution into the draft (GitLab in v1).
    Unresolve(ThreadArgs),
    /// Show the pending draft for an MR (`--json` for editors).
    Draft(DraftArgs),
    /// Flush pending drafts to the forge (the only network write).
    Submit(SubmitArgs),
    /// Materialize an MR's code into a worktree (or in place) to build and test.
    Checkout(CheckoutArgs),
    /// Drop cache and pins for terminal (merged/closed) MRs.
    Prune(PruneArgs),
}

#[derive(Debug, Args)]
pub struct FetchArgs {
    /// The MR to fetch, by number or URL.
    pub mr: Option<String>,
    /// Fetch every MR matching a configured feed instead of one MR.
    #[arg(long, conflicts_with = "mr")]
    pub feed: Option<String>,
    /// Refresh every MR already in the local store.
    #[arg(long, conflicts_with_all = ["mr", "feed"])]
    pub all: bool,
}

#[derive(Debug, Args)]
pub struct ShowArgs {
    /// The MR to show. Omit for the inbox (every fetched MR).
    pub mr: Option<String>,
    /// Only threads whose anchored line has fallen out of the current diff.
    #[arg(long)]
    pub outdated: bool,
    /// Only resolved threads.
    #[arg(long)]
    pub resolved: bool,
    /// Only threads with comments you haven't seen (remote, newer than the draft).
    #[arg(long)]
    pub unread: bool,
    /// Only threads anchored in this file.
    #[arg(long, value_name = "PATH")]
    pub file: Option<String>,
    /// Emit machine-readable JSON — the stable editor contract.
    #[arg(long)]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct DiffArgs {
    /// The MR whose diff to describe.
    pub mr: String,
    /// A git range or rev: `A..B`, a single `<sha>`, or `all` (base..head).
    #[arg(long, default_value = "all")]
    pub range: String,
    /// Print the textual patch (shells to git) instead of coordinates.
    #[arg(long)]
    pub patch: bool,
    /// Emit machine-readable JSON.
    #[arg(long, conflicts_with = "patch")]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct CommentArgs {
    /// The MR to comment on.
    pub mr: String,
    /// Anchor on a code line: `PATH:LINE` or `PATH:LINE:old|new` (default new).
    #[arg(long, value_name = "PATH:LINE[:SIDE]", group = "placement")]
    pub line: Option<String>,
    /// Anchor on a changed file, no specific line.
    #[arg(long, value_name = "PATH", group = "placement")]
    pub file: Option<String>,
    /// An MR-level conversation comment, no code anchor.
    #[arg(long, group = "placement")]
    pub mr_level: bool,
    /// Reply to an existing thread (its id from `show`).
    #[arg(long, value_name = "THREAD", group = "placement")]
    pub reply: Option<String>,
    /// Edit the body of a pending draft comment/reply (its local id).
    #[arg(long, value_name = "ID", group = "placement")]
    pub edit: Option<String>,
    /// A multi-line anchor's start line (with `--line`, marks LINE as the end).
    #[arg(long, value_name = "LINE")]
    pub start_line: Option<u32>,
    /// File to read the comment body from; `-` or omitted reads stdin.
    pub body: Option<PathBuf>,
}

#[derive(Debug, Args)]
pub struct VerdictArgs {
    /// The MR to render a verdict on.
    pub mr: String,
    /// The verdict.
    #[arg(value_enum)]
    pub verdict: VerdictKind,
    /// File to read the review summary from; `-` or omitted reads stdin.
    pub body: Option<PathBuf>,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
pub enum VerdictKind {
    Approve,
    RequestChanges,
    Comment,
}

impl From<VerdictKind> for Verdict {
    fn from(v: VerdictKind) -> Self {
        match v {
            VerdictKind::Approve => Verdict::Approve,
            VerdictKind::RequestChanges => Verdict::RequestChanges,
            VerdictKind::Comment => Verdict::Comment,
        }
    }
}

#[derive(Debug, Args)]
pub struct DropArgs {
    /// The MR the draft belongs to.
    pub mr: String,
    /// The pending action's local id (from `draft`/`show`).
    pub id: String,
}

#[derive(Debug, Args)]
pub struct ThreadArgs {
    /// The MR the thread belongs to.
    pub mr: String,
    /// The thread id (from `show`).
    pub thread: String,
}

#[derive(Debug, Args)]
pub struct DraftArgs {
    /// The MR whose draft to show.
    pub mr: String,
    /// Emit machine-readable JSON.
    #[arg(long)]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct SubmitArgs {
    /// The MR to submit. Omit with `--all`.
    pub mr: Option<String>,
    /// Submit every MR in the stack around the given MR.
    #[arg(long, requires = "mr")]
    pub stack: bool,
    /// Submit every MR that has a pending draft.
    #[arg(long, conflicts_with_all = ["mr", "stack"])]
    pub all: bool,
}

#[derive(Debug, Args)]
pub struct CheckoutArgs {
    /// The MR to materialize (optional with `--next`/`--prev`).
    pub mr: Option<String>,
    /// Materialize the next MR up the stack from the current checkout.
    #[arg(long, conflicts_with = "prev")]
    pub next: bool,
    /// Materialize the previous MR down the stack from the current checkout.
    #[arg(long)]
    pub prev: bool,
    /// Check out in the current working tree (moves HEAD; refuses a dirty tree)
    /// rather than adding a worktree.
    #[arg(long)]
    pub in_place: bool,
    /// Where to put the worktree (default: a sibling `<repo>.review/mr-<id>`).
    #[arg(long, value_name = "DIR", conflicts_with = "in_place")]
    pub worktree: Option<PathBuf>,
}

#[derive(Debug, Args)]
pub struct PruneArgs {
    /// Also drop MRs untouched for at least this many days (implicit death).
    #[arg(long, value_name = "DAYS")]
    pub older_than: Option<u64>,
}

pub fn run(args: &ReviewArgs) -> Result<()> {
    let cwd = std::env::current_dir()?;
    let repo = Repository::new(&cwd);

    match &args.action {
        ReviewAction::Fetch(a) => fetch::run(&repo, a),
        ReviewAction::Show(a) => show::run(&repo, a),
        ReviewAction::Diff(a) => diff::run(&repo, a),
        ReviewAction::Comment(a) => comment::run(&repo, a),
        ReviewAction::Verdict(a) => comment::run_verdict(&repo, a),
        ReviewAction::Drop(a) => comment::run_drop(&repo, a),
        ReviewAction::Resolve(a) => comment::run_resolve(&repo, a, true),
        ReviewAction::Unresolve(a) => comment::run_resolve(&repo, a, false),
        ReviewAction::Draft(a) => show::run_draft(&repo, a),
        ReviewAction::Submit(a) => submit::run(&repo, a),
        ReviewAction::Checkout(a) => checkout::run(&repo, a),
        ReviewAction::Prune(a) => checkout::run_prune(&repo, a),
    }
}

// ---------------------------------------------------------------------------
// Shared context.
// ---------------------------------------------------------------------------

/// The repo's target identity and its store — everything a *local* verb needs,
/// without requiring a forge token. `show`, `comment`, `draft`, and friends work
/// with nothing configured but a remote.
pub(crate) struct Local {
    pub repo: Repository,
    pub target: RemoteInfo,
    pub store: Store,
}

/// Resolve the store and repo identity from the checkout, without contacting the
/// forge. Fails only when there is no remote to key the store on.
pub(crate) fn local(repo: &Repository) -> Result<Local> {
    let remotes = Remotes::resolve(repo);
    let target = remotes
        .target()
        .cloned()
        .context("no 'origin' or 'upstream' remote to derive the forge from")?;
    let store = Store::open(repo, &target)?;
    Ok(Local {
        repo: repo.clone(),
        target,
        store,
    })
}

/// A [`Local`] plus a live forge — what the network verbs (`fetch`, `submit`)
/// need. Detecting the forge resolves the token, so this is where a missing
/// token is reported.
pub(crate) struct Online {
    pub local: Local,
    pub remotes: Remotes,
    pub forge: Box<dyn Forge>,
}

pub(crate) fn online(repo: &Repository) -> Result<Online> {
    let remotes = Remotes::resolve(repo);
    let forge = forge::detect(repo, &remotes)?;
    let local = local(repo)?;
    Ok(Online {
        local,
        remotes,
        forge,
    })
}

/// Parse an MR handle: a bare number, or a forge URL whose last numeric path
/// segment is the number (`…/pull/123`, `…/merge_requests/123`, `…/123`).
pub(crate) fn parse_mr_handle(handle: &str) -> Result<String> {
    let handle = handle.trim();
    if handle.chars().all(|c| c.is_ascii_digit()) && !handle.is_empty() {
        return Ok(handle.to_owned());
    }
    // A URL: take the last all-digit path segment.
    let last_num = handle
        .trim_end_matches('/')
        .rsplit(['/', '#'])
        .find(|seg| !seg.is_empty() && seg.chars().all(|c| c.is_ascii_digit()));
    last_num
        .map(str::to_owned)
        .with_context(|| format!("could not read an MR number from '{handle}'"))
}

/// Reconstruct the stack chain containing `current`, bottom (base-most) to top,
/// by linking one MR's `base` to another's `source` branch. Returns the ordered
/// MR ids and `current`'s index within them. A lone MR yields just itself.
///
/// This is how `review` is stack-aware for *anyone's* stack: the shape comes off
/// the fetched MR list, never a local `.git/machete`.
pub(crate) fn stack_chain(caches: &[model::RemoteCache], current: &str) -> (Vec<String>, usize) {
    use std::collections::HashMap;

    // Index by id, and by the branches that link neighbours.
    let by_id: HashMap<&str, &model::RemoteCache> =
        caches.iter().map(|c| (c.mr.id.as_str(), c)).collect();
    let by_source: HashMap<&str, &str> = caches
        .iter()
        .filter(|c| !c.mr.source.is_empty())
        .map(|c| (c.mr.source.as_str(), c.mr.id.as_str()))
        .collect();
    let by_base: HashMap<&str, &str> = caches
        .iter()
        .filter(|c| !c.mr.base.is_empty())
        .map(|c| (c.mr.base.as_str(), c.mr.id.as_str()))
        .collect();

    let Some(cur) = by_id.get(current) else {
        return (vec![current.to_owned()], 0);
    };

    // Walk down toward the base branch: the MR whose source is our base.
    let mut down = Vec::new();
    let mut seen: std::collections::HashSet<&str> = std::collections::HashSet::new();
    seen.insert(current);
    let mut node = *cur;
    while let Some(&prev) = by_source.get(node.mr.base.as_str()) {
        if !seen.insert(prev) {
            break; // cycle guard
        }
        down.push(prev.to_owned());
        node = by_id[prev];
    }
    down.reverse();

    // Walk up toward the tip: the MR whose base is our source.
    let mut up = Vec::new();
    let mut node = *cur;
    while let Some(&next) = by_base.get(node.mr.source.as_str()) {
        if !seen.insert(next) {
            break;
        }
        up.push(next.to_owned());
        node = by_id[next];
    }

    let index = down.len();
    let mut chain = down;
    chain.push(current.to_owned());
    chain.extend(up);
    (chain, index)
}

/// Read a comment/summary body from a file, or from stdin when the path is `-`
/// or absent. Trailing whitespace is trimmed; a wholly empty body is an error,
/// since posting an empty comment is never intended.
pub(crate) fn read_body(path: Option<&std::path::Path>) -> Result<String> {
    use std::io::Read;
    let raw = match path {
        Some(p) if p.as_os_str() != "-" => std::fs::read_to_string(p)
            .with_context(|| format!("reading body from {}", p.display()))?,
        _ => {
            let mut buf = String::new();
            std::io::stdin()
                .read_to_string(&mut buf)
                .context("reading body from stdin")?;
            buf
        }
    };
    let trimmed = raw.trim_end().to_owned();
    if trimmed.trim().is_empty() {
        anyhow::bail!("empty comment body");
    }
    Ok(trimmed)
}

#[cfg(test)]
pub(crate) fn stub_cache(id: &str, base: &str, source: &str) -> model::RemoteCache {
    model::RemoteCache {
        schema: model::SCHEMA,
        mr: model::MrInfo {
            id: id.into(),
            display: format!("#{id}"),
            state: "open".into(),
            draft: false,
            title: String::new(),
            author: String::new(),
            base: base.into(),
            source: source.into(),
            head_sha: None,
            updated_at: String::new(),
            labels: Vec::new(),
            web_url: String::new(),
        },
        version: Default::default(),
        fetched_at: String::new(),
        commits: Vec::new(),
        files: Vec::new(),
        threads: Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reconstructs_a_stack_from_mr_base_links() {
        // main <- a(feat-a) <- b(feat-b) <- c(feat-c): b's base is feat-a, etc.
        let caches = vec![
            stub_cache("a", "main", "feat-a"),
            stub_cache("b", "feat-a", "feat-b"),
            stub_cache("c", "feat-b", "feat-c"),
        ];
        let (chain, pos) = stack_chain(&caches, "b");
        assert_eq!(chain, ["a", "b", "c"]);
        assert_eq!(pos, 1);

        // From the bottom, the whole chain is above.
        let (chain, pos) = stack_chain(&caches, "a");
        assert_eq!(chain, ["a", "b", "c"]);
        assert_eq!(pos, 0);
    }

    #[test]
    fn a_lone_mr_is_its_own_chain() {
        let caches = vec![stub_cache("x", "main", "feature-x")];
        assert_eq!(stack_chain(&caches, "x"), (vec!["x".to_owned()], 0));
        // An MR not in the cache still yields itself.
        assert_eq!(stack_chain(&caches, "z"), (vec!["z".to_owned()], 0));
    }

    #[test]
    fn parses_mr_numbers_and_urls() {
        assert_eq!(parse_mr_handle("123").unwrap(), "123");
        assert_eq!(
            parse_mr_handle("https://github.com/o/r/pull/456").unwrap(),
            "456"
        );
        assert_eq!(
            parse_mr_handle("https://gitlab.com/o/r/-/merge_requests/789/").unwrap(),
            "789"
        );
        assert!(parse_mr_handle("not-a-number").is_err());
        assert!(parse_mr_handle("").is_err());
    }
}
