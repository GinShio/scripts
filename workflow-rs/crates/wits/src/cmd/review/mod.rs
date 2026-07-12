//! `wits review` — local-first, forge-agnostic code review.
//!
//! The mirror image of `stack`: where `stack` owns the *existence and structure*
//! of a set of MRs, `review` owns their *review content* — the diff a reviewer
//! reads, the threads they leave, the verdict they render. It never touches the
//! code or the branches; `git` and `stack` do that.
//!
//! Two principles shape it. Acquisition is **forge-first**: an MR is addressed
//! by number, and its objects are fetched and pinned locally, so any MR in the
//! repo can be reviewed without a local branch. And you **author by editing a
//! local file**, not by running commands — only two verbs touch the network,
//! `fetch` (read) and `submit` (write); in between you edit `local.json` and
//! `submit` flushes it as one batch. See `docs/review/design.md`.

mod checkout;
mod config;
mod diff;
mod fetch;
mod model;
mod show;
mod store;
mod submit;

use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::{Args, Subcommand};

use wits_util::forge::{self, Forge};
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
    /// Fetch an MR, a feed, or every feed from the forge into the local store.
    Fetch(FetchArgs),
    /// Show the inbox, or one MR's review state (`--json` for editors).
    Show(ShowArgs),
    /// Show a diff's coordinates for an MR (`--patch` for text, `--json` for editors).
    Diff(DiffArgs),
    /// Show the pending local draft for an MR (`--json` for editors).
    Draft(DraftArgs),
    /// Flush the local draft to the forge (the only network write).
    Submit(SubmitArgs),
    /// Materialize an MR's code into a worktree (or in place) to build and test.
    Checkout(CheckoutArgs),
    /// Drop the store for terminal (merged/closed) or dormant MRs.
    Prune(PruneArgs),
}

#[derive(Debug, Args)]
pub struct FetchArgs {
    /// The MR to fetch, by number or URL (a full pull).
    pub mr: Option<String>,
    /// Fetch a configured feed's MRs (a light, inbox-only refresh).
    #[arg(long, conflicts_with = "mr")]
    pub feed: Option<String>,
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
    /// Only unresolved threads.
    #[arg(long, conflicts_with = "resolved")]
    pub unresolved: bool,
    /// Only threads whose last comment is someone else's.
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
    /// Browse a historical snapshot by its head SHA (a prefix is fine); its
    /// base..head replaces `--range`.
    #[arg(long, value_name = "SHA", conflicts_with = "range")]
    pub snapshot: Option<String>,
    /// Print the textual patch (shells to git) instead of coordinates.
    #[arg(long)]
    pub patch: bool,
    /// Emit machine-readable JSON.
    #[arg(long, conflicts_with = "patch")]
    pub json: bool,
}

#[derive(Debug, Args)]
pub struct DraftArgs {
    /// The MR whose draft to show or append to.
    pub mr: String,
    /// A JSON batch of actions to append to the draft; `-` or a file. When
    /// given, `draft` ingests (the tool owns the write); when omitted, it shows.
    pub input: Option<PathBuf>,
    /// Emit machine-readable JSON (on show).
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
    /// Also drop MRs untouched for at least this long: a number of days, or an
    /// ISO-8601 date (`2026-06-01`) — anything last updated before it.
    #[arg(long, value_name = "DAYS|DATE")]
    pub older_than: Option<String>,
}

