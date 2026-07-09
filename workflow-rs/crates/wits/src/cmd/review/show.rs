//! `wits review show` / `draft` — the read path, and the stable `--json`
//! contract the editor consumes.
//!
//! `show` with no MR is the inbox; with an MR it is the detail view, which folds
//! the pending draft into the remote threads so the editor sees one merged
//! picture. Filtering (`--outdated`/`--resolved`/`--unread`/`--file`) is the knob
//! for large MRs — the payload is always the whole MR, never paginated.

use anyhow::{Context, Result};
use serde::Serialize;

use wits_util::git::Repository;

use super::model::{
    Action, Comment, Draft, MrInfo, Placement, RemoteCache, StoredCommit, StoredFile, Thread,
    SCHEMA,
};
use super::{local, ShowArgs};

#[derive(Serialize)]
struct Snapshot {
    base_sha: String,
    head_sha: String,
}

#[derive(Serialize)]
struct Neighbors {
    position: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    prev_mr: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    next_mr: Option<String>,
    nodes: Vec<String>,
}

#[derive(Serialize)]
struct DraftView {
    #[serde(skip_serializing_if = "Option::is_none")]
    verdict: Option<wits_util::forge::Verdict>,
    #[serde(skip_serializing_if = "Option::is_none")]
    summary: Option<String>,
    pending: usize,
}

#[derive(Serialize)]
struct DetailView {
    schema: u32,
    mr: MrInfo,
    snapshot: Snapshot,
    neighbors: Neighbors,
    commits: Vec<StoredCommit>,
    files: Vec<StoredFile>,
    threads: Vec<Thread>,
    draft: DraftView,
}

#[derive(Serialize)]
struct InboxReview {
    pending: usize,
    outdated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    reviewed_sha: Option<String>,
}

#[derive(Serialize)]
struct InboxItem {
    #[serde(flatten)]
    mr: MrInfo,
    review: InboxReview,
}

#[derive(Serialize)]
struct InboxView {
    schema: u32,
    items: Vec<InboxItem>,
}

pub fn run(repo: &Repository, args: &ShowArgs) -> Result<()> {
    let ctx = local(repo)?;
    match &args.mr {
        Some(handle) => {
            let id = super::parse_mr_handle(handle)?;
            show_detail(&ctx, &id, args)
        }
        None => show_inbox(&ctx, args),
    }
}

fn show_detail(ctx: &super::Local, id: &str, args: &ShowArgs) -> Result<()> {
    let cache = ctx.store.load_cache(id).with_context(|| {
        format!("MR {id} isn't in the store yet — run `wits review fetch {id}` first")
    })?;
    let draft = ctx.store.load_draft(id);

    let all_caches = ctx.store.list_cached();
    let (nodes, position) = super::stack_chain(&all_caches, id);
    let neighbors = Neighbors {
        position,
        prev_mr: position.checked_sub(1).and_then(|i| nodes.get(i).cloned()),
        next_mr: nodes.get(position + 1).cloned(),
        nodes,
    };

    let mut threads = merge_threads(&cache, &draft);
    apply_filters(&mut threads, args);

    let view = DetailView {
        schema: SCHEMA,
        snapshot: Snapshot {
            base_sha: cache.version.base_sha.clone(),
            head_sha: cache.version.head_sha.clone(),
        },
        neighbors,
        commits: cache.commits.clone(),
        files: cache.files.clone(),
        threads,
        draft: DraftView {
            verdict: draft.verdict,
            summary: draft.summary.clone(),
            pending: pending_count(&draft),
        },
        mr: cache.mr,
    };

    if args.json {
        println!("{}", serde_json::to_string_pretty(&view)?);
    } else {
        print_detail_human(&view);
    }
    Ok(())
}

/// Fold the pending draft into the remote threads: replies attach to their
/// remote thread as pending comments, resolutions flip the flag, and new
/// comments become fresh local threads.
fn merge_threads(cache: &RemoteCache, draft: &Draft) -> Vec<Thread> {
    let mut threads = cache.threads.clone();

    for action in &draft.actions {
        match action {
            Action::Comment {
                id,
                placement,
                body,
            } => threads.push(Thread {
                id: id.clone(),
                origin: "local".into(),
                resolved: false,
                outdated: false,
                placement: placement.clone(),
                comments: vec![pending_comment(id, body)],
            }),
            Action::Reply { id, thread, body } => {
                let target = format!("remote:{thread}");
                if let Some(t) = threads.iter_mut().find(|t| t.id == target) {
                    t.comments.push(pending_comment(id, body));
                }
            }
            Action::Resolve { thread, resolved } => {
                let target = format!("remote:{thread}");
                if let Some(t) = threads.iter_mut().find(|t| t.id == target) {
                    t.resolved = *resolved;
                }
            }
        }
    }
    threads
}

fn pending_comment(id: &str, body: &str) -> Comment {
    Comment {
        id: id.to_owned(),
        author: "@me".into(),
        origin: "local".into(),
        body: body.to_owned(),
        created_at: String::new(),
        state: "pending".into(),
    }
}

fn apply_filters(threads: &mut Vec<Thread>, args: &ShowArgs) {
    if args.outdated {
        threads.retain(|t| t.outdated);
    }
    if args.resolved {
        threads.retain(|t| t.resolved);
    }
    if args.unread {
        // "Unread" is a practical proxy: the last activity on the thread is
        // someone else's (a remote comment), so it likely awaits your reply.
        threads.retain(|t| t.comments.last().is_some_and(|c| c.origin == "remote"));
    }
    if let Some(path) = &args.file {
        threads.retain(|t| placement_path(&t.placement) == Some(path.as_str()));
    }
}

fn placement_path(p: &Placement) -> Option<&str> {
    match p {
        Placement::Line { path, .. } | Placement::File { path, .. } => Some(path),
        Placement::Mr => None,
    }
}