pub fn run(args: &ReviewArgs) -> Result<()> {
    let cwd = std::env::current_dir()?;
    let repo = Repository::new(&cwd);

    match &args.action {
        ReviewAction::Fetch(a) => fetch::run(&repo, a),
        ReviewAction::Show(a) => show::run(&repo, a),
        ReviewAction::Diff(a) => diff::run(&repo, a),
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
/// without a forge token.
pub(crate) struct Local {
    pub repo: Repository,
    pub target: RemoteInfo,
    pub store: Store,
}

pub(crate) fn local(repo: &Repository) -> Result<Local> {
    let remotes = Remotes::resolve(repo);
    local_from_remotes(repo, &remotes)
}

fn local_from_remotes(repo: &Repository, remotes: &Remotes) -> Result<Local> {
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
/// need. Detecting the forge resolves the token, so a missing token is reported
/// here.
pub(crate) struct Online {
    pub local: Local,
    pub remotes: Remotes,
    pub forge: Box<dyn Forge>,
}

pub(crate) fn online(repo: &Repository) -> Result<Online> {
    let remotes = Remotes::resolve(repo);
    let forge = forge::detect(repo, &remotes)?;
    let local = local_from_remotes(repo, &remotes)?;
    Ok(Online {
        local,
        remotes,
        forge,
    })
}

/// How many forge operations run at once. Review submissions are independent per
/// MR and network-bound, so scoped OS threads keep the latency down without
/// any tuning burden.
const MAX_PARALLEL: usize = 8;

/// Run `f` over `items` with bounded parallelism, returning results in input
/// order. Uses scoped threads so the closure can borrow freely from the
/// surrounding stack frame — no `Arc`, no `'static` bounds.
pub(crate) fn map_parallel<I, T>(items: &[I], f: impl Fn(&I) -> T + Sync) -> Vec<T>
where
    I: Sync,
    T: Send,
{
    let mut out = Vec::with_capacity(items.len());
    for chunk in items.chunks(MAX_PARALLEL.max(1)) {
        std::thread::scope(|scope| {
            let handles: Vec<_> = chunk.iter().map(|item| scope.spawn(|| f(item))).collect();
            for handle in handles {
                out.push(handle.join().expect("worker thread panicked"));
            }
        });
    }
    out
}

/// Parse an MR handle: a bare number, or a forge URL whose last numeric path
/// segment is the number (`…/pull/123`, `…/merge_requests/123`).
pub(crate) fn parse_mr_handle(handle: &str) -> Result<String> {
    let handle = handle.trim();
    if !handle.is_empty() && handle.chars().all(|c| c.is_ascii_digit()) {
        return Ok(handle.to_owned());
    }
    handle
        .trim_end_matches('/')
        .rsplit(['/', '#'])
        .find(|seg| !seg.is_empty() && seg.chars().all(|c| c.is_ascii_digit()))
        .map(str::to_owned)
        .with_context(|| format!("could not read an MR number from '{handle}'"))
}

/// Reconstruct the stack chain containing `current`, bottom (base-most) to top,
/// by linking one MR's `base` to another's `source` branch. Returns the ordered
/// ids and `current`'s index. A lone MR yields just itself.
pub(crate) fn stack_chain(infos: &[model::Info], current: &str) -> (Vec<String>, usize) {
    use std::collections::{HashMap, HashSet};

    let by_id: HashMap<&str, &model::Info> = infos.iter().map(|i| (i.mr.id.as_str(), i)).collect();
    let by_source: HashMap<&str, &str> = infos
        .iter()
        .filter(|i| !i.mr.source.is_empty())
        .map(|i| (i.mr.source.as_str(), i.mr.id.as_str()))
        .collect();
    let by_base: HashMap<&str, Vec<&str>> = infos.iter().fold(HashMap::new(), |mut map, i| {
        if !i.mr.base.is_empty() {
            map.entry(i.mr.base.as_str())
                .or_default()
                .push(i.mr.id.as_str());
        }
        map
    });

    let Some(cur) = by_id.get(current) else {
        return (vec![current.to_owned()], 0);
    };

    let mut seen: HashSet<&str> = HashSet::from([current]);
    let mut down = Vec::new();
    let mut node = *cur;
    while let Some(&prev) = by_source.get(node.mr.base.as_str()) {
        if !seen.insert(prev) {
            break;
        }
        down.push(prev.to_owned());
        node = by_id[prev];
    }
    down.reverse();

    let mut up = Vec::new();
    let mut node = *cur;
    while let Some(children) = by_base.get(node.mr.source.as_str()) {
        if children.len() > 1 {
            log::warn!(
                "stack fork at {}: {} children ({}), following first",
                node.mr.source,
                children.len(),
                children.join(", ")
            );
        }
        let &next = &children[0];
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

#[cfg(test)]
pub(crate) fn stub_info(id: &str, base: &str, source: &str) -> model::Info {
    model::Info {
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
        snapshots: Vec::new(),
        commits: Vec::new(),
        files: Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reconstructs_a_stack_from_mr_base_links() {
        let infos = vec![
            stub_info("a", "main", "feat-a"),
            stub_info("b", "feat-a", "feat-b"),
            stub_info("c", "feat-b", "feat-c"),
        ];
        assert_eq!(
            stack_chain(&infos, "b"),
            (vec!["a".into(), "b".into(), "c".into()], 1)
        );
        assert_eq!(
            stack_chain(&infos, "a"),
            (vec!["a".into(), "b".into(), "c".into()], 0)
        );
    }

    #[test]
    fn a_lone_mr_is_its_own_chain() {
        let infos = vec![stub_info("x", "main", "feature-x")];
        assert_eq!(stack_chain(&infos, "x"), (vec!["x".to_owned()], 0));
        assert_eq!(stack_chain(&infos, "z"), (vec!["z".to_owned()], 0));
    }

    #[test]
    fn follows_first_child_at_a_fork() {
        // Two MRs branch off the same base (a fork in the stack).
        // The chain follows the first child deterministically and warns.
        let infos = vec![
            stub_info("a", "main", "feat-a"),
            stub_info("b", "feat-a", "feat-b"),
            stub_info("c", "feat-a", "feat-c"),
        ];
        let (chain, idx) = stack_chain(&infos, "a");
        // a is at index 0, followed by one of b/c (insertion order → b first).
        assert_eq!(idx, 0);
        assert_eq!(chain[0], "a");
        assert_eq!(chain.len(), 2);
        assert!(chain[1] == "b" || chain[1] == "c");
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
    }
}