fn pending_count(draft: &Draft) -> usize {
    draft.actions.len() + usize::from(draft.verdict.is_some() || draft.summary.is_some())
}

fn show_inbox(ctx: &super::Local, args: &ShowArgs) -> Result<()> {
    let mut caches = ctx.store.list_cached();
    caches.sort_by(|a, b| b.mr.updated_at.cmp(&a.mr.updated_at));

    let items: Vec<InboxItem> = caches
        .into_iter()
        .map(|cache| {
            let draft = ctx.store.load_draft(&cache.mr.id);
            let reviewed_sha = draft.actions.iter().find_map(|a| match a {
                Action::Comment {
                    placement: Placement::Line { commit, .. } | Placement::File { commit, .. },
                    ..
                } => commit.clone(),
                _ => None,
            });
            let outdated = reviewed_sha
                .as_ref()
                .is_some_and(|s| *s != cache.version.head_sha);
            InboxItem {
                review: InboxReview {
                    pending: pending_count(&draft),
                    outdated,
                    reviewed_sha,
                },
                mr: cache.mr,
            }
        })
        .collect();

    if args.json {
        let view = InboxView {
            schema: SCHEMA,
            items,
        };
        println!("{}", serde_json::to_string_pretty(&view)?);
    } else if items.is_empty() {
        println!(
            "(nothing fetched — `wits review fetch <mr>` or `wits review fetch --feed <name>`)"
        );
    } else {
        for item in &items {
            let pending = if item.review.pending > 0 {
                format!("  pending:{}", item.review.pending)
            } else {
                String::new()
            };
            let stale = if item.review.outdated {
                " (outdated)"
            } else {
                ""
            };
            println!(
                "{:<7} [{}] {}  ({}){pending}{stale}",
                item.mr.display, item.mr.state, item.mr.title, item.mr.author
            );
        }
    }
    Ok(())
}

fn print_detail_human(view: &DetailView) {
    println!("{} [{}] {}", view.mr.display, view.mr.state, view.mr.title);
    println!(
        "  by {} · base {} · {}",
        view.mr.author, view.mr.base, view.mr.web_url
    );
    println!(
        "  snapshot {}..{}",
        short(&view.snapshot.base_sha),
        short(&view.snapshot.head_sha)
    );
    if view.neighbors.nodes.len() > 1 {
        println!("  stack: {}", view.neighbors.nodes.join(" -> "));
    }
    if !view.files.is_empty() {
        println!("  files:");
        for f in &view.files {
            println!("    {} {}", f.status, f.path);
        }
    }
    if view.threads.is_empty() {
        println!("  (no threads)");
    } else {
        println!("  threads:");
        for t in &view.threads {
            let flags = [
                (t.resolved, "resolved"),
                (t.outdated, "outdated"),
                (t.origin == "local", "pending"),
            ]
            .iter()
            .filter(|(on, _)| *on)
            .map(|(_, s)| *s)
            .collect::<Vec<_>>()
            .join(",");
            let flags = if flags.is_empty() {
                String::new()
            } else {
                format!(" [{flags}]")
            };
            println!("    {} {}{flags}", t.id, describe_placement(&t.placement));
            for c in &t.comments {
                println!("      {} ({}): {}", c.author, c.origin, first_line(&c.body));
            }
        }
    }
    if pending_count_view(&view.draft) > 0 {
        let verdict = view
            .draft
            .verdict
            .map(|v| format!(" verdict={}", verdict_word(v)))
            .unwrap_or_default();
        println!("  draft: {} pending action(s){verdict}", view.draft.pending);
    }
}

fn pending_count_view(d: &DraftView) -> usize {
    d.pending
}

fn verdict_word(v: wits_util::forge::Verdict) -> &'static str {
    match v {
        wits_util::forge::Verdict::Approve => "approve",
        wits_util::forge::Verdict::RequestChanges => "request-changes",
        wits_util::forge::Verdict::Comment => "comment",
    }
}

fn describe_placement(p: &Placement) -> String {
    match p {
        Placement::Line {
            path, line, side, ..
        } => format!("{path}:{line} ({})", side.as_str()),
        Placement::File { path, .. } => format!("{path} (file)"),
        Placement::Mr => "(conversation)".to_owned(),
    }
}

fn first_line(s: &str) -> &str {
    s.lines().next().unwrap_or("")
}

fn short(sha: &str) -> &str {
    if sha.len() > 8 {
        &sha[..8]
    } else {
        sha
    }
}

pub fn run_draft(repo: &Repository, args: &super::DraftArgs) -> Result<()> {
    let ctx = local(repo)?;
    let id = super::parse_mr_handle(&args.mr)?;
    let draft = ctx.store.load_draft(&id);

    if args.json {
        println!("{}", serde_json::to_string_pretty(&draft)?);
        return Ok(());
    }

    if draft.is_empty() {
        println!("(no pending draft for MR {id})");
        return Ok(());
    }
    if let Some(v) = draft.verdict {
        println!("verdict: {}", verdict_word(v));
    }
    if let Some(s) = &draft.summary {
        println!("summary: {}", first_line(s));
    }
    for action in &draft.actions {
        match action {
            Action::Comment {
                id,
                placement,
                body,
            } => {
                println!(
                    "  {id}  comment {}  {}",
                    describe_placement(placement),
                    first_line(body)
                )
            }
            Action::Reply { id, thread, body } => {
                println!("  {id}  reply -> remote:{thread}  {}", first_line(body))
            }
            Action::Resolve { thread, resolved } => {
                let verb = if *resolved { "resolve" } else { "unresolve" };
                println!("  {verb} remote:{thread}")
            }
        }
    }
    Ok(())
}
